"""
Structured custom exception taxonomy and error models for ReliaDL.
Implements a hierarchical exception classification with retryability predicates,
contextual debugging payloads, and serialization.
"""

from __future__ import annotations

import builtins
from datetime import datetime, timezone
from typing import Any, Optional


class ReliaDLError(Exception):
    """
    Root base exception for all ChunkGuard / ReliaDL operational errors.

    Provides structured context, retryability classification, and JSON serialization.
    """

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        context: Optional[dict[str, Any]] = None,
        is_retryable: Optional[bool] = None,
        cause: Optional[Exception] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context.copy() if context else {}
        self._is_retryable = is_retryable if is_retryable is not None else self.default_retryable
        self.timestamp: datetime = datetime.now(timezone.utc)
        self.cause = cause

    @property
    def is_retryable(self) -> bool:
        """Indicates whether this error is considered transient and safe for automatic retry."""
        return self._is_retryable

    def to_dict(self) -> dict[str, Any]:
        """Convert the structured exception into a dictionary for logging and serialization."""
        return {
            "error_type": self.__class__.__name__,
            "message": self.message,
            "context": self.context,
            "is_retryable": self.is_retryable,
            "timestamp": self.timestamp.isoformat(),
        }

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(message={self.message!r}, is_retryable={self.is_retryable}, context={self.context!r})"


# Alias for backward compatibility with ChunkGuard specifications
ChunkGuardError = ReliaDLError


# ─────────────────────────────────────────────────────────────────────────────
# Configuration Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class ConfigurationError(ReliaDLError):
    """Raised when configuration values, arguments, or environment variables are invalid."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        parameter: Optional[str] = None,
        value: Any = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if parameter is not None:
            ctx["parameter"] = parameter
        if value is not None:
            ctx["value"] = value
        self.parameter = parameter
        self.value = value
        super().__init__(message, context=ctx, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Network Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class NetworkError(ReliaDLError):
    """Base exception for all transient and permanent network transport failures."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        url: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if url is not None:
            ctx["url"] = url
        self.url = url
        super().__init__(message, context=ctx, **kwargs)


class ConnectionError(NetworkError, builtins.ConnectionError):
    """Raised when TCP socket establishment or DNS resolution fails."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        host: Optional[str] = None,
        port: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if host is not None:
            ctx["host"] = host
        if port is not None:
            ctx["port"] = port
        self.host = host
        self.port = port
        super().__init__(message, context=ctx, **kwargs)


class TimeoutError(NetworkError, builtins.TimeoutError):
    """Raised when a connection attempt or chunk data transfer exceeds configured deadline."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        timeout_seconds: Optional[float] = None,
        phase: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if timeout_seconds is not None:
            ctx["timeout_seconds"] = timeout_seconds
        if phase is not None:
            ctx["phase"] = phase
        self.timeout_seconds = timeout_seconds
        self.phase = phase
        super().__init__(message, context=ctx, **kwargs)


class HTTPError(NetworkError):
    """Base exception for non-2xx HTTP responses."""

    def __init__(
        self,
        message: str,
        status_code: Optional[int] = None,
        response_headers: Optional[dict[str, str]] = None,
        body: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        is_retryable: Optional[bool] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if status_code is not None:
            ctx["status_code"] = status_code
        if response_headers is not None:
            ctx["response_headers"] = response_headers
        if body is not None:
            ctx["body"] = body

        self.status_code = status_code
        self.response_headers = response_headers or {}
        self.body = body

        # Determine default retryability based on HTTP status code if not explicitly given
        if is_retryable is None:
            if status_code is not None:
                # 408 (Request Timeout) and 429 (Too Many Requests) or 5xx are retryable
                is_retryable = status_code in (408, 429) or (500 <= status_code <= 599)
            else:
                is_retryable = self.default_retryable

        super().__init__(message, context=ctx, is_retryable=is_retryable, **kwargs)


class ClientError(HTTPError):
    """Raised for 4xx HTTP client errors. Generally non-retryable unless rate-limited."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        retry_after: Optional[float] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if retry_after is not None:
            ctx["retry_after"] = retry_after
        self.retry_after = retry_after
        super().__init__(message, context=ctx, **kwargs)


class ServerError(HTTPError):
    """Raised for 5xx HTTP upstream server errors. Typically retryable with backoff."""

    default_retryable: bool = True


class PreconditionFailedError(ClientError):
    """Raised for HTTP 412 when upstream file has changed (ETag / Last-Modified mismatch)."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        etag: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if etag is not None:
            ctx["etag"] = etag
        self.etag = etag
        super().__init__(message, status_code=412, is_retryable=False, context=ctx, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Proxy Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class ProxyError(ReliaDLError):
    """Base exception for forward proxy tunneling and negotiation failures."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        proxy_url: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if proxy_url is not None:
            ctx["proxy_url"] = proxy_url
        self.proxy_url = proxy_url
        super().__init__(message, context=ctx, **kwargs)


class ProxyConnectionError(ProxyError):
    """Raised when unable to establish connection to the proxy server."""

    default_retryable: bool = True


class ProxyAuthenticationError(ProxyError):
    """Raised on HTTP 407 when proxy authentication credentials fail."""

    default_retryable: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# Manifest Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class ManifestError(ReliaDLError):
    """Base exception for .cgmanifest manifest parsing and verification failures."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        manifest_path: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if manifest_path is not None:
            ctx["manifest_path"] = manifest_path
        self.manifest_path = manifest_path
        super().__init__(message, context=ctx, **kwargs)


class ManifestSignatureMismatchError(ManifestError):
    """Raised when cryptographic signature validation on a manifest fails."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        key_id: Optional[str] = None,
        algorithm: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if key_id is not None:
            ctx["key_id"] = key_id
        if algorithm is not None:
            ctx["algorithm"] = algorithm
        self.key_id = key_id
        self.algorithm = algorithm
        super().__init__(message, context=ctx, **kwargs)


class ManifestFormatError(ManifestError):
    """Raised when manifest JSON schema validation fails."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        schema_errors: Optional[list[str]] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if schema_errors is not None:
            ctx["schema_errors"] = schema_errors
        self.schema_errors = schema_errors or []
        super().__init__(message, context=ctx, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Integrity Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class IntegrityError(ReliaDLError):
    """Base exception for checksum and payload verification failures."""

    default_retryable: bool = True


class ChunkHashMismatchError(IntegrityError):
    """Raised when a downloaded chunk's SHA-256 does not match expected hash."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        chunk_index: Optional[int] = None,
        expected_hash: Optional[str] = None,
        computed_hash: Optional[str] = None,
        start_byte: Optional[int] = None,
        end_byte: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if chunk_index is not None:
            ctx["chunk_index"] = chunk_index
        if expected_hash is not None:
            ctx["expected_hash"] = expected_hash
        if computed_hash is not None:
            ctx["computed_hash"] = computed_hash
        if start_byte is not None:
            ctx["start_byte"] = start_byte
        if end_byte is not None:
            ctx["end_byte"] = end_byte

        self.chunk_index = chunk_index
        self.expected_hash = expected_hash
        self.computed_hash = computed_hash
        self.start_byte = start_byte
        self.end_byte = end_byte
        super().__init__(message, context=ctx, **kwargs)


class SubBlockCorruptedError(ChunkHashMismatchError):
    """Raised when a 64 KB sub-block streaming hash verification fails (SBM-IA)."""

    def __init__(
        self,
        message: str,
        sub_block_index: int,
        expected_hash: Optional[str] = None,
        computed_hash: Optional[str] = None,
        chunk_index: Optional[int] = None,
        offset: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        ctx["sub_block_index"] = sub_block_index
        if offset is not None:
            ctx["offset"] = offset
        self.sub_block_index = sub_block_index
        self.offset = offset
        super().__init__(
            message,
            chunk_index=chunk_index,
            expected_hash=expected_hash,
            computed_hash=computed_hash,
            context=ctx,
            **kwargs,
        )


class FileHashMismatchError(IntegrityError):
    """Raised when whole-file post-assembly hash verification does not match."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        file_path: Optional[str] = None,
        expected_hash: Optional[str] = None,
        computed_hash: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if file_path is not None:
            ctx["file_path"] = file_path
        if expected_hash is not None:
            ctx["expected_hash"] = expected_hash
        if computed_hash is not None:
            ctx["computed_hash"] = computed_hash

        self.file_path = file_path
        self.expected_hash = expected_hash
        self.computed_hash = computed_hash
        super().__init__(message, context=ctx, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Storage Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class StorageError(ReliaDLError):
    """Base exception for local disk I/O and filesystem errors."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        path: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if path is not None:
            ctx["path"] = path
        self.path = path
        super().__init__(message, context=ctx, **kwargs)


class DiskFullError(StorageError):
    """Raised when disk space is insufficient for chunk or whole-file writes."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        available_bytes: Optional[int] = None,
        required_bytes: Optional[int] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if available_bytes is not None:
            ctx["available_bytes"] = available_bytes
        if required_bytes is not None:
            ctx["required_bytes"] = required_bytes
        self.available_bytes = available_bytes
        self.required_bytes = required_bytes
        super().__init__(message, context=ctx, **kwargs)


class PermissionError(StorageError, builtins.PermissionError):
    """Raised when filesystem permissions prevent reading or writing target file."""

    default_retryable: bool = False


StoragePermissionError = PermissionError


class AllocationError(StorageError):
    """Raised when pre-allocating sparse file space fails."""

    default_retryable: bool = False


# ─────────────────────────────────────────────────────────────────────────────
# State Management Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class StateError(ReliaDLError):
    """Base exception for session state file tracking errors."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        state_file: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if state_file is not None:
            ctx["state_file"] = state_file
        self.state_file = state_file
        super().__init__(message, context=ctx, **kwargs)


class StateNotFoundError(StateError):
    """Raised when attempting to resume a download but state file cannot be found."""

    default_retryable: bool = False


class StateCorruptedError(StateError):
    """Raised when a state file is unparseable or fails structural validation."""

    default_retryable: bool = False

    def __init__(
        self,
        message: str,
        corrupted_field: Optional[str] = None,
        details: Optional[str] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if corrupted_field is not None:
            ctx["corrupted_field"] = corrupted_field
        if details is not None:
            ctx["details"] = details
        self.corrupted_field = corrupted_field
        self.details = details
        super().__init__(message, context=ctx, **kwargs)


# ─────────────────────────────────────────────────────────────────────────────
# Assembly Hierarchy
# ─────────────────────────────────────────────────────────────────────────────

class AssemblyError(ReliaDLError):
    """Base exception for chunk concatenation and final file assembly errors."""

    default_retryable: bool = True


class AssemblyFailedError(AssemblyError):
    """Raised when sequential chunk assembly fails."""

    default_retryable: bool = True

    def __init__(
        self,
        message: str,
        reason: Optional[str] = None,
        missing_chunks: Optional[list[int]] = None,
        context: Optional[dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        ctx = context.copy() if context else {}
        if reason is not None:
            ctx["reason"] = reason
        if missing_chunks is not None:
            ctx["missing_chunks"] = missing_chunks
        self.reason = reason
        self.missing_chunks = missing_chunks or []
        super().__init__(message, context=ctx, **kwargs)
