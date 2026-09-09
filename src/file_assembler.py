"""
Sequential chunk file assembler and integrity re-verifier for ReliaDL.
Implements sequential chunk concatenation, progressive whole-file SHA-256 calculation,
atomic target replacement, and intermediate chunk cleanup.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import shutil
from pathlib import Path
from typing import Callable, Optional, Union

from src.exceptions import (
    AssemblyFailedError,
    FileHashMismatchError,
    StorageError,
)
from src.models import (
    ChunkState,
    ChunkStatus,
    DownloadState,
    DownloadStatus,
)

DEFAULT_ASSEMBLY_BUFFER_SIZE = 64 * 1024  # 64 KB


def _normalize_hex_hash(hash_val: str) -> str:
    """Normalize hex hash by lowercasing and stripping prefixes."""
    cleaned = hash_val.strip().lower()
    for pfx in ("sha256:", "sha512:", "sha384:"):
        if cleaned.startswith(pfx):
            cleaned = cleaned[len(pfx):]
            break
    return cleaned


class FileAssembler:
    """
    Concatenates staged chunk files into final output file with streaming hash validation.
    """

    def __init__(
        self,
        buffer_size: int = DEFAULT_ASSEMBLY_BUFFER_SIZE,
        cleanup_chunks: bool = True,
        verify_hash: bool = True,
    ) -> None:
        self.buffer_size = buffer_size
        self.cleanup_chunks = cleanup_chunks
        self.verify_hash = verify_hash

    def assemble(
        self,
        state: DownloadState,
        chunk_dir: Union[str, Path],
        output_path: Optional[Union[str, Path]] = None,
        expected_hash: Optional[str] = None,
        on_progress: Optional[Callable[[int, int], None]] = None,
    ) -> Path:
        """
        Sequentially concatenate verified chunk files to target output path.

        Args:
            state: DownloadState containing chunk specifications and metadata
            chunk_dir: Path to directory containing staged chunk files
            output_path: Destination path (defaults to state.output_path)
            expected_hash: Expected whole-file SHA-256 hash for verification
            on_progress: Optional callback invoked with (bytes_assembled, total_bytes)

        Returns:
            Path to final assembled file on disk.

        Raises:
            AssemblyFailedError: If chunk files are missing or incomplete.
            FileHashMismatchError: If assembled file hash does not match expected hash.
            StorageError: If disk write or filesystem operations fail.
        """
        c_dir = Path(chunk_dir).resolve()
        dest_path = Path(output_path or state.output_path or state.target_path).resolve()
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        exp_hash = expected_hash or state.expected_file_hash
        sorted_chunks = sorted(state.chunks, key=lambda c: c.index)

        # 1. Pre-validation: ensure all chunks have files on disk with expected sizes
        missing_chunks: list[int] = []
        chunk_paths: list[Path] = []

        for chunk in sorted_chunks:
            # Check specified file_path or standard chunk_{index}.part / chunk_{index}.chunk
            potential_paths = []
            if chunk.file_path:
                potential_paths.append(Path(chunk.file_path))
            potential_paths.append(c_dir / f"chunk_{chunk.index}.part")
            potential_paths.append(c_dir / f"chunk_{chunk.index}.chunk")
            potential_paths.append(c_dir / f"{chunk.index}.part")

            found_path: Optional[Path] = None
            for p in potential_paths:
                if p.is_file():
                    found_path = p
                    break

            if not found_path:
                missing_chunks.append(chunk.index)
            else:
                chunk_paths.append(found_path)

        if missing_chunks:
            raise AssemblyFailedError(
                f"Cannot assemble file '{dest_path.name}': missing {len(missing_chunks)} chunk files "
                f"for chunk indices {missing_chunks}",
                missing_chunks=missing_chunks,
                reason="Missing chunk files on disk",
            )

        # 2. Sequential write to temporary assembling file
        tmp_target = dest_path.parent / f"{dest_path.name}.assembling.{os.getpid()}"
        hasher = hashlib.sha256()
        total_assembled: int = 0
        total_size = state.file_size or sum(c.size for c in sorted_chunks)

        try:
            with open(tmp_target, "wb") as out_f:
                for chunk, c_path in zip(sorted_chunks, chunk_paths):
                    file_size = c_path.stat().st_size
                    if file_size != chunk.size:
                        raise AssemblyFailedError(
                            f"Chunk {chunk.index} file size mismatch: expected {chunk.size} bytes, "
                            f"found {file_size} bytes in {c_path.name}",
                            missing_chunks=[chunk.index],
                            reason="Truncated or corrupt chunk file",
                        )

                    with open(c_path, "rb") as in_f:
                        while True:
                            buf = in_f.read(self.buffer_size)
                            if not buf:
                                break
                            hasher.update(buf)
                            out_f.write(buf)
                            total_assembled += len(buf)
                            if on_progress:
                                on_progress(total_assembled, total_size)

                out_f.flush()
                os.fsync(out_f.fileno())

            computed_hash = hasher.hexdigest()

            # 3. Whole-file cryptographic integrity verification
            if self.verify_hash and exp_hash:
                normalized_expected = _normalize_hex_hash(exp_hash)
                if not hmac.compare_digest(computed_hash, normalized_expected):
                    if tmp_target.exists():
                        try:
                            tmp_target.unlink()
                        except OSError:
                            pass
                    raise FileHashMismatchError(
                        f"Assembled file integrity verification failed for '{dest_path.name}': "
                        f"expected {normalized_expected}, computed {computed_hash}",
                        file_path=str(dest_path),
                        expected_hash=normalized_expected,
                        computed_hash=computed_hash,
                    )

            # 4. Atomic replacement into final destination
            os.replace(tmp_target, dest_path)

            # 5. Cleanup intermediate chunk files if requested
            if self.cleanup_chunks:
                for c_path in chunk_paths:
                    try:
                        c_path.unlink(missing_ok=True)
                    except OSError:
                        pass
                # If chunk directory is now empty, remove it
                try:
                    c_dir.rmdir()
                except OSError:
                    pass

            return dest_path

        except (OSError, AssemblyFailedError, FileHashMismatchError) as e:
            if tmp_target.exists():
                try:
                    tmp_target.unlink()
                except OSError:
                    pass
            if isinstance(e, (AssemblyFailedError, FileHashMismatchError)):
                raise
            raise StorageError(
                f"Storage error while assembling {dest_path.name}: {e}",
                path=str(dest_path),
                cause=e,
            ) from e
