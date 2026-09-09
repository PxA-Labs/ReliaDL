"""
Zero-assembly direct sparse file pre-allocation and positional writer for ReliaDL.
Implements pre-allocation (posix_fallocate / Darwin F_PREALLOCATE / sparse ftruncate)
and concurrent positional writes via os.pwrite() to achieve zero-assembly 1.0x peak disk overhead.
"""

from __future__ import annotations

import enum
import errno
import hashlib
import hmac
import os
import shutil
import struct
import sys
import threading
from pathlib import Path
from typing import Any, Optional, Union

from src.exceptions import (
    AllocationError,
    DiskFullError,
    FileHashMismatchError,
    StorageError,
    StoragePermissionError,
)
from src.models import ChunkSpec

# macOS (Darwin) constant for F_PREALLOCATE
_DARWIN_F_PREALLOCATE = 42
_DARWIN_F_ALLOCATEALL = 4
_DARWIN_F_PEOFPOSMODE = 3


class AllocationStrategy(str, enum.Enum):
    """File allocation strategies for zero-assembly storage."""

    AUTO = "auto"
    FALLOCATE = "fallocate"
    SPARSE = "sparse"
    TRUNCATE = "truncate"


def _normalize_hex_hash(hash_val: str) -> str:
    """Normalize hex hash string by stripping prefixes and whitespace."""
    cleaned = hash_val.strip().lower()
    for prefix in ("sha256:", "sha512:", "sha384:", "sha1:", "md5:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break
    return cleaned


class SparseFileWriter:
    """
    Direct sparse file pre-allocator and positional writer.

    Enables multiple worker threads or tasks to write non-overlapping byte ranges
    directly into the destination file using positional I/O (os.pwrite), eliminating
    temporary chunk file staging and reducing peak disk overhead to exactly 1.0x file size.
    """

    def __init__(
        self,
        target_path: Union[str, Path],
        total_size: int,
        strategy: Union[str, AllocationStrategy] = AllocationStrategy.AUTO,
        create_dirs: bool = True,
        check_disk_space: bool = True,
    ) -> None:
        """
        Initialize and pre-allocate the target file.

        Args:
            target_path: Path to the target output file.
            total_size: Total expected size of the final file in bytes.
            strategy: Allocation strategy ('auto', 'fallocate', 'sparse', 'truncate').
            create_dirs: Automatically create parent directories if missing.
            check_disk_space: Validate sufficient free disk space before allocation.

        Raises:
            ValueError: If total_size is negative.
            DiskFullError: If available disk space is less than total_size.
            StoragePermissionError: If permissions prevent file creation or opening.
            AllocationError: If file pre-allocation fails.
        """
        if total_size < 0:
            raise ValueError(f"total_size must be non-negative, got {total_size}")

        self._target_path = Path(target_path).resolve()
        self._total_size = total_size
        self._strategy = AllocationStrategy(strategy) if isinstance(strategy, str) else strategy
        self._lock = threading.Lock()
        self._fd: Optional[int] = None
        self._is_closed = False
        self._preallocated = False
        self._written_bytes = 0

        # Check directory and disk space
        parent_dir = self._target_path.parent
        if create_dirs:
            try:
                parent_dir.mkdir(parents=True, exist_ok=True)
            except builtins_permission_error():
                raise StoragePermissionError(
                    f"Permission denied creating directory: {parent_dir}",
                    path=str(parent_dir),
                )
            except OSError as err:
                raise StorageError(
                    f"Failed to create directory {parent_dir}: {err}",
                    path=str(parent_dir),
                )

        if check_disk_space and total_size > 0:
            self._verify_disk_space(parent_dir, total_size)

        # Open file in read/write mode (creating if not existing)
        open_flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_BINARY"):
            open_flags |= os.O_BINARY

        try:
            self._fd = os.open(str(self._target_path), open_flags, 0o666)
        except (PermissionError, OSError) as err:
            if getattr(err, "errno", None) in (errno.EACCES, errno.EPERM):
                raise StoragePermissionError(
                    f"Permission denied opening target file {self._target_path}: {err}",
                    path=str(self._target_path),
                )
            raise StorageError(
                f"Failed to open target file {self._target_path}: {err}",
                path=str(self._target_path),
            )

        # Perform allocation
        self._allocate()

    @property
    def target_path(self) -> Path:
        """Destination file path."""
        return self._target_path

    @property
    def total_size(self) -> int:
        """Expected total file size in bytes."""
        return self._total_size

    @property
    def strategy(self) -> AllocationStrategy:
        """Allocation strategy utilized."""
        return self._strategy

    @property
    def is_closed(self) -> bool:
        """Whether the file descriptor is closed."""
        return self._is_closed

    @property
    def is_preallocated(self) -> bool:
        """Whether space allocation has succeeded."""
        return self._preallocated

    @property
    def peak_overhead_ratio(self) -> float:
        """
        Peak disk overhead ratio relative to final file size.
        For zero-assembly direct positional writer, overhead is exactly 1.0x
        (zero temporary chunks or assembly scratch files).
        """
        return 1.0

    @property
    def current_size(self) -> int:
        """Current logical size of the target file in bytes."""
        self._ensure_open()
        assert self._fd is not None
        return os.fstat(self._fd).st_size

    @property
    def allocated_blocks(self) -> Optional[int]:
        """Number of 512-byte blocks allocated on disk, if reported by filesystem."""
        self._ensure_open()
        assert self._fd is not None
        stat = os.fstat(self._fd)
        return getattr(stat, "st_blocks", None)

    @property
    def allocated_bytes(self) -> Optional[int]:
        """Estimated physical bytes allocated on disk, if reported by filesystem."""
        blocks = self.allocated_blocks
        return (blocks * 512) if blocks is not None else None

    def _verify_disk_space(self, directory: Path, required_bytes: int) -> None:
        """Check whether sufficient disk space is available."""
        try:
            usage = shutil.disk_usage(str(directory))
            if usage.free < required_bytes:
                raise DiskFullError(
                    f"Insufficient disk space in {directory}: available {usage.free} bytes, "
                    f"required {required_bytes} bytes",
                    available_bytes=usage.free,
                    required_bytes=required_bytes,
                    path=str(directory),
                )
        except DiskFullError:
            raise
        except OSError:
            # Filesystem usage check failure (e.g. non-standard mounts) does not abort
            pass

    def _allocate(self) -> None:
        """Allocate space according to selected strategy."""
        assert self._fd is not None
        if self._total_size == 0:
            try:
                os.ftruncate(self._fd, 0)
                self._preallocated = True
                return
            except OSError as err:
                raise AllocationError(
                    f"Failed to truncate 0-byte file {self._target_path}: {err}",
                    path=str(self._target_path),
                )

        if self._strategy == AllocationStrategy.SPARSE:
            self._allocate_sparse()
        elif self._strategy == AllocationStrategy.FALLOCATE:
            self._allocate_fallocate()
        elif self._strategy == AllocationStrategy.TRUNCATE:
            self._allocate_truncate()
        else:  # AUTO
            try:
                self._allocate_fallocate()
            except (NotImplementedError, AllocationError):
                # Fallback to sparse truncation if fallocate is unsupported on this filesystem
                self._allocate_sparse()

    def _allocate_fallocate(self) -> None:
        """Allocate physical disk blocks upfront using posix_fallocate or Darwin F_PREALLOCATE."""
        assert self._fd is not None
        # 1. POSIX fallocate (Linux, FreeBSD, etc.)
        if hasattr(os, "posix_fallocate"):
            try:
                os.posix_fallocate(self._fd, 0, self._total_size)
                self._preallocated = True
                return
            except OSError as err:
                if err.errno == errno.ENOSPC:
                    raise DiskFullError(
                        f"Insufficient disk space pre-allocating {self._total_size} bytes: {err}",
                        required_bytes=self._total_size,
                        path=str(self._target_path),
                    )
                if err.errno in (errno.EOPNOTSUPP, errno.ENOTSUP, errno.EINVAL):
                    raise AllocationError(
                        f"posix_fallocate not supported on filesystem: {err}",
                        path=str(self._target_path),
                    )
                raise AllocationError(
                    f"posix_fallocate failed on {self._target_path}: {err}",
                    path=str(self._target_path),
                )

        # 2. macOS Darwin F_PREALLOCATE
        if sys.platform == "darwin":
            try:
                import fcntl
                # struct fstore: u_int32_t fst_flags, int fst_posmode, off_t fst_offset, length, bytesalloc
                fstore = struct.pack(
                    "IIqqq",
                    _DARWIN_F_ALLOCATEALL,
                    _DARWIN_F_PEOFPOSMODE,
                    0,
                    self._total_size,
                    0,
                )
                fcntl.fcntl(self._fd, _DARWIN_F_PREALLOCATE, fstore)
                os.ftruncate(self._fd, self._total_size)
                self._preallocated = True
                return
            except OSError as err:
                if getattr(err, "errno", None) == errno.ENOSPC:
                    raise DiskFullError(
                        f"Insufficient disk space on macOS pre-allocating {self._total_size} bytes: {err}",
                        required_bytes=self._total_size,
                        path=str(self._target_path),
                    )
                # Fall back to truncate if preallocate fails
                try:
                    os.ftruncate(self._fd, self._total_size)
                    self._preallocated = True
                    return
                except OSError as trunc_err:
                    raise AllocationError(
                        f"Darwin pre-allocation and fallback truncate failed: {trunc_err}",
                        path=str(self._target_path),
                    )

        # 3. Default fallback for other platforms
        raise NotImplementedError("Physical pre-allocation not supported natively on this platform")

    def _allocate_sparse(self) -> None:
        """Create a sparse file of the target size without pre-allocating physical blocks."""
        assert self._fd is not None
        try:
            os.ftruncate(self._fd, self._total_size)
            self._preallocated = True
        except OSError as err:
            if err.errno == errno.ENOSPC:
                raise DiskFullError(
                    f"Insufficient disk space creating sparse file: {err}",
                    required_bytes=self._total_size,
                    path=str(self._target_path),
                )
            raise AllocationError(
                f"Sparse allocation failed on {self._target_path}: {err}",
                path=str(self._target_path),
            )

    def _allocate_truncate(self) -> None:
        """Explicit truncation allocation."""
        self._allocate_sparse()

    def _ensure_open(self) -> None:
        """Ensure the writer is open and valid."""
        if self._is_closed or self._fd is None:
            raise StorageError(
                "SparseFileWriter is closed",
                path=str(self._target_path),
            )

    def write_at(
        self,
        offset: int,
        data: Union[bytes, bytearray, memoryview],
    ) -> int:
        """
        Write data to the target file at the specified byte offset.

        Thread-safe: non-overlapping writes can be executed concurrently by multiple
        worker threads without interfering with each other's positions.

        Args:
            offset: Absolute start byte offset in the destination file.
            data: Binary payload to write.

        Returns:
            Number of bytes written.

        Raises:
            StorageError: If the writer is closed or bounds are exceeded.
            DiskFullError: If the disk runs out of space during writing.
        """
        self._ensure_open()
        assert self._fd is not None

        if offset < 0:
            raise StorageError(
                f"Negative offset {offset} is not allowed",
                path=str(self._target_path),
            )

        length = len(data)
        if length == 0:
            return 0

        end_offset = offset + length
        if end_offset > self._total_size:
            raise StorageError(
                f"Write out of bounds: offset {offset} + {length} bytes exceeds total size {self._total_size}",
                path=str(self._target_path),
            )

        # Positional write via os.pwrite
        if hasattr(os, "pwrite"):
            bytes_written = 0
            view = memoryview(data)
            while bytes_written < length:
                try:
                    chunk_written = os.pwrite(
                        self._fd,
                        view[bytes_written:],
                        offset + bytes_written,
                    )
                    if chunk_written == 0:
                        raise StorageError(
                            f"Zero bytes written at offset {offset + bytes_written}",
                            path=str(self._target_path),
                        )
                    bytes_written += chunk_written
                except OSError as err:
                    if err.errno == errno.ENOSPC:
                        raise DiskFullError(
                            f"Disk full while writing at offset {offset}: {err}",
                            path=str(self._target_path),
                        )
                    raise StorageError(
                        f"pwrite failed at offset {offset}: {err}",
                        path=str(self._target_path),
                    )
        else:
            # Fallback for systems lacking os.pwrite: locked seek + write
            with self._lock:
                try:
                    os.lseek(self._fd, offset, os.SEEK_SET)
                    bytes_written = 0
                    view = memoryview(data)
                    while bytes_written < length:
                        chunk_written = os.write(self._fd, view[bytes_written:])
                        if chunk_written == 0:
                            raise StorageError(
                                f"Zero bytes written at offset {offset + bytes_written}",
                                path=str(self._target_path),
                            )
                        bytes_written += chunk_written
                except OSError as err:
                    if err.errno == errno.ENOSPC:
                        raise DiskFullError(
                            f"Disk full while writing at offset {offset}: {err}",
                            path=str(self._target_path),
                        )
                    raise StorageError(
                        f"seek/write failed at offset {offset}: {err}",
                        path=str(self._target_path),
                    )

        with self._lock:
            self._written_bytes += bytes_written

        return bytes_written

    def write_chunk(
        self,
        chunk: Union[ChunkSpec, int],
        data: Union[bytes, bytearray, memoryview],
        offset: Optional[int] = None,
    ) -> int:
        """
        Write a chunk to the file either via ChunkSpec or explicit offset.

        Args:
            chunk: ChunkSpec object or chunk index.
            data: Binary payload.
            offset: Optional explicit start offset if chunk is an integer index.

        Returns:
            Number of bytes written.
        """
        if isinstance(chunk, ChunkSpec):
            if len(data) != chunk.size:
                raise StorageError(
                    f"Chunk payload size ({len(data)}) does not match ChunkSpec size ({chunk.size})",
                    path=str(self._target_path),
                )
            target_offset = chunk.start_byte
        else:
            if offset is None:
                raise ValueError("Explicit offset must be provided when chunk is specified by index")
            target_offset = offset

        return self.write_at(target_offset, data)

    def read_at(self, offset: int, length: int) -> bytes:
        """
        Read length bytes starting at the specified offset without altering file pointer.

        Args:
            offset: Byte offset to read from.
            length: Number of bytes to read.

        Returns:
            Bytes read.
        """
        self._ensure_open()
        assert self._fd is not None

        if offset < 0 or length < 0:
            raise StorageError(
                f"Invalid read parameters: offset={offset}, length={length}",
                path=str(self._target_path),
            )

        if length == 0:
            return b""

        if hasattr(os, "pread"):
            try:
                return os.pread(self._fd, length, offset)
            except OSError as err:
                raise StorageError(
                    f"pread failed at offset {offset}: {err}",
                    path=str(self._target_path),
                )
        else:
            with self._lock:
                try:
                    os.lseek(self._fd, offset, os.SEEK_SET)
                    result = bytearray()
                    while len(result) < length:
                        chunk = os.read(self._fd, length - len(result))
                        if not chunk:
                            break
                        result.extend(chunk)
                    return bytes(result)
                except OSError as err:
                    raise StorageError(
                        f"seek/read failed at offset {offset}: {err}",
                        path=str(self._target_path),
                    )

    def sync(self) -> None:
        """Flush and synchronize all file data and metadata to permanent storage."""
        self._ensure_open()
        assert self._fd is not None
        try:
            os.fsync(self._fd)
        except OSError as err:
            raise StorageError(
                f"fsync failed on {self._target_path}: {err}",
                path=str(self._target_path),
            )

    def verify_hash(
        self,
        expected_hash: str,
        algorithm: str = "sha256",
        buffer_size: int = 64 * 1024,
    ) -> bool:
        """
        Verify the whole target file against an expected cryptographic hash.

        Args:
            expected_hash: Expected hex hash string.
            algorithm: Hash algorithm name (default: 'sha256').
            buffer_size: Read buffer size in bytes (default: 64 KB).

        Returns:
            True if hash matches.

        Raises:
            FileHashMismatchError: If the computed hash does not match expected_hash.
        """
        self._ensure_open()
        self.sync()

        hasher = hashlib.new(algorithm)
        offset = 0

        while offset < self._total_size:
            read_len = min(buffer_size, self._total_size - offset)
            chunk = self.read_at(offset, read_len)
            if not chunk:
                break
            hasher.update(chunk)
            offset += len(chunk)

        computed = hasher.hexdigest().lower()
        normalized_expected = _normalize_hex_hash(expected_hash)

        if not hmac.compare_digest(computed, normalized_expected):
            raise FileHashMismatchError(
                f"Whole-file hash mismatch for {self._target_path}: "
                f"expected {normalized_expected}, got {computed}",
                file_path=str(self._target_path),
                expected_hash=normalized_expected,
                computed_hash=computed,
            )

        return True

    def close(self) -> None:
        """Synchronize and close the file descriptor."""
        with self._lock:
            if self._is_closed:
                return
            self._is_closed = True
            if self._fd is not None:
                try:
                    os.fsync(self._fd)
                except OSError:
                    pass
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None

    def __enter__(self) -> "SparseFileWriter":
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()


def builtins_permission_error() -> type[BaseException]:
    """Helper to retrieve builtins PermissionError for except clauses."""
    return PermissionError
