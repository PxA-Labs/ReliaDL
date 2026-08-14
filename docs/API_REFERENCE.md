# API Reference — ChunkGuard

> **Audience**: Developers integrating or extending ChunkGuard
> **Reading time**: ~15 minutes

---

## 1. Public API Overview

ChunkGuard can be used as a CLI tool or as a Python library. This document covers the programmatic Python API.

### Installation

```bash
pip install chunkguard
```

### Quick Start (Library Usage)

```python
import asyncio
from chunkguard import ChunkGuard, DownloadConfig

async def main():
    config = DownloadConfig(
        chunk_size_bytes=8_388_608,  # 8 MB
        max_parallel_workers=4,
        max_retries_per_chunk=3,
    )
    
    cg = ChunkGuard(config)
    
    result = await cg.download(
        url="https://example.com/largefile.iso",
        output_path="./downloads/largefile.iso",
        expected_hash="sha256:abcdef1234567890...",
    )
    
    print(f"Downloaded: {result.output_path}")
    print(f"File hash:  {result.file_hash}")
    print(f"Verified:   {result.is_verified}")

asyncio.run(main())
```

---

## 2. Core Classes

### 2.1 `ChunkGuard`

The main entry point for all download operations.

```python
class ChunkGuard:
    """
    Fault-tolerant chunked file download engine.
    
    Thread Safety: NOT thread-safe. Use one instance per asyncio event loop.
    """
    
    def __init__(self, config: DownloadConfig | None = None) -> None:
        """
        Initialize ChunkGuard.
        
        Args:
            config: Download configuration. If None, uses defaults.
        """
    
    async def download(
        self,
        url: str,
        output_path: str | Path,
        expected_hash: str | None = None,
        on_progress: ProgressCallback | None = None,
        headers: dict[str, str] | None = None,
    ) -> DownloadResult:
        """
        Download a file with chunked verification.
        
        If a state file exists for this download, automatically resumes.
        
        Args:
            url:           Source URL (HTTP/HTTPS)
            output_path:   Destination file path
            expected_hash: Expected SHA-256 hash (format: "sha256:hexdigest" or bare hexdigest)
            on_progress:   Callback invoked with ProgressReport on each update
            headers:       Additional HTTP headers (e.g., auth tokens)
        
        Returns:
            DownloadResult with file path, hash, and verification status
        
        Raises:
            ConfigurationError:  Invalid configuration
            NetworkError:        Unrecoverable network failure
            IntegrityError:      File hash mismatch after all retries
            StorageError:        Disk full or permission denied
        """
    
    async def resume(
        self,
        state_file: str | Path,
        on_progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        """
        Resume an interrupted download from a state file.
        
        Args:
            state_file:    Path to the .chunkguard state file
            on_progress:   Progress callback
        
        Returns:
            DownloadResult
        
        Raises:
            StateNotFoundError:    State file doesn't exist
            StateCorruptedError:   State file is unreadable
        """
    
    async def verify(
        self,
        file_path: str | Path,
        expected_hash: str,
    ) -> VerificationResult:
        """
        Verify a file's SHA-256 hash.
        
        Args:
            file_path:     Path to the file to verify
            expected_hash: Expected SHA-256 hash
        
        Returns:
            VerificationResult with match status
        """
    
    def get_status(
        self,
        state_file: str | Path,
    ) -> DownloadState:
        """
        Get the current status of a download.
        
        Args:
            state_file: Path to the state file
        
        Returns:
            DownloadState snapshot
        """
    
    async def cancel(
        self,
        state_file: str | Path,
        cleanup: bool = False,
    ) -> None:
        """
        Cancel an in-progress download.
        
        Args:
            state_file: Path to the state file
            cleanup:    If True, delete chunk files and state file
        """
```

---

### 2.2 `DownloadConfig`

```python
@dataclass
class DownloadConfig:
    """Configuration for download operations."""
    
    # Chunking
    chunk_size_bytes: int = 8_388_608
    """Size of each chunk in bytes. Range: 1 MB – 256 MB."""
    
    # Parallelism
    max_parallel_workers: int = 4
    """Maximum concurrent chunk downloads. Range: 1 – 32."""
    
    # Retry
    max_retries_per_chunk: int = 3
    """Maximum retry attempts per chunk (including first attempt)."""
    
    retry_base_delay_seconds: float = 1.0
    """Base delay before first retry."""
    
    retry_max_delay_seconds: float = 60.0
    """Maximum delay between retries."""
    
    retry_backoff_factor: float = 2.0
    """Multiplier applied to delay on each successive retry."""
    
    # Timeouts
    connect_timeout_seconds: float = 30.0
    """TCP connection timeout."""
    
    read_timeout_seconds: float = 300.0
    """Time to wait for data after connection is established."""
    
    # Hashing
    hash_algorithm: str = "sha256"
    """Hash algorithm for verification. Currently only 'sha256' is supported."""
    
    # Storage
    state_directory: str = ".chunkguard"
    """Directory name for state and chunk files (relative to output directory)."""
    
    # Verification
    verify_on_complete: bool = True
    """Perform whole-file hash verification after assembly."""
    
    cleanup_chunks_on_complete: bool = True
    """Delete chunk files after successful assembly."""
    
    # Progress
    progress_update_interval_seconds: float = 0.5
    """Minimum interval between progress callbacks."""
    
    # Network
    user_agent: str = "ChunkGuard/1.0"
    """HTTP User-Agent header value."""
    
    max_bandwidth_bytes_per_sec: int = 0
    """Maximum download bandwidth in bytes/sec. 0 = unlimited."""
    
    http2: bool = True
    """Enable HTTP/2 protocol."""
    
    verify_ssl: bool = True
    """Verify TLS certificates."""
    
    max_redirects: int = 5
    """Maximum number of HTTP redirects to follow."""
    
    def validate(self) -> None:
        """Validate configuration values. Raises ConfigurationError on invalid values."""
```

---

### 2.3 `DownloadResult`

```python
@dataclass(frozen=True)
class DownloadResult:
    """Result of a completed download operation."""
    
    output_path: Path
    """Path to the downloaded file."""
    
    file_hash: str
    """Computed SHA-256 hash of the downloaded file."""
    
    is_verified: bool
    """True if the file hash matches the expected hash."""
    
    file_size: int
    """Total file size in bytes."""
    
    total_chunks: int
    """Total number of chunks."""
    
    chunks_retried: int
    """Number of chunks that required at least one retry."""
    
    total_bytes_downloaded: int
    """Total bytes transferred (including retried chunks)."""
    
    elapsed_seconds: float
    """Total download time in seconds."""
    
    average_speed_bps: float
    """Average download speed in bytes per second."""
    
    download_id: str
    """Unique identifier for this download session."""
```

---

### 2.4 `VerificationResult`

```python
@dataclass(frozen=True)
class VerificationResult:
    """Result of a hash verification operation."""
    
    file_path: Path
    """Path to the verified file."""
    
    expected_hash: str
    """Expected hash value."""
    
    computed_hash: str
    """Computed hash value."""
    
    is_valid: bool
    """True if hashes match."""
    
    bytes_verified: int
    """Number of bytes processed during verification."""
    
    duration_seconds: float
    """Time taken to compute the hash."""
    
    algorithm: str
    """Hash algorithm used."""
```

---

### 2.5 `ProgressReport`

```python
@dataclass(frozen=True)
class ProgressReport:
    """Snapshot of download progress at a point in time."""
    
    download_id: str
    timestamp: datetime
    
    # Byte progress
    total_bytes: int
    downloaded_bytes: int
    percentage: float                    # 0.0 – 100.0
    
    # Chunk progress
    total_chunks: int
    chunks_complete: int
    chunks_in_progress: int
    chunks_failed: int
    chunks_pending: int
    
    # Speed
    current_speed_bps: float
    average_speed_bps: float
    
    # Time
    elapsed_seconds: float
    estimated_remaining_seconds: float | None
    
    # Workers
    active_workers: int
```

---

### 2.6 `ChunkSpec`

```python
@dataclass(frozen=True)
class ChunkSpec:
    """Specification for a single chunk."""
    
    index: int
    """Zero-based chunk index."""
    
    start_byte: int
    """First byte of the chunk (inclusive)."""
    
    end_byte: int
    """Last byte of the chunk (inclusive, for HTTP Range header)."""
    
    size: int
    """Chunk size in bytes (end_byte - start_byte + 1)."""
    
    expected_hash: str | None
    """Expected SHA-256 hash, or None if not yet known."""
```

---

### 2.7 `DownloadState`

```python
@dataclass
class DownloadState:
    """Complete state of a download (serializable to/from JSON)."""
    
    version: str
    download_id: str
    url: str
    file_size: int
    chunk_size: int
    hash_algorithm: str
    expected_file_hash: str | None
    etag: str | None
    output_path: str
    created_at: datetime
    updated_at: datetime
    status: DownloadStatus
    chunks: list[ChunkState]
    statistics: DownloadStatistics
```

---

## 3. Enumerations

### 3.1 `DownloadStatus`

```python
class DownloadStatus(str, Enum):
    PENDING      = "PENDING"       # Download created but not started
    IN_PROGRESS  = "IN_PROGRESS"   # Actively downloading chunks
    ASSEMBLING   = "ASSEMBLING"    # All chunks complete, assembling file
    VERIFYING    = "VERIFYING"     # Running whole-file verification
    COMPLETE     = "COMPLETE"      # Successfully completed
    FAILED       = "FAILED"        # Unrecoverable failure
    CANCELLED    = "CANCELLED"     # Cancelled by user
```

### 3.2 `ChunkStatus`

```python
class ChunkStatus(str, Enum):
    PENDING      = "PENDING"       # Not yet downloaded
    DOWNLOADING  = "DOWNLOADING"   # Currently being downloaded
    COMPLETE     = "COMPLETE"      # Downloaded and hash-verified
    FAILED       = "FAILED"        # Failed, eligible for retry
    ABANDONED    = "ABANDONED"     # Max retries exceeded
```

---

## 4. Callback Types

### 4.1 `ProgressCallback`

```python
ProgressCallback = Callable[[ProgressReport], None]
"""
Called periodically during download with progress updates.

Example:
    def my_progress(report: ProgressReport) -> None:
        print(f"{report.percentage:.1f}% — {report.current_speed_bps / 1e6:.1f} MB/s")
    
    await cg.download(url, output, on_progress=my_progress)
"""
```

---

## 5. CLI Reference

### 5.1 `download` Command

```
Usage: chunkguard download [OPTIONS] URL OUTPUT

  Download a file with chunked verification.

Arguments:
  URL     Source URL (HTTP/HTTPS)
  OUTPUT  Output file path

Options:
  --hash TEXT               Expected SHA-256 hash for verification
  --chunk-size TEXT         Chunk size (e.g., '8MB', '16MB') [default: 8MB]
  --workers INTEGER         Parallel download workers [default: 4]
  --retries INTEGER         Max retries per chunk [default: 3]
  --config FILE             Path to config file
  --no-verify               Skip whole-file verification after assembly
  --header TEXT             Additional HTTP header (KEY:VALUE), repeatable
  --verbose / --quiet       Log verbosity
  --help                    Show this message and exit
```

### 5.2 `resume` Command

```
Usage: chunkguard resume [OPTIONS] STATE_FILE

  Resume an interrupted download.

Arguments:
  STATE_FILE  Path to .chunkguard state file

Options:
  --workers INTEGER         Override parallel workers [default: from state]
  --verbose / --quiet       Log verbosity
  --help                    Show this message and exit
```

### 5.3 `verify` Command

```
Usage: chunkguard verify [OPTIONS] FILE HASH

  Verify a file's SHA-256 hash.

Arguments:
  FILE  Path to file
  HASH  Expected SHA-256 hash (64-char hex string)

Options:
  --algorithm TEXT           Hash algorithm [default: sha256]
  --help                    Show this message and exit
```

### 5.4 `status` Command

```
Usage: chunkguard status [OPTIONS] STATE_FILE

  Display download progress from a state file.

Arguments:
  STATE_FILE  Path to .chunkguard state file

Options:
  --json                    Output as JSON
  --help                    Show this message and exit
```

---

## 6. Exit Codes

| Code | Meaning |
|---|---|
| `0` | Success — download complete and verified |
| `1` | General error — check stderr for details |
| `2` | Usage error — invalid arguments |
| `3` | Network error — could not reach server |
| `4` | Integrity error — hash verification failed |
| `5` | Storage error — disk full or permission denied |
| `10` | Cancelled — download was cancelled by user (Ctrl+C) |

---

## 7. Environment Variables

| Variable | Description | Default |
|---|---|---|
| `CHUNKGUARD_CHUNK_SIZE` | Override chunk size (e.g., `16MB`) | `8MB` |
| `CHUNKGUARD_WORKERS` | Override parallel worker count | `4` |
| `CHUNKGUARD_RETRIES` | Override max retries per chunk | `3` |
| `CHUNKGUARD_LOG_LEVEL` | Logging level | `INFO` |
| `CHUNKGUARD_LOG_FORMAT` | Log format (`json` or `text`) | `json` |
| `CHUNKGUARD_CONFIG` | Path to config file | None |
| `CHUNKGUARD_STATE_DIR` | State directory name | `.chunkguard` |
| `CHUNKGUARD_NO_VERIFY` | Skip whole-file verification (`1`/`true`) | `false` |
| `CHUNKGUARD_HTTP2` | Enable HTTP/2 (`1`/`true`) | `true` |
| `CHUNKGUARD_VERIFY_SSL` | Verify TLS certs (`1`/`true`) | `true` |
| `CHUNKGUARD_USER_AGENT` | User-Agent header | `ChunkGuard/1.0` |

---

## 8. Usage Examples

### 8.1 Basic Download

```python
async def basic_download():
    cg = ChunkGuard()
    result = await cg.download(
        url="https://releases.example.com/app-v2.0.tar.gz",
        output_path="./app-v2.0.tar.gz",
    )
    print(f"Hash: {result.file_hash}")
```

### 8.2 Download with Verification and Progress

```python
async def verified_download():
    config = DownloadConfig(
        chunk_size_bytes=16 * 1024 * 1024,  # 16 MB chunks
        max_parallel_workers=8,
    )
    
    def show_progress(report: ProgressReport):
        bar_width = 40
        filled = int(bar_width * report.percentage / 100)
        bar = '█' * filled + '░' * (bar_width - filled)
        speed = report.current_speed_bps / (1024 * 1024)
        print(f"\r  {bar}  {report.percentage:5.1f}%  {speed:.1f} MB/s", end="")
    
    cg = ChunkGuard(config)
    result = await cg.download(
        url="https://data.example.com/dataset.parquet",
        output_path="./dataset.parquet",
        expected_hash="sha256:a1b2c3d4e5f6...",
        on_progress=show_progress,
    )
    
    print(f"\n{'✅ Verified' if result.is_verified else '❌ MISMATCH'}")
```

### 8.3 Download with Custom Headers (Auth)

```python
async def authenticated_download():
    cg = ChunkGuard()
    result = await cg.download(
        url="https://private.example.com/artifact.zip",
        output_path="./artifact.zip",
        headers={
            "Authorization": "Bearer eyJhbGciOi...",
            "X-Request-ID": "req-12345",
        },
    )
```

### 8.4 Resume After Crash

```python
async def resume_download():
    cg = ChunkGuard()
    result = await cg.resume(
        state_file="./downloads/.chunkguard/largefile.iso.state",
    )
    print(f"Resumed and completed: {result.output_path}")
```

### 8.5 Verify Existing File

```python
async def verify_file():
    cg = ChunkGuard()
    result = await cg.verify(
        file_path="./largefile.iso",
        expected_hash="e3b0c44298fc1c149afbf4c8996fb924...",
    )
    
    if result.is_valid:
        print(f"✅ File is intact ({result.bytes_verified:,} bytes verified)")
    else:
        print(f"❌ Hash mismatch!")
        print(f"   Expected: {result.expected_hash}")
        print(f"   Computed: {result.computed_hash}")
```
