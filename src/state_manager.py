"""
Crash-resilient atomic state persistence manager for ReliaDL.
Implements write-flush-fsync-replace protocol, permission enforcement (0600),
automated backup rotation, corrupted state recovery, and on-disk chunk reconciliation.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from pathlib import Path
from typing import Any, Optional, Union

from src.exceptions import (
    StateCorruptedError,
    StateError,
    StateNotFoundError,
    StorageError,
)
from src.models import (
    ChunkState,
    ChunkStatus,
    DownloadState,
    DownloadStatus,
)

# Owner read/write only (POSIX 0600)
STATE_FILE_PERMISSIONS = 0o600
CHUNK_FILE_PERMISSIONS = 0o600


class StateManager:
    """
    Manages atomic persistence and crash recovery of download session states.
    """

    def __init__(
        self,
        default_state_dir: str = ".ReliaDL",
        backup_enabled: bool = True,
    ) -> None:
        self.default_state_dir = default_state_dir
        self.backup_enabled = backup_enabled

    def get_state_path(
        self,
        target_path: Union[str, Path],
        state_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        """
        Compute standardized state file path for a target download destination.
        Example: /downloads/data.iso -> /downloads/.ReliaDL/data.iso.state
        """
        target = Path(target_path).resolve()
        parent_dir = target.parent
        s_dir = Path(state_dir) if state_dir else parent_dir / self.default_state_dir
        return s_dir / f"{target.name}.state"

    def get_chunk_dir(
        self,
        target_path: Union[str, Path],
        state_dir: Optional[Union[str, Path]] = None,
    ) -> Path:
        """
        Compute directory path for staged download chunks.
        Example: /downloads/data.iso -> /downloads/.ReliaDL/data.iso.chunks/
        """
        target = Path(target_path).resolve()
        parent_dir = target.parent
        s_dir = Path(state_dir) if state_dir else parent_dir / self.default_state_dir
        return s_dir / f"{target.name}.chunks"

    def save(
        self,
        state: DownloadState,
        state_path: Optional[Union[str, Path]] = None,
    ) -> Path:
        """
        Atomically persist DownloadState to disk using write-fsync-replace protocol.

        Protocol:
        1. Write state JSON to temporary file (.state.tmp.<pid>)
        2. Flush python buffer and execute os.fsync(fd) to commit physical disk blocks
        3. Enforce 0600 permissions
        4. If existing state file exists, rotate to .state.bak
        5. Atomically replace temp file into target .state path
        6. fsync parent directory to guarantee metadata persistence
        """
        if state_path is None:
            state_path = self.get_state_path(state.target_path)
        path = Path(state_path).resolve()

        # Ensure destination directory exists
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

        # Update timestamp before serializing
        state.update_timestamp()
        json_data = state.to_json(indent=2)

        tmp_path = path.parent / f"{path.name}.tmp.{os.getpid()}"
        bak_path = path.parent / f"{path.name}.bak"

        # 1. Write to temp file with fsync
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json_data)
                f.flush()
                os.fsync(f.fileno())

            # 2. Enforce mode 0600
            try:
                os.chmod(tmp_path, STATE_FILE_PERMISSIONS)
            except OSError:
                pass

            # 3. Create backup if primary state currently exists
            if self.backup_enabled and path.is_file():
                try:
                    shutil.copy2(path, bak_path)
                    try:
                        os.chmod(bak_path, STATE_FILE_PERMISSIONS)
                    except OSError:
                        pass
                except OSError:
                    pass

            # 4. Atomically replace temp file into target path
            try:
                os.replace(tmp_path, path)
            except OSError as e:
                # Fall back to write-in-place if cross-device or atomic replace fails
                with open(path, "w", encoding="utf-8") as f:
                    f.write(json_data)
                    f.flush()
                    os.fsync(f.fileno())

            # 5. Enforce 0600 on final destination
            try:
                os.chmod(path, STATE_FILE_PERMISSIONS)
            except OSError:
                pass

            # 6. Fsync parent directory
            try:
                dir_fd = os.open(str(path.parent), os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except OSError:
                pass

            return path

        except OSError as e:
            raise StorageError(
                f"Failed to persist state file {path}: {e}",
                path=str(path),
                cause=e,
            ) from e
        finally:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass

    def load(
        self,
        state_path: Union[str, Path],
        auto_recover: bool = True,
    ) -> DownloadState:
        """
        Load and parse DownloadState from disk with crash recovery support.

        If the primary state file is corrupted or partially written, attempts
        recovery from the .bak backup file.
        """
        path = Path(state_path).resolve()
        bak_path = path.parent / f"{path.name}.bak"

        # Check if primary exists
        if not path.is_file():
            if auto_recover and bak_path.is_file():
                # Attempt restore from backup
                try:
                    return self._load_file(bak_path)
                except StateCorruptedError:
                    pass
            raise StateNotFoundError(
                f"State file does not exist: {path}",
                state_file=str(path),
            )

        # Attempt to load primary state file
        try:
            return self._load_file(path)
        except StateCorruptedError as primary_err:
            if auto_recover and bak_path.is_file():
                try:
                    state = self._load_file(bak_path)
                    # Restore primary from backup
                    self.save(state, state_path=path)
                    return state
                except Exception:
                    pass
            raise primary_err

    def _load_file(self, file_path: Path) -> DownloadState:
        """Helper to read and parse a single state file."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
        except OSError as e:
            raise StorageError(
                f"Failed to read state file {file_path}: {e}",
                path=str(file_path),
                cause=e,
            ) from e

        if not content:
            raise StateCorruptedError(
                f"State file is empty: {file_path}",
                state_file=str(file_path),
                details="Empty payload",
            )

        try:
            return DownloadState.from_json(content)
        except (json.JSONDecodeError, ValueError, KeyError) as e:
            raise StateCorruptedError(
                f"State file corrupted or invalid JSON in {file_path}: {e}",
                state_file=str(file_path),
                details=str(e),
            ) from e

    def exists(self, state_path: Union[str, Path]) -> bool:
        """True if state file or backup file exists."""
        p = Path(state_path).resolve()
        bak = p.parent / f"{p.name}.bak"
        return p.is_file() or bak.is_file()

    def delete(
        self,
        state_path: Union[str, Path],
        delete_backup: bool = True,
    ) -> None:
        """Remove state file and optional backup file."""
        p = Path(state_path).resolve()
        if p.is_file():
            try:
                p.unlink()
            except OSError:
                pass
        if delete_backup:
            bak = p.parent / f"{p.name}.bak"
            if bak.is_file():
                try:
                    bak.unlink()
                except OSError:
                    pass

    def reconcile_inventory(
        self,
        state: DownloadState,
        chunk_dir: Union[str, Path],
    ) -> DownloadState:
        """
        Reconcile DownloadState chunk status with actual on-disk chunk inventory.

        Rules:
        - If chunk is marked COMPLETE in state, but file on disk is missing or truncated,
          mark status FAILED so it will be re-downloaded.
        - If chunk is marked COMPLETE and matches expected size on disk, keep COMPLETE.
        - If chunk was IN_PROGRESS when process crashed, check file on disk:
          if complete size, verify and transition or leave for verification;
          otherwise reset to PENDING.
        """
        c_dir = Path(chunk_dir).resolve()
        updated_chunks: list[ChunkState] = []

        for chunk in state.chunks:
            chunk_file = c_dir / f"chunk_{chunk.index}.part"
            expected_size = chunk.size

            if chunk.status in (ChunkStatus.COMPLETE, ChunkStatus.COMPLETED):
                if not chunk_file.is_file():
                    # File missing -> mark failed
                    chunk.status = ChunkStatus.FAILED
                    chunk.hash_verified = False
                    chunk.error = "Chunk file missing on disk"
                elif chunk_file.stat().st_size != expected_size:
                    # Size truncated -> mark failed
                    chunk.status = ChunkStatus.FAILED
                    chunk.hash_verified = False
                    chunk.error = (
                        f"Chunk size mismatch: expected {expected_size} bytes, "
                        f"got {chunk_file.stat().st_size} bytes"
                    )
                else:
                    chunk.file_path = str(chunk_file)
            elif chunk.status == ChunkStatus.IN_PROGRESS:
                # Crashed during download
                if chunk_file.is_file() and chunk_file.stat().st_size == expected_size:
                    chunk.status = ChunkStatus.VERIFYING
                    chunk.file_path = str(chunk_file)
                else:
                    chunk.status = ChunkStatus.PENDING
                    chunk.file_path = None

            updated_chunks.append(chunk)

        state.chunks = updated_chunks
        return state
