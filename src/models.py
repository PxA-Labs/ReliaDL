"""
Core domain models, dataclasses, and type definitions for ReliaDL.
Implements data structures for chunk specifications, states, statistics,
results, and system configuration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Union

try:
    from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
    _HAS_PYDANTIC = True
except ImportError:
    _HAS_PYDANTIC = False


class ChunkStatus(str, Enum):
    """Lifecycle status of an individual chunk."""

    PENDING = "PENDING"
    DOWNLOADING = "DOWNLOADING"
    IN_PROGRESS = "IN_PROGRESS"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABANDONED = "ABANDONED"

    @property
    def is_terminal(self) -> bool:
        """Indicates if the chunk has reached a final state."""
        return self in (
            ChunkStatus.COMPLETE,
            ChunkStatus.COMPLETED,
            ChunkStatus.ABANDONED,
        )

    @property
    def is_successful(self) -> bool:
        """Indicates if chunk was completed and verified."""
        return self in (ChunkStatus.COMPLETE, ChunkStatus.COMPLETED)


class DownloadStatus(str, Enum):
    """Lifecycle status of a download session."""

    PENDING = "PENDING"
    IN_PROGRESS = "IN_PROGRESS"
    DOWNLOADING = "DOWNLOADING"
    ASSEMBLING = "ASSEMBLING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        """Indicates if the download session is completed or terminated."""
        return self in (
            DownloadStatus.COMPLETE,
            DownloadStatus.COMPLETED,
            DownloadStatus.FAILED,
            DownloadStatus.CANCELLED,
        )

    @property
    def is_successful(self) -> bool:
        """Indicates if the download concluded successfully."""
        return self in (DownloadStatus.COMPLETE, DownloadStatus.COMPLETED)


@dataclass(frozen=True)
class ChunkSpec:
    """
    Immutable specification for a single chunk.

    Defines byte offsets, size, and optional expected cryptographic hash.
    """

    index: int
    start_byte: int
    end_byte: int
    size: int = 0
    expected_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError(f"Chunk index must be non-negative, got {self.index}")
        if self.start_byte < 0:
            raise ValueError(f"start_byte must be non-negative, got {self.start_byte}")
        if self.end_byte < self.start_byte:
            raise ValueError(
                f"end_byte ({self.end_byte}) cannot be less than start_byte ({self.start_byte})"
            )

        expected_size = self.end_byte - self.start_byte + 1
        if self.size == 0:
            object.__setattr__(self, "size", expected_size)
        elif self.size != expected_size:
            raise ValueError(
                f"Chunk size ({self.size}) does not match byte range size ({expected_size})"
            )

        if self.expected_hash is not None:
            cleaned_hash = self.expected_hash.strip().lower()
            if cleaned_hash.startswith("sha256:"):
                cleaned_hash = cleaned_hash[7:]
            object.__setattr__(self, "expected_hash", cleaned_hash)

    @property
    def range_header_value(self) -> str:
        """Returns standard HTTP Range header value (e.g., 'bytes=0-1023')."""
        return f"bytes={self.start_byte}-{self.end_byte}"

    def to_dict(self) -> dict[str, Any]:
        """Serialize ChunkSpec to dictionary."""
        return asdict(self)


@dataclass
class ChunkResult:
    """Outcome of downloading a single chunk."""

    index: int
    success: bool
    bytes_downloaded: int
    duration_sec: float
    computed_hash: Optional[str] = None
    error: Optional[str] = None

    @property
    def speed_bps(self) -> float:
        """Average download speed in bytes per second."""
        if self.duration_sec > 0:
            return self.bytes_downloaded / self.duration_sec
        return 0.0

    def to_dict(self) -> dict[str, Any]:
        """Serialize ChunkResult to dictionary."""
        return asdict(self)


@dataclass
class ChunkState:
    """State tracking for a chunk within a persistent download session."""

    index: int
    start_byte: int
    end_byte: int
    status: ChunkStatus = ChunkStatus.PENDING
    retries: int = 0
    hash_verified: bool = False
    expected_hash: Optional[str] = None
    computed_hash: Optional[str] = None
    file_path: Optional[str] = None
    error: Optional[str] = None

    @property
    def size(self) -> int:
        """Size of chunk in bytes."""
        return self.end_byte - self.start_byte + 1

    @property
    def spec(self) -> ChunkSpec:
        """Construct immutable ChunkSpec from this state."""
        return ChunkSpec(
            index=self.index,
            start_byte=self.start_byte,
            end_byte=self.end_byte,
            size=self.size,
            expected_hash=self.expected_hash,
        )

    def mark_in_progress(self) -> None:
        """Transition chunk state to IN_PROGRESS."""
        self.status = ChunkStatus.IN_PROGRESS

    def mark_verifying(self) -> None:
        """Transition chunk state to VERIFYING."""
        self.status = ChunkStatus.VERIFYING

    def mark_complete(self, computed_hash: Optional[str] = None) -> None:
        """Transition chunk state to COMPLETE upon successful verification."""
        self.status = ChunkStatus.COMPLETE
        self.hash_verified = True
        if computed_hash:
            self.computed_hash = computed_hash
        self.error = None

    def mark_failed(self, error: Optional[str] = None, increment_retry: bool = True) -> None:
        """Transition chunk state to FAILED for retry handling."""
        self.status = ChunkStatus.FAILED
        if increment_retry:
            self.retries += 1
        if error:
            self.error = error

    def mark_abandoned(self, error: Optional[str] = None) -> None:
        """Transition chunk state to ABANDONED when retry limit is exhausted."""
        self.status = ChunkStatus.ABANDONED
        if error:
            self.error = error

    def to_dict(self) -> dict[str, Any]:
        """Serialize ChunkState to dictionary."""
        data = asdict(self)
        data["status"] = self.status.value
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChunkState:
        """Deserialize ChunkState from dictionary."""
        data_copy = dict(data)
        if "status" in data_copy and isinstance(data_copy["status"], str):
            data_copy["status"] = ChunkStatus(data_copy["status"])
        return cls(**data_copy)


@dataclass
class DownloadStatistics:
    """Aggregated runtime metrics for a download session."""

    bytes_completed: int = 0
    bytes_total: int = 0
    speed_bps: float = 0.0
    eta_seconds: Optional[float] = None
    active_workers: int = 0

    @property
    def progress_percentage(self) -> float:
        """Download completion percentage normalized to range [0.0, 100.0]."""
        if self.bytes_total <= 0:
            return 0.0
        pct = (self.bytes_completed / self.bytes_total) * 100.0
        return min(max(pct, 0.0), 100.0)

    @property
    def is_complete(self) -> bool:
        """True if all bytes have completed transferring."""
        return self.bytes_total > 0 and self.bytes_completed >= self.bytes_total

    @property
    def bytes_remaining(self) -> int:
        """Total uncompleted bytes."""
        return max(0, self.bytes_total - self.bytes_completed)

    def to_dict(self) -> dict[str, Any]:
        """Serialize DownloadStatistics to dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadStatistics:
        """Deserialize DownloadStatistics from dictionary."""
        return cls(**data)


@dataclass
class DownloadState:
    """
    Complete persistent state of a download session.

    Enables atomic serialization and crash-resilient session resumption.
    """

    download_id: str
    url: str
    target_path: str
    file_size: int
    chunk_size: int = 8_388_608
    hash_algorithm: str = "sha256"
    expected_file_hash: Optional[str] = None
    etag: Optional[str] = None
    output_path: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: DownloadStatus = DownloadStatus.PENDING
    chunks: list[ChunkState] = field(default_factory=list)
    statistics: Optional[DownloadStatistics] = None
    version: str = "1.0.0"

    def __post_init__(self) -> None:
        if self.output_path is None:
            self.output_path = self.target_path

    @property
    def total_chunks(self) -> int:
        """Total number of chunks planned."""
        return len(self.chunks)

    @property
    def completed_chunks(self) -> list[ChunkState]:
        """List of successfully downloaded and verified chunks."""
        return [
            c for c in self.chunks
            if c.status in (ChunkStatus.COMPLETE, ChunkStatus.COMPLETED)
        ]

    @property
    def pending_chunks(self) -> list[ChunkState]:
        """List of chunks waiting for dispatch."""
        return [c for c in self.chunks if c.status == ChunkStatus.PENDING]

    @property
    def failed_chunks(self) -> list[ChunkState]:
        """List of chunks that failed and await retry."""
        return [c for c in self.chunks if c.status == ChunkStatus.FAILED]

    @property
    def abandoned_chunks(self) -> list[ChunkState]:
        """List of chunks that permanently failed after exhausting max retries."""
        return [c for c in self.chunks if c.status == ChunkStatus.ABANDONED]

    @property
    def is_finished(self) -> bool:
        """True if session has concluded (terminal state)."""
        return self.status.is_terminal

    def update_timestamp(self) -> None:
        """Refresh the updated_at timestamp to current UTC."""
        self.updated_at = datetime.now(timezone.utc)

    def to_dict(self) -> dict[str, Any]:
        """Convert download state to a JSON-compatible dictionary."""
        return {
            "version": self.version,
            "download_id": self.download_id,
            "url": self.url,
            "target_path": self.target_path,
            "output_path": self.output_path or self.target_path,
            "file_size": self.file_size,
            "chunk_size": self.chunk_size,
            "hash_algorithm": self.hash_algorithm,
            "expected_file_hash": self.expected_file_hash,
            "etag": self.etag,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "status": self.status.value,
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "statistics": self.statistics.to_dict() if self.statistics else None,
        }

    def to_json(self, indent: int = 2) -> str:
        """Serialize state to formatted JSON string."""
        return json.dumps(self.to_dict(), indent=indent)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DownloadState:
        """Construct DownloadState instance from serialized dictionary."""
        data_copy = dict(data)

        if isinstance(data_copy.get("created_at"), str):
            data_copy["created_at"] = datetime.fromisoformat(data_copy["created_at"])
        if isinstance(data_copy.get("updated_at"), str):
            data_copy["updated_at"] = datetime.fromisoformat(data_copy["updated_at"])

        if "status" in data_copy and isinstance(data_copy["status"], str):
            data_copy["status"] = DownloadStatus(data_copy["status"])

        if "chunks" in data_copy and isinstance(data_copy["chunks"], list):
            data_copy["chunks"] = [
                c if isinstance(c, ChunkState) else ChunkState.from_dict(c)
                for c in data_copy["chunks"]
            ]

        if "statistics" in data_copy and isinstance(data_copy["statistics"], dict):
            data_copy["statistics"] = DownloadStatistics.from_dict(data_copy["statistics"])

        return cls(**data_copy)

    @classmethod
    def from_json(cls, json_str: str) -> DownloadState:
        """Construct DownloadState instance from JSON string."""
        return cls.from_dict(json.loads(json_str))


@dataclass(frozen=True)
class DownloadResult:
    """Result of a completed download operation."""

    output_path: Path
    file_hash: str
    is_verified: bool
    file_size: int
    total_chunks: int
    chunks_retried: int = 0
    total_bytes_downloaded: int = 0
    elapsed_seconds: float = 0.0
    average_speed_bps: float = 0.0
    download_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Serialize DownloadResult to dictionary."""
        data = asdict(self)
        data["output_path"] = str(self.output_path)
        return data


@dataclass(frozen=True)
class VerificationResult:
    """Result of a cryptographic hash verification operation."""

    file_path: Path
    expected_hash: str
    computed_hash: str
    is_valid: bool
    bytes_verified: int
    duration_seconds: float = 0.0
    algorithm: str = "sha256"

    def to_dict(self) -> dict[str, Any]:
        """Serialize VerificationResult to dictionary."""
        data = asdict(self)
        data["file_path"] = str(self.file_path)
        return data


@dataclass(frozen=True)
class ProgressReport:
    """Point-in-time progress telemetry snapshot for callbacks and monitoring."""

    download_id: str
    timestamp: datetime
    total_bytes: int
    downloaded_bytes: int
    percentage: float
    total_chunks: int
    chunks_complete: int
    chunks_in_progress: int
    chunks_failed: int
    chunks_pending: int
    current_speed_bps: float
    average_speed_bps: float
    elapsed_seconds: float
    estimated_remaining_seconds: Optional[float] = None
    active_workers: int = 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize ProgressReport to dictionary."""
        data = asdict(self)
        data["timestamp"] = self.timestamp.isoformat()
        return data


if _HAS_PYDANTIC:
    class DownloadConfig(BaseModel):
        """
        Pydantic model providing validation and typing for download engine configurations.
        """

        model_config = ConfigDict(extra="ignore", validate_assignment=True)

        # Chunking
        chunk_size_bytes: int = Field(
            default=8_388_608,
            description="Size of each chunk in bytes (1MB to 256MB)",
        )

        # Parallelism
        max_parallel_workers: int = Field(
            default=4,
            ge=1,
            le=32,
            description="Maximum concurrent chunk downloads",
        )

        # Retry Policies
        max_retries_per_chunk: int = Field(
            default=3,
            ge=0,
            le=100,
            description="Maximum retry attempts per chunk",
        )
        retry_base_delay_seconds: float = Field(
            default=1.0,
            ge=0.0,
            description="Base delay before first retry in seconds",
        )
        retry_max_delay_seconds: float = Field(
            default=60.0,
            ge=0.0,
            description="Maximum delay between retries in seconds",
        )
        retry_backoff_factor: float = Field(
            default=2.0,
            ge=1.0,
            description="Exponential multiplier applied to retry delay",
        )
        retry_jitter_factor: float = Field(
            default=0.5,
            ge=0.0,
            le=1.0,
            description="Random jitter factor added to retry delay",
        )

        # Timeouts
        connect_timeout_seconds: float = Field(
            default=30.0,
            gt=0.0,
            description="TCP connection timeout in seconds",
        )
        read_timeout_seconds: float = Field(
            default=300.0,
            gt=0.0,
            description="Read timeout for chunk transfer in seconds",
        )

        # Hashing & Storage
        hash_algorithm: str = Field(
            default="sha256",
            description="Cryptographic hash algorithm for chunk/file verification",
        )
        state_directory: str = Field(
            default=".ReliaDL",
            description="Directory name for state and chunk files",
        )
        verify_on_complete: bool = Field(
            default=True,
            description="Perform whole-file hash verification after assembly",
        )
        cleanup_chunks_on_complete: bool = Field(
            default=True,
            description="Delete chunk files after successful assembly",
        )
        pre_check_disk_space: bool = Field(
            default=True,
            description="Verify sufficient disk space prior to downloading",
        )
        direct_write: bool = Field(
            default=False,
            description="Direct sparse file allocation bypassing staged chunk storage",
        )

        # Progress & Metrics
        progress_update_interval_seconds: float = Field(
            default=0.5,
            gt=0.0,
            description="Minimum interval between progress updates in seconds",
        )

        # Network & Transport
        user_agent: str = Field(
            default="ReliaDL/1.0",
            description="HTTP User-Agent header value",
        )
        max_bandwidth_bytes_per_sec: int = Field(
            default=0,
            ge=0,
            description="Maximum download bandwidth in bytes/sec (0 = unlimited)",
        )
        http2: bool = Field(
            default=True,
            description="Enable HTTP/2 protocol",
        )
        verify_ssl: bool = Field(
            default=True,
            description="Verify TLS/SSL certificates",
        )
        max_redirects: int = Field(
            default=5,
            ge=0,
            le=50,
            description="Maximum HTTP redirects to follow",
        )
        proxy_url: Optional[str] = Field(
            default=None,
            description="HTTP/HTTPS/SOCKS5 proxy URL",
        )
        manifest_path: Optional[str] = Field(
            default=None,
            description="Path to .cgmanifest file",
        )

        @field_validator("chunk_size_bytes")
        @classmethod
        def validate_chunk_size(cls, v: int) -> int:
            min_size = 1024 * 1024  # 1 MB
            max_size = 256 * 1024 * 1024  # 256 MB
            if v < min_size or v > max_size:
                raise ValueError(
                    f"chunk_size_bytes must be between {min_size} (1MB) and {max_size} (256MB), got {v}"
                )
            return v

        @field_validator("hash_algorithm")
        @classmethod
        def validate_hash_algorithm(cls, v: str) -> str:
            v_clean = v.strip().lower()
            if v_clean in ("md5", "sha1"):
                raise ValueError(
                    f"Weak hash algorithm '{v}' is disallowed by security policy. Use sha256 or sha512."
                )
            if v_clean not in ("sha256", "sha384", "sha512"):
                raise ValueError(
                    f"Unsupported hash algorithm '{v}'. Supported algorithms: sha256, sha384, sha512."
                )
            return v_clean

        @model_validator(mode="after")
        def validate_retry_ranges(self) -> DownloadConfig:
            if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
                raise ValueError(
                    f"retry_max_delay_seconds ({self.retry_max_delay_seconds}) cannot be less than "
                    f"retry_base_delay_seconds ({self.retry_base_delay_seconds})"
                )
            return self

        def validate(self) -> None:
            """Validate configuration values. Raises ValueError on invalid values."""
            self.model_validate(self.model_dump())

        def to_dict(self) -> dict[str, Any]:
            """Convert configuration to dictionary."""
            return self.model_dump()

        @classmethod
        def from_dict(cls, data: dict[str, Any]) -> DownloadConfig:
            """Instantiate configuration from dictionary."""
            return cls.model_validate(data)

else:
    @dataclass
    class DownloadConfig:
        """
        Configuration dataclass providing typing and boundary validation.
        Fallback implementation for environments where pydantic is not installed.
        """

        chunk_size_bytes: int = 8_388_608
        max_parallel_workers: int = 4
        max_retries_per_chunk: int = 3
        retry_base_delay_seconds: float = 1.0
        retry_max_delay_seconds: float = 60.0
        retry_backoff_factor: float = 2.0
        retry_jitter_factor: float = 0.5
        connect_timeout_seconds: float = 30.0
        read_timeout_seconds: float = 300.0
        hash_algorithm: str = "sha256"
        state_directory: str = ".ReliaDL"
        verify_on_complete: bool = True
        cleanup_chunks_on_complete: bool = True
        pre_check_disk_space: bool = True
        direct_write: bool = False
        progress_update_interval_seconds: float = 0.5
        user_agent: str = "ReliaDL/1.0"
        max_bandwidth_bytes_per_sec: int = 0
        http2: bool = True
        verify_ssl: bool = True
        max_redirects: int = 5
        proxy_url: Optional[str] = None
        manifest_path: Optional[str] = None

        def __post_init__(self) -> None:
            self.validate()

        def validate(self) -> None:
            """Validate configuration parameters against system limits."""
            min_size = 1024 * 1024
            max_size = 256 * 1024 * 1024
            if self.chunk_size_bytes < min_size or self.chunk_size_bytes > max_size:
                raise ValueError(
                    f"chunk_size_bytes must be between {min_size} (1MB) and {max_size} (256MB), got {self.chunk_size_bytes}"
                )
            if not (1 <= self.max_parallel_workers <= 32):
                raise ValueError(
                    f"max_parallel_workers must be between 1 and 32, got {self.max_parallel_workers}"
                )
            if not (0 <= self.max_retries_per_chunk <= 100):
                raise ValueError("max_retries_per_chunk must be between 0 and 100")
            if self.retry_base_delay_seconds < 0:
                raise ValueError("retry_base_delay_seconds must be >= 0")
            if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
                raise ValueError(
                    f"retry_max_delay_seconds ({self.retry_max_delay_seconds}) cannot be less than "
                    f"retry_base_delay_seconds ({self.retry_base_delay_seconds})"
                )
            if self.retry_backoff_factor < 1.0:
                raise ValueError("retry_backoff_factor must be >= 1.0")
            if not (0.0 <= self.retry_jitter_factor <= 1.0):
                raise ValueError("retry_jitter_factor must be between 0.0 and 1.0")
            if self.connect_timeout_seconds <= 0:
                raise ValueError("connect_timeout_seconds must be > 0")
            if self.read_timeout_seconds <= 0:
                raise ValueError("read_timeout_seconds must be > 0")
            if self.progress_update_interval_seconds <= 0:
                raise ValueError("progress_update_interval_seconds must be > 0")
            if self.max_bandwidth_bytes_per_sec < 0:
                raise ValueError("max_bandwidth_bytes_per_sec must be >= 0")
            if not (0 <= self.max_redirects <= 50):
                raise ValueError("max_redirects must be between 0 and 50")

            clean_hash = self.hash_algorithm.strip().lower()
            if clean_hash in ("md5", "sha1"):
                raise ValueError(
                    f"Weak hash algorithm '{self.hash_algorithm}' is disallowed by security policy. Use sha256 or sha512."
                )
            if clean_hash not in ("sha256", "sha384", "sha512"):
                raise ValueError(
                    f"Unsupported hash algorithm '{self.hash_algorithm}'. Supported algorithms: sha256, sha384, sha512."
                )

        def to_dict(self) -> dict[str, Any]:
            """Convert configuration to dictionary."""
            return asdict(self)

        @classmethod
        def from_dict(cls, data: dict[str, Any]) -> DownloadConfig:
            """Instantiate configuration from dictionary."""
            return cls(**data)
