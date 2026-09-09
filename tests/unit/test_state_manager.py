"""
Unit tests for crash-resilient atomic state manager in src.state_manager.
Verifies write-fsync-replace protocol, mode 0600 permissions, backup rotation,
crash recovery from truncated/corrupted states, and on-disk chunk inventory reconciliation.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from src.exceptions import StateCorruptedError, StateNotFoundError
from src.models import (
    ChunkState,
    ChunkStatus,
    DownloadState,
    DownloadStatistics,
    DownloadStatus,
)
from src.state_manager import (
    STATE_FILE_PERMISSIONS,
    StateManager,
)


def create_sample_state(
    download_id: str = "dl-test-01",
    target_path: str = "/tmp/downloads/sample.iso",
    num_chunks: int = 4,
) -> DownloadState:
    """Helper to generate a populated DownloadState."""
    chunks = []
    chunk_size = 1024 * 1024  # 1 MB
    for i in range(num_chunks):
        chunks.append(
            ChunkState(
                index=i,
                start_byte=i * chunk_size,
                end_byte=((i + 1) * chunk_size) - 1,
                status=ChunkStatus.PENDING,
            )
        )

    stats = DownloadStatistics(
        bytes_completed=0,
        bytes_total=num_chunks * chunk_size,
        speed_bps=0.0,
        active_workers=0,
    )

    return DownloadState(
        download_id=download_id,
        url="https://example.com/sample.iso",
        target_path=target_path,
        file_size=num_chunks * chunk_size,
        chunk_size=chunk_size,
        chunks=chunks,
        statistics=stats,
        status=DownloadStatus.PENDING,
    )


class TestAtomicSaveAndPermissions(unittest.TestCase):
    """Tests for atomic state file creation and POSIX 0600 permissions."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.manager = StateManager(default_state_dir=".ReliaDL", backup_enabled=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_atomic_save_and_mode_0600(self) -> None:
        target = self.dir_path / "data.bin"
        state = create_sample_state(target_path=str(target))

        state_path = self.manager.save(state)
        self.assertTrue(state_path.is_file())

        # Check POSIX file mode 0600 (owner read/write only)
        file_stat = os.stat(state_path)
        mode = stat.S_IMODE(file_stat.st_mode)
        self.assertEqual(mode, STATE_FILE_PERMISSIONS)

        # Ensure temporary files were cleaned up
        tmp_files = list(state_path.parent.glob("*.tmp*"))
        self.assertEqual(len(tmp_files), 0)

    def test_backup_rotation_on_subsequent_save(self) -> None:
        target = self.dir_path / "archive.tar"
        state = create_sample_state(target_path=str(target))

        # First save
        state_path = self.manager.save(state)
        bak_path = state_path.parent / f"{state_path.name}.bak"
        self.assertFalse(bak_path.exists())

        # Modify state and save again
        state.status = DownloadStatus.IN_PROGRESS
        state.chunks[0].mark_complete(computed_hash="abc123hash")
        self.manager.save(state)

        # Backup should now exist and hold the original PENDING state
        self.assertTrue(bak_path.is_file())
        with open(bak_path, "r", encoding="utf-8") as f:
            bak_data = json.load(f)
        self.assertEqual(bak_data["status"], "PENDING")

        # Primary state holds updated IN_PROGRESS state
        with open(state_path, "r", encoding="utf-8") as f:
            primary_data = json.load(f)
        self.assertEqual(primary_data["status"], "IN_PROGRESS")


class TestCrashRecovery(unittest.TestCase):
    """Tests for crash recovery on interrupted/corrupted state files."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.manager = StateManager(backup_enabled=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_recovery_from_backup_on_primary_corruption(self) -> None:
        target = self.dir_path / "file.iso"
        state = create_sample_state(target_path=str(target))

        # Save initial state and rotate to backup
        state_path = self.manager.save(state)
        state.chunks[0].mark_complete()
        self.manager.save(state)

        # Simulate crash: truncate / corrupt primary state file
        with open(state_path, "w", encoding="utf-8") as f:
            f.write('{"version": "1.0.0", "download_id": "corrupted')  # Incomplete JSON

        # Load should detect corruption and auto-recover from .bak
        recovered = self.manager.load(state_path, auto_recover=True)
        self.assertEqual(recovered.download_id, state.download_id)

        # Primary file should have been re-persisted and valid
        with open(state_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["download_id"], state.download_id)

    def test_corrupted_state_without_backup_raises(self) -> None:
        state_path = self.dir_path / "single.state"
        with open(state_path, "w", encoding="utf-8") as f:
            f.write("GARBAGE_NOT_JSON")

        with self.assertRaises(StateCorruptedError):
            self.manager.load(state_path, auto_recover=True)

    def test_empty_state_file_raises_corrupted(self) -> None:
        state_path = self.dir_path / "empty.state"
        state_path.touch()

        with self.assertRaises(StateCorruptedError):
            self.manager.load(state_path, auto_recover=False)

    def test_nonexistent_state_raises_not_found(self) -> None:
        with self.assertRaises(StateNotFoundError):
            self.manager.load(self.dir_path / "does_not_exist.state")


class TestChunkInventoryReconciliation(unittest.TestCase):
    """Tests for on-disk chunk inventory verification and reconciliation."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.manager = StateManager()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_reconcile_missing_and_truncated_chunks(self) -> None:
        chunk_dir = self.dir_path / "chunks"
        chunk_dir.mkdir()

        state = create_sample_state(num_chunks=4)
        chunk0, chunk1, chunk2, chunk3 = state.chunks

        # Chunk 0: Marked complete, file fully intact on disk -> keeps COMPLETE
        chunk0.mark_complete()
        c0_path = (chunk_dir / "chunk_0.part").resolve()
        with open(c0_path, "wb") as f:
            f.write(b"0" * chunk0.size)

        # Chunk 1: Marked complete, but file missing on disk -> reverts to FAILED
        chunk1.mark_complete()

        # Chunk 2: Marked complete, but file truncated on disk -> reverts to FAILED
        chunk2.mark_complete()
        c2_path = (chunk_dir / "chunk_2.part").resolve()
        with open(c2_path, "wb") as f:
            f.write(b"2" * (chunk2.size // 2))

        # Chunk 3: Was IN_PROGRESS, but complete size exists on disk -> transitions to VERIFYING
        chunk3.mark_in_progress()
        c3_path = (chunk_dir / "chunk_3.part").resolve()
        with open(c3_path, "wb") as f:
            f.write(b"3" * chunk3.size)

        reconciled = self.manager.reconcile_inventory(state, chunk_dir)

        # Assertions
        self.assertEqual(reconciled.chunks[0].status, ChunkStatus.COMPLETE)
        self.assertEqual(reconciled.chunks[0].file_path, str(c0_path))

        self.assertEqual(reconciled.chunks[1].status, ChunkStatus.FAILED)
        self.assertFalse(reconciled.chunks[1].hash_verified)

        self.assertEqual(reconciled.chunks[2].status, ChunkStatus.FAILED)
        self.assertFalse(reconciled.chunks[2].hash_verified)

        self.assertEqual(reconciled.chunks[3].status, ChunkStatus.VERIFYING)


class TestStateLifecycleAndCleanup(unittest.TestCase):
    """Tests for exists and delete operations."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.manager = StateManager(backup_enabled=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_exists_and_delete(self) -> None:
        target = self.dir_path / "clean.bin"
        state = create_sample_state(target_path=str(target))

        state_path = self.manager.save(state)
        self.assertTrue(self.manager.exists(state_path))

        # Save again to trigger backup
        self.manager.save(state)
        bak_path = state_path.parent / f"{state_path.name}.bak"
        self.assertTrue(bak_path.is_file())

        # Delete with backup
        self.manager.delete(state_path, delete_backup=True)
        self.assertFalse(state_path.is_file())
        self.assertFalse(bak_path.is_file())
        self.assertFalse(self.manager.exists(state_path))


if __name__ == "__main__":
    unittest.main()
