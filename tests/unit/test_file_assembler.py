"""
Unit tests for sequential chunk file assembler in src.file_assembler.
Verifies multi-part reassembly, whole-file SHA-256 integrity verification,
automatic intermediate chunk cleanup, and error handling for missing/corrupted chunks.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from src.exceptions import AssemblyFailedError, FileHashMismatchError
from src.file_assembler import FileAssembler
from src.models import (
    ChunkState,
    ChunkStatus,
    DownloadState,
    DownloadStatistics,
    DownloadStatus,
)


def build_chunked_test_data(
    chunk_sizes: list[int],
    chunk_dir: Path,
) -> tuple[DownloadState, bytes, str]:
    """
    Helper to generate test chunks on disk and a corresponding DownloadState.
    """
    full_payload = bytearray()
    chunks = []
    current_offset = 0

    for i, size in enumerate(chunk_sizes):
        # Distinct content per chunk to detect ordering issues
        chunk_data = bytes([(i * 37 + j) % 256 for j in range(size)])
        full_payload.extend(chunk_data)

        chunk_path = chunk_dir / f"chunk_{i}.part"
        with open(chunk_path, "wb") as f:
            f.write(chunk_data)

        start = current_offset
        end = current_offset + size - 1
        current_offset += size

        c_state = ChunkState(
            index=i,
            start_byte=start,
            end_byte=end,
            status=ChunkStatus.COMPLETE,
            file_path=str(chunk_path),
        )
        chunks.append(c_state)

    full_bytes = bytes(full_payload)
    full_hash = hashlib.sha256(full_bytes).hexdigest()

    stats = DownloadStatistics(
        bytes_completed=len(full_bytes),
        bytes_total=len(full_bytes),
    )

    state = DownloadState(
        download_id="dl-asm-01",
        url="https://example.com/blob.dat",
        target_path=str(chunk_dir.parent / "assembled_output.bin"),
        file_size=len(full_bytes),
        chunks=chunks,
        statistics=stats,
        status=DownloadStatus.COMPLETE,
        expected_file_hash=full_hash,
    )

    return state, full_bytes, full_hash


class TestFileAssembler(unittest.TestCase):
    """Tests for FileAssembler."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.dir_path = Path(self.temp_dir.name)
        self.chunk_dir = self.dir_path / "chunks"
        self.chunk_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_multi_part_reassembly_success(self) -> None:
        # Create 4 chunks with different sizes
        chunk_sizes = [65536, 32768, 131072, 16384]
        state, expected_payload, expected_hash = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        out_path = self.dir_path / "final_output.bin"
        assembler = FileAssembler(cleanup_chunks=False, verify_hash=True)

        assembled_path = assembler.assemble(
            state=state,
            chunk_dir=self.chunk_dir,
            output_path=out_path,
        )

        self.assertTrue(assembled_path.is_file())
        with open(assembled_path, "rb") as f:
            assembled_data = f.read()

        # Check payload exact match
        self.assertEqual(assembled_data, expected_payload)

        # Check hash calculation
        computed_hash = hashlib.sha256(assembled_data).hexdigest()
        self.assertEqual(computed_hash, expected_hash)

        # Chunks should still exist since cleanup_chunks=False
        for i in range(len(chunk_sizes)):
            self.assertTrue((self.chunk_dir / f"chunk_{i}.part").is_file())

    def test_automatic_cleanup_of_chunks(self) -> None:
        chunk_sizes = [1024, 2048, 4096]
        state, expected_payload, expected_hash = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        out_path = self.dir_path / "cleaned_output.bin"
        assembler = FileAssembler(cleanup_chunks=True, verify_hash=True)

        assembled_path = assembler.assemble(
            state=state,
            chunk_dir=self.chunk_dir,
            output_path=out_path,
        )

        self.assertTrue(assembled_path.is_file())
        with open(assembled_path, "rb") as f:
            self.assertEqual(f.read(), expected_payload)

        # Intermediate chunk files and empty chunk dir should be removed
        self.assertFalse((self.chunk_dir / "chunk_0.part").exists())
        self.assertFalse(self.chunk_dir.exists())

    def test_missing_chunk_raises_assembly_failed_error(self) -> None:
        chunk_sizes = [1024, 1024, 1024, 1024]
        state, _, _ = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        # Delete chunk 2
        missing_file = self.chunk_dir / "chunk_2.part"
        missing_file.unlink()

        out_path = self.dir_path / "failed_output.bin"
        assembler = FileAssembler(cleanup_chunks=False)

        with self.assertRaises(AssemblyFailedError) as ctx:
            assembler.assemble(
                state=state,
                chunk_dir=self.chunk_dir,
                output_path=out_path,
            )

        self.assertIn(2, ctx.exception.missing_chunks)
        self.assertFalse(out_path.exists())

    def test_truncated_chunk_raises_assembly_failed_error(self) -> None:
        chunk_sizes = [2048, 2048]
        state, _, _ = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        # Truncate chunk 1 to half its size
        chunk1_path = self.chunk_dir / "chunk_1.part"
        with open(chunk1_path, "wb") as f:
            f.write(b"T" * 1024)

        out_path = self.dir_path / "truncated_output.bin"
        assembler = FileAssembler()

        with self.assertRaises(AssemblyFailedError) as ctx:
            assembler.assemble(
                state=state,
                chunk_dir=self.chunk_dir,
                output_path=out_path,
            )

        self.assertIn(1, ctx.exception.missing_chunks)
        self.assertFalse(out_path.exists())

    def test_hash_mismatch_raises_file_hash_mismatch_error(self) -> None:
        chunk_sizes = [1024, 1024]
        state, _, _ = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        out_path = self.dir_path / "mismatch_output.bin"
        assembler = FileAssembler(verify_hash=True)
        bad_hash = "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"

        with self.assertRaises(FileHashMismatchError):
            assembler.assemble(
                state=state,
                chunk_dir=self.chunk_dir,
                output_path=out_path,
                expected_hash=bad_hash,
            )

        self.assertFalse(out_path.exists())

    def test_assembly_progress_callback(self) -> None:
        chunk_sizes = [32768, 32768, 32768]
        state, _, _ = build_chunked_test_data(
            chunk_sizes=chunk_sizes,
            chunk_dir=self.chunk_dir,
        )

        out_path = self.dir_path / "progress_output.bin"
        progress_records: list[tuple[int, int]] = []

        def on_progress(current: int, total: int) -> None:
            progress_records.append((current, total))

        assembler = FileAssembler()
        assembler.assemble(
            state=state,
            chunk_dir=self.chunk_dir,
            output_path=out_path,
            on_progress=on_progress,
        )

        self.assertGreater(len(progress_records), 0)
        final_current, final_total = progress_records[-1]
        self.assertEqual(final_current, sum(chunk_sizes))
        self.assertEqual(final_total, sum(chunk_sizes))


if __name__ == "__main__":
    unittest.main()
