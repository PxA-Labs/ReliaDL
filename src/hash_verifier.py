"""
Streaming cryptographic hash verification subsystem for ReliaDL.
Implements incremental 64 KB buffer streaming verification, constant-time
digest comparison via hmac.compare_digest, and NIST compliance.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
from pathlib import Path
from typing import BinaryIO, Callable, Iterable, Optional, Union

from src.exceptions import (
    ConfigurationError,
    FileHashMismatchError,
    ReliaDLError,
    StorageError,
)
from src.models import VerificationResult

# Default streaming buffer chunk size (64 KB)
DEFAULT_BUFFER_SIZE = 64 * 1024

# Supported secure cryptographic hash algorithms
SUPPORTED_ALGORITHMS = {"sha256", "sha384", "sha512"}

# Insecure algorithms disallowed by security policy
DISALLOWED_ALGORITHMS = {"md5", "sha1"}


def normalize_hash(hash_str: str) -> str:
    """
    Normalize cryptographic hash string.
    Strips leading/trailing whitespace, converts to lowercase,
    and removes optional algorithm prefix (e.g., 'sha256:abcdef' -> 'abcdef').

    Raises:
        ConfigurationError: If hash string contains invalid characters.
    """
    if not isinstance(hash_str, str):
        raise ConfigurationError(
            f"Expected string hash, got {type(hash_str).__name__}",
            parameter="expected_hash",
            value=hash_str,
        )

    cleaned = hash_str.strip().lower()
    for prefix in ("sha256:", "sha384:", "sha512:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
            break

    if not cleaned or not all(c in "0123456789abcdef" for c in cleaned):
        raise ConfigurationError(
            f"Invalid hexadecimal hash string: '{hash_str}'",
            parameter="expected_hash",
            value=hash_str,
        )

    return cleaned


def constant_time_compare(hash_a: str, hash_b: str) -> bool:
    """
    Compare two cryptographic hash digests in constant time using hmac.compare_digest.
    Mitigates timing analysis and side-channel vulnerability attacks.
    """
    try:
        norm_a = normalize_hash(hash_a)
        norm_b = normalize_hash(hash_b)
    except ConfigurationError:
        return False
    return hmac.compare_digest(norm_a, norm_b)


class StreamingHashVerifier:
    """
    Incremental cryptographic stream verifier.

    Processes streaming byte chunks using a bounded memory footprint
    and provides constant-time comparison against expected digests.
    """

    def __init__(
        self,
        algorithm: str = "sha256",
        buffer_size: int = DEFAULT_BUFFER_SIZE,
    ) -> None:
        clean_algo = algorithm.strip().lower()
        if clean_algo in DISALLOWED_ALGORITHMS:
            raise ConfigurationError(
                f"Cryptographic algorithm '{algorithm}' is disallowed by security policy. Use sha256 or sha512.",
                parameter="algorithm",
                value=algorithm,
            )
        if clean_algo not in SUPPORTED_ALGORITHMS:
            raise ConfigurationError(
                f"Unsupported cryptographic algorithm '{algorithm}'. Supported: {sorted(SUPPORTED_ALGORITHMS)}",
                parameter="algorithm",
                value=algorithm,
            )
        if buffer_size <= 0:
            raise ConfigurationError(
                f"Buffer size must be positive integer, got {buffer_size}",
                parameter="buffer_size",
                value=buffer_size,
            )

        self.algorithm = clean_algo
        self.buffer_size = buffer_size
        self._hasher = hashlib.new(self.algorithm)
        self._bytes_processed: int = 0

    @property
    def bytes_processed(self) -> int:
        """Total number of bytes fed into the verifier so far."""
        return self._bytes_processed

    def update(self, chunk: Union[bytes, bytearray, memoryview]) -> None:
        """
        Feed an incremental byte chunk into the running hash calculation.
        """
        self._hasher.update(chunk)
        self._bytes_processed += len(chunk)

    def hexdigest(self) -> str:
        """Return the current hexadecimal hash digest."""
        return self._hasher.hexdigest()

    def digest(self) -> bytes:
        """Return the current raw binary hash digest."""
        return self._hasher.digest()

    def verify(self, expected_hash: str) -> bool:
        """
        Verify the computed digest against expected hash using constant-time comparison.
        """
        return constant_time_compare(self.hexdigest(), expected_hash)

    def reset(self) -> None:
        """Reset the internal hasher state to process a new stream."""
        self._hasher = hashlib.new(self.algorithm)
        self._bytes_processed = 0


def compute_file_hash(
    file_path: Union[str, Path],
    algorithm: str = "sha256",
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> str:
    """
    Stream and compute cryptographic hash for a file on disk.

    Guarantees strictly bounded memory consumption by reading in fixed-size buffers.

    Args:
        file_path: Path to target file on disk
        algorithm: Hash algorithm ('sha256', 'sha384', 'sha512')
        buffer_size: Size of incremental read buffer in bytes (default 64 KB)
        on_progress: Optional callback invoked with (bytes_read, total_file_size)

    Returns:
        Hexadecimal hash string
    """
    path_obj = Path(file_path)
    if not path_obj.is_file():
        raise StorageError(
            f"Target file for hashing does not exist: {file_path}",
            path=str(file_path),
        )

    total_size = path_obj.stat().st_size
    verifier = StreamingHashVerifier(algorithm=algorithm, buffer_size=buffer_size)

    try:
        with open(path_obj, "rb") as f:
            while True:
                chunk = f.read(buffer_size)
                if not chunk:
                    break
                verifier.update(chunk)
                if on_progress:
                    on_progress(verifier.bytes_processed, total_size)
    except OSError as e:
        raise StorageError(
            f"Failed to read file during hash computation: {e}",
            path=str(file_path),
            cause=e,
        ) from e

    return verifier.hexdigest()


def verify_file_hash(
    file_path: Union[str, Path],
    expected_hash: str,
    algorithm: str = "sha256",
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    raise_on_mismatch: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> VerificationResult:
    """
    Verify a file's cryptographic hash against an expected value.

    Args:
        file_path: Path to target file
        expected_hash: Expected cryptographic hash
        algorithm: Hash algorithm (default 'sha256')
        buffer_size: Read buffer size (default 64 KB)
        raise_on_mismatch: If True, raises FileHashMismatchError when verification fails
        on_progress: Optional progress callback

    Returns:
        VerificationResult model with match status, timing, and byte metrics.
    """
    path_obj = Path(file_path)
    normalized_expected = normalize_hash(expected_hash)

    start_time = time.perf_counter()
    computed = compute_file_hash(
        file_path=path_obj,
        algorithm=algorithm,
        buffer_size=buffer_size,
        on_progress=on_progress,
    )
    elapsed = time.perf_counter() - start_time

    is_valid = constant_time_compare(computed, normalized_expected)
    total_bytes = path_obj.stat().st_size

    if not is_valid and raise_on_mismatch:
        raise FileHashMismatchError(
            f"File integrity check failed for {path_obj.name}: expected {normalized_expected}, computed {computed}",
            file_path=str(path_obj),
            expected_hash=normalized_expected,
            computed_hash=computed,
        )

    return VerificationResult(
        file_path=path_obj,
        expected_hash=normalized_expected,
        computed_hash=computed,
        is_valid=is_valid,
        bytes_verified=total_bytes,
        duration_seconds=elapsed,
        algorithm=algorithm.lower(),
    )


def verify_stream(
    stream: Union[BinaryIO, Iterable[bytes]],
    expected_hash: str,
    algorithm: str = "sha256",
    buffer_size: int = DEFAULT_BUFFER_SIZE,
) -> tuple[bool, str, int]:
    """
    Verify streaming byte chunks or file-like object against expected hash.

    Returns:
        Tuple of (is_valid, computed_hexdigest, total_bytes_processed)
    """
    verifier = StreamingHashVerifier(algorithm=algorithm, buffer_size=buffer_size)

    if hasattr(stream, "read"):
        # File-like object
        while True:
            chunk = stream.read(buffer_size)  # type: ignore[union-attr]
            if not chunk:
                break
            verifier.update(chunk)
    else:
        # Iterable of byte chunks
        for chunk in stream:
            verifier.update(chunk)

    computed = verifier.hexdigest()
    is_valid = verifier.verify(expected_hash)
    return is_valid, computed, verifier.bytes_processed


async def async_verify_file_hash(
    file_path: Union[str, Path],
    expected_hash: str,
    algorithm: str = "sha256",
    buffer_size: int = DEFAULT_BUFFER_SIZE,
    raise_on_mismatch: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
) -> VerificationResult:
    """
    Asynchronously verify file hash in a background worker thread
    to prevent blocking the asyncio event loop.
    """
    return await asyncio.to_thread(
        verify_file_hash,
        file_path,
        expected_hash,
        algorithm=algorithm,
        buffer_size=buffer_size,
        raise_on_mismatch=raise_on_mismatch,
        on_progress=on_progress,
    )
