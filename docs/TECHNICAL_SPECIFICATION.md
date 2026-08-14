# Technical Specification — ChunkGuard

> **Audience**: Software Engineers, Implementers
> **Reading time**: ~25 minutes

---

## 1. Protocol Specification

### 1.1 HTTP Range Request Protocol

ChunkGuard uses the standard HTTP/1.1 Range Request mechanism defined in [RFC 7233](https://tools.ietf.org/html/rfc7233).

#### Server Capability Detection

Before chunked downloading begins, a `HEAD` request is issued to determine server capabilities:

```http
HEAD /largefile.iso HTTP/1.1
Host: example.com
User-Agent: ChunkGuard/1.0
```

**Expected Response Headers**:

| Header | Required | Example | Purpose |
|---|---|---|---|
| `Content-Length` | Yes | `107374182400` | Total file size in bytes |
| `Accept-Ranges` | Yes | `bytes` | Confirms Range support |
| `ETag` | Recommended | `"abc123"` | File version identifier |
| `Last-Modified` | Recommended | `Thu, 15 Jan 2026 10:00:00 GMT` | Detect file changes between chunks |
| `Content-Type` | Optional | `application/octet-stream` | MIME type |

#### Fallback Behavior

| Server Response | ChunkGuard Behavior |
|---|---|
| `Accept-Ranges: bytes` present | Proceed with chunked download |
| `Accept-Ranges: none` or header absent | Fall back to single-stream download with whole-file hash verification |
| No `Content-Length` header | Fall back to single-stream (cannot compute chunk boundaries) |
| `ETag` changes between requests | Abort and restart — file changed on server |

#### Chunk Download Request

```http
GET /largefile.iso HTTP/1.1
Host: example.com
Range: bytes=8388608-16777215
User-Agent: ChunkGuard/1.0
If-Match: "abc123"
```

**Expected Response**:

```http
HTTP/1.1 206 Partial Content
Content-Range: bytes 8388608-16777215/107374182400
Content-Length: 8388608
Content-Type: application/octet-stream
ETag: "abc123"

[binary data]
```

#### Response Code Handling

| Status Code | Meaning | Action |
|---|---|---|
| `206 Partial Content` | Success — partial content delivered | Accept and verify chunk |
| `200 OK` | Server ignored Range header | If chunk 0, may be acceptable; otherwise retry or fall back |
| `416 Range Not Satisfiable` | Invalid byte range | Bug — log error, mark chunk ABANDONED |
| `304 Not Modified` | Conditional request — no change | Skip chunk (already have it) |
| `429 Too Many Requests` | Rate limited | Retry with `Retry-After` header value or backoff |
| `500/502/503/504` | Server error | Retry with exponential backoff |
| `401/403` | Authentication required | Abort — non-retryable |
| `404` | File not found | Abort — non-retryable |

---

## 2. Chunking Algorithm

### 2.1 Chunk Size Computation

```python
def compute_chunks(file_size: int, chunk_size: int) -> list[ChunkSpec]:
    """
    Divide a file into chunk specifications.
    
    Args:
        file_size: Total file size in bytes
        chunk_size: Desired chunk size in bytes
    
    Returns:
        List of ChunkSpec objects defining byte ranges
    
    Invariants:
        - Chunks are contiguous and non-overlapping
        - Union of all chunk ranges == [0, file_size)
        - All chunks except possibly the last are exactly chunk_size bytes
        - The last chunk is <= chunk_size bytes
    """
    chunks = []
    num_chunks = math.ceil(file_size / chunk_size)
    
    for i in range(num_chunks):
        start = i * chunk_size
        end = min(start + chunk_size, file_size) - 1  # inclusive end for HTTP Range
        chunks.append(ChunkSpec(
            index=i,
            start_byte=start,
            end_byte=end,
            size=end - start + 1,
            expected_hash=None  # populated after first download or from manifest
        ))
    
    return chunks
```

### 2.2 Chunk Size Guidelines

| File Size | Recommended Chunk Size | Resulting Chunks | Rationale |
|---|---|---|---|
| < 10 MB | No chunking | 1 | Overhead exceeds benefit |
| 10 MB – 1 GB | 4 MB | 3 – 256 | Fine-grained retry |
| 1 GB – 10 GB | 8 MB (default) | 128 – 1,280 | Balance granularity & overhead |
| 10 GB – 100 GB | 16 MB | 640 – 6,400 | Reduce manifest size |
| 100 GB – 1 TB | 32–64 MB | 1,600 – 32,000 | Reduce per-chunk overhead |
| > 1 TB | 64–128 MB | 8,000 – 16,000 | Practical upper limit for chunk count |

### 2.3 Chunk Size Constraints

```
MINIMUM_CHUNK_SIZE = 1_048_576        # 1 MB — below this, overhead dominates
MAXIMUM_CHUNK_SIZE = 268_435_456      # 256 MB — above this, retry cost is too high
MAXIMUM_CHUNK_COUNT = 100_000         # prevent state file explosion
```

If `file_size / chunk_size > MAXIMUM_CHUNK_COUNT`, the chunk size is automatically increased:

```python
if num_chunks > MAXIMUM_CHUNK_COUNT:
    chunk_size = math.ceil(file_size / MAXIMUM_CHUNK_COUNT)
    log.warning("chunk_size_auto_adjusted", new_chunk_size=chunk_size)
```

---

## 3. Hashing Specification

### 3.1 Algorithm: SHA-256

| Property | Value |
|---|---|
| Algorithm | SHA-256 (FIPS 180-4) |
| Output Length | 256 bits (32 bytes) |
| Hex Digest Length | 64 characters |
| Collision Resistance | 2¹²⁸ (birthday attack bound) |
| Performance | ~500 MB/s on modern CPUs with SHA-NI extensions |
| Library | Python `hashlib` (OpenSSL backend) |

### 3.2 Streaming Hash Computation

Hashes are computed in a streaming fashion to avoid loading entire chunks into memory:

```python
async def compute_hash_streaming(
    stream: AsyncIterator[bytes],
    write_callback: Callable[[bytes], Awaitable[None]],
    buffer_size: int = 65_536  # 64 KB read buffer
) -> str:
    """
    Compute SHA-256 hash while streaming data through.
    
    Data flows: HTTP stream → hash update → disk write
    Memory usage is bounded by buffer_size regardless of chunk size.
    """
    hasher = hashlib.sha256()
    
    async for buffer in stream:
        hasher.update(buffer)
        await write_callback(buffer)
    
    return hasher.hexdigest()
```

### 3.3 Verification Levels

ChunkGuard employs a **two-tier verification strategy**:

#### Tier 1: Per-Chunk Verification (Mandatory)

Every chunk is hash-verified immediately after download:

```
Download Chunk → Compute SHA-256 → Compare with Expected Hash
                                        │
                                   ✅ Accept chunk
                                   ❌ Re-download chunk
```

**First Download (No Expected Hashes Available)**:

On the first download of a file where no chunk manifest is provided, ChunkGuard operates in "trust-on-first-download" mode:

1. Download each chunk and compute its hash
2. Store the computed hash in the state file
3. On any retry/re-download, verify against the stored hash
4. After assembly, verify whole-file hash against the expected hash (if provided)

#### Tier 2: Whole-File Verification (Configurable, Default: Enabled)

After chunk assembly, the entire file's SHA-256 is computed and compared:

```python
def verify_assembled_file(
    file_path: Path,
    expected_hash: str,
    buffer_size: int = 1_048_576  # 1 MB
) -> VerificationResult:
    hasher = hashlib.sha256()
    bytes_processed = 0
    
    with open(file_path, 'rb') as f:
        while True:
            data = f.read(buffer_size)
            if not data:
                break
            hasher.update(data)
            bytes_processed += len(data)
    
    computed_hash = hasher.hexdigest()
    return VerificationResult(
        expected=expected_hash,
        computed=computed_hash,
        is_valid=(computed_hash == expected_hash),
        bytes_verified=bytes_processed
    )
```

### 3.4 Hash Format Specification

All hashes are stored and compared as lowercase hexadecimal strings:

```
Format:   [a-f0-9]{64}
Example:  e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
          (SHA-256 of empty string)
```

Comparison is performed using constant-time comparison (`hmac.compare_digest`) to prevent timing attacks in security-sensitive scenarios.

---

## 4. State Management Specification

### 4.1 State File Schema (v1.0)

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "required": ["version", "download_id", "url", "file_size", "chunk_size", 
                "hash_algorithm", "output_path", "created_at", "status", "chunks"],
  "properties": {
    "version": {
      "type": "string",
      "const": "1.0",
      "description": "State file format version"
    },
    "download_id": {
      "type": "string",
      "format": "uuid",
      "description": "Unique identifier for this download"
    },
    "url": {
      "type": "string",
      "format": "uri",
      "description": "Source URL"
    },
    "file_size": {
      "type": "integer",
      "minimum": 0,
      "description": "Total file size in bytes"
    },
    "chunk_size": {
      "type": "integer",
      "minimum": 1048576,
      "maximum": 268435456,
      "description": "Chunk size in bytes"
    },
    "hash_algorithm": {
      "type": "string",
      "enum": ["sha256"],
      "description": "Hash algorithm used"
    },
    "expected_file_hash": {
      "type": ["string", "null"],
      "pattern": "^[a-f0-9]{64}$",
      "description": "Expected SHA-256 of complete file"
    },
    "etag": {
      "type": ["string", "null"],
      "description": "Server ETag for change detection"
    },
    "output_path": {
      "type": "string",
      "description": "Final output file path"
    },
    "created_at": {
      "type": "string",
      "format": "date-time"
    },
    "updated_at": {
      "type": "string",
      "format": "date-time"
    },
    "status": {
      "type": "string",
      "enum": ["PENDING", "IN_PROGRESS", "ASSEMBLING", "VERIFYING",
               "COMPLETE", "FAILED", "CANCELLED"]
    },
    "chunks": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["index", "start_byte", "end_byte", "status"],
        "properties": {
          "index": { "type": "integer", "minimum": 0 },
          "start_byte": { "type": "integer", "minimum": 0 },
          "end_byte": { "type": "integer", "minimum": 0 },
          "expected_hash": { "type": ["string", "null"] },
          "computed_hash": { "type": ["string", "null"] },
          "status": {
            "type": "string",
            "enum": ["PENDING", "DOWNLOADING", "COMPLETE", "FAILED", "ABANDONED"]
          },
          "attempts": { "type": "integer", "minimum": 0, "default": 0 },
          "last_error": { "type": ["string", "null"] },
          "started_at": { "type": ["string", "null"], "format": "date-time" },
          "completed_at": { "type": ["string", "null"], "format": "date-time" }
        }
      }
    },
    "statistics": {
      "type": "object",
      "properties": {
        "total_bytes_downloaded": { "type": "integer" },
        "chunks_complete": { "type": "integer" },
        "chunks_failed": { "type": "integer" },
        "chunks_pending": { "type": "integer" },
        "chunks_abandoned": { "type": "integer" },
        "elapsed_seconds": { "type": "number" },
        "average_chunk_speed_bps": { "type": "number" }
      }
    }
  }
}
```

### 4.2 State File Location

```
Output file:     /downloads/largefile.iso
State directory: /downloads/.chunkguard/
State file:      /downloads/.chunkguard/largefile.iso.state
Chunk directory: /downloads/.chunkguard/largefile.iso.chunks/
Chunk files:     /downloads/.chunkguard/largefile.iso.chunks/00000.chunk
                 /downloads/.chunkguard/largefile.iso.chunks/00001.chunk
                 ...
```

### 4.3 Atomic Write Protocol

```
Step 1: Serialize state to JSON string
Step 2: Write to temporary file:  {state_file}.tmp.{random_suffix}
Step 3: Flush file buffer:        file.flush()
Step 4: Sync to disk:             os.fsync(file.fileno())
Step 5: Atomic rename:            os.replace(tmp_path, state_file)
```

On Windows, `os.replace()` is atomic for NTFS. On Linux/macOS, `os.rename()` within the same filesystem is atomic per POSIX specification.

### 4.4 State Recovery from Crash

On resume, the state manager performs:

```
1. Load state file (or detect it's missing/corrupted)
2. For each chunk marked COMPLETE:
   a. Verify chunk file exists on disk
   b. Verify chunk file size matches expected size
   c. (Optional) Re-hash chunk file to verify integrity
   d. If verification fails → mark chunk as PENDING
3. For each chunk marked DOWNLOADING:
   a. Mark as PENDING (download was interrupted)
   b. Delete partial chunk file if it exists
4. Re-queue all PENDING and FAILED (< max retries) chunks
5. Resume download with existing state
```

---

## 5. Retry Specification

### 5.1 Retry Policy

```python
@dataclass
class RetryPolicy:
    max_attempts: int = 3           # Including the first attempt
    base_delay: float = 1.0         # Seconds
    max_delay: float = 60.0         # Seconds
    backoff_factor: float = 2.0     # Exponential multiplier
    jitter_factor: float = 0.5      # Random jitter as fraction of delay
    retryable_status_codes: set[int] = field(default_factory=lambda: {
        408,  # Request Timeout
        429,  # Too Many Requests
        500,  # Internal Server Error
        502,  # Bad Gateway
        503,  # Service Unavailable
        504,  # Gateway Timeout
    })
    retryable_exceptions: tuple = (
        ConnectionError,
        TimeoutError,
        ChunkHashMismatchError,
    )
```

### 5.2 Backoff Algorithm (with Jitter)

```python
import random

def compute_delay(attempt: int, policy: RetryPolicy) -> float:
    """
    Compute retry delay using decorrelated jitter strategy.
    
    This prevents "thundering herd" when multiple workers retry simultaneously.
    """
    # Exponential base
    exponential = policy.base_delay * (policy.backoff_factor ** attempt)
    
    # Cap at maximum
    capped = min(exponential, policy.max_delay)
    
    # Add jitter: uniform random between 0 and jitter_factor * capped
    jitter = random.uniform(0, policy.jitter_factor * capped)
    
    return capped + jitter
```

### 5.3 Retry Decision Tree

```
Error Occurred
     │
     ├─ Is error in retryable_exceptions?
     │       │
     │       ├─ YES → Is attempt < max_attempts?
     │       │            │
     │       │            ├─ YES → Compute delay → Sleep → Retry
     │       │            │
     │       │            └─ NO  → Mark ABANDONED → Log → Report
     │       │
     │       └─ NO  → Mark FAILED (non-retryable) → Log → Report
     │
     ├─ Is HTTP status in retryable_status_codes?
     │       │
     │       ├─ YES → Is Retry-After header present?
     │       │            │
     │       │            ├─ YES → Use Retry-After value as delay → Retry
     │       │            │
     │       │            └─ NO  → Use computed backoff delay → Retry
     │       │
     │       └─ NO  → Mark FAILED (non-retryable) → Log → Report
     │
     └─ Unknown error → Mark FAILED → Log full traceback → Report
```

---

## 6. Parallel Download Specification

### 6.1 Worker Pool

```python
async def download_with_pool(
    chunks: list[ChunkSpec],
    max_workers: int,
    client: httpx.AsyncClient,
    state: DownloadState,
) -> DownloadResult:
    """
    Download chunks using a bounded worker pool.
    
    Architecture:
      - asyncio.Semaphore limits concurrent downloads
      - asyncio.Queue distributes chunks to workers
      - Workers report results via callback
    """
    semaphore = asyncio.Semaphore(max_workers)
    queue = asyncio.Queue()
    
    # Populate queue with pending chunks
    for chunk in chunks:
        if chunk.status in (ChunkStatus.PENDING, ChunkStatus.FAILED):
            await queue.put(chunk)
    
    async def worker(worker_id: int):
        while True:
            try:
                chunk = queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            
            async with semaphore:
                result = await download_chunk(chunk, client)
                await handle_result(result, state, queue)
                queue.task_done()
    
    # Launch workers
    workers = [asyncio.create_task(worker(i)) for i in range(max_workers)]
    await asyncio.gather(*workers)
```

### 6.2 Connection Pool Configuration

```python
# httpx.AsyncClient configuration
client = httpx.AsyncClient(
    http2=True,                          # Enable HTTP/2 multiplexing
    limits=httpx.Limits(
        max_connections=max_workers + 2,  # Pool matches worker count + headroom
        max_keepalive_connections=max_workers,
        keepalive_expiry=30.0,            # Keep connections warm
    ),
    timeout=httpx.Timeout(
        connect=30.0,
        read=300.0,
        write=30.0,
        pool=60.0,
    ),
    follow_redirects=True,
    max_redirects=5,
)
```

### 6.3 Backpressure Mechanism

To prevent memory exhaustion when downloads are faster than disk writes:

```
                      High-water mark
                           │
Chunk Download ─────────── ▼ ──────── Disk Write
    Rate               Buffer          Rate
                     (bounded)
                         │
    If buffer > threshold:
      ├── Pause download (stop reading from socket)
      └── Resume when buffer drains below low-water mark
```

```python
WRITE_BUFFER_HIGH_WATER = 32 * 1024 * 1024   # 32 MB — pause downloads
WRITE_BUFFER_LOW_WATER  = 8 * 1024 * 1024    # 8 MB — resume downloads
```

---

## 7. File Assembly Specification

### 7.1 Assembly Algorithm

```python
async def assemble_file(
    state: DownloadState,
    config: DownloadConfig,
) -> AssemblyResult:
    """
    Concatenate verified chunks into final output file.
    
    Invariants:
      - Chunks are read in index order (0, 1, 2, ...)
      - Each chunk is re-verified during assembly (hash check)
      - Output file hash is computed incrementally during assembly
      - Assembly is atomic: temp file → rename
    """
    output_path = Path(state.output_path)
    temp_path = output_path.with_suffix('.chunkguard.assembling')
    
    file_hasher = hashlib.sha256()
    bytes_written = 0
    
    async with aiofiles.open(temp_path, 'wb') as output:
        for chunk in sorted(state.chunks, key=lambda c: c.index):
            chunk_path = get_chunk_path(state, chunk)
            
            # Read and verify chunk
            chunk_hasher = hashlib.sha256()
            async with aiofiles.open(chunk_path, 'rb') as chunk_file:
                while True:
                    data = await chunk_file.read(65_536)
                    if not data:
                        break
                    chunk_hasher.update(data)
                    file_hasher.update(data)
                    await output.write(data)
                    bytes_written += len(data)
            
            # Verify chunk hash during assembly
            if chunk.expected_hash:
                computed = chunk_hasher.hexdigest()
                if computed != chunk.expected_hash:
                    raise ChunkHashMismatchError(
                        chunk_index=chunk.index,
                        expected=chunk.expected_hash,
                        computed=computed,
                    )
    
    # Verify whole file hash
    computed_file_hash = file_hasher.hexdigest()
    
    if state.expected_file_hash:
        if computed_file_hash != state.expected_file_hash:
            raise FileHashMismatchError(
                expected=state.expected_file_hash,
                computed=computed_file_hash,
            )
    
    # Atomic rename
    os.replace(temp_path, output_path)
    
    return AssemblyResult(
        output_path=output_path,
        file_hash=computed_file_hash,
        bytes_written=bytes_written,
        is_verified=True,
    )
```

### 7.2 Post-Assembly Cleanup

```
After successful assembly:
  1. Delete all chunk files:     .chunkguard/file.chunks/*.chunk
  2. Delete chunk directory:     .chunkguard/file.chunks/
  3. Update state file status:   status → COMPLETE
  4. (Optional) Delete state:    Based on config.cleanup_on_complete
```

---

## 8. Progress Reporting Specification

### 8.1 Progress Data Model

```python
@dataclass
class ProgressReport:
    download_id: str
    timestamp: datetime
    
    # Byte-level progress
    total_bytes: int
    downloaded_bytes: int
    percentage: float               # 0.0 – 100.0
    
    # Chunk-level progress
    total_chunks: int
    chunks_complete: int
    chunks_in_progress: int
    chunks_failed: int
    chunks_pending: int
    
    # Speed metrics
    current_speed_bps: float        # Bytes per second (instantaneous)
    average_speed_bps: float        # Bytes per second (overall average)
    
    # Time estimates
    elapsed_seconds: float
    estimated_remaining_seconds: float
    estimated_completion_time: datetime
    
    # Active workers
    active_workers: int
```

### 8.2 Speed Calculation

```python
# Exponential moving average for smooth speed display
SPEED_ALPHA = 0.3  # Smoothing factor (0 = very smooth, 1 = instant)

current_speed = SPEED_ALPHA * instant_speed + (1 - SPEED_ALPHA) * previous_speed
```

### 8.3 CLI Progress Display

```
ChunkGuard v1.0.0 — Downloading largefile.iso

  ████████████████████░░░░░░░░░░  68.2%

  Downloaded:   69.1 GB / 100.0 GB
  Speed:        145.3 MB/s (avg: 128.7 MB/s)
  Chunks:       8714 / 12800 complete (3 failed, 4083 pending)
  Workers:      4/4 active
  Elapsed:      8m 57s
  ETA:          4m 12s
  
  Recent: chunk 8714 ✅ (142ms)  chunk 8713 ✅ (156ms)  chunk 8710 ⚠️ retry 2/3
```

---

## 9. Configuration Specification

### 9.1 Default Configuration File

```yaml
# chunkguard.yaml — Default Configuration

download:
  chunk_size: "8MB"                    # 1MB – 256MB; supports units: KB, MB, GB
  max_parallel_workers: 4              # 1 – 32
  verify_on_complete: true             # Whole-file SHA-256 after assembly
  cleanup_chunks_on_complete: true     # Delete chunk files after success
  pre_check_disk_space: true           # Verify available disk before download

network:
  connect_timeout: 30                  # Seconds
  read_timeout: 300                    # Seconds
  max_redirects: 5
  user_agent: "ChunkGuard/1.0"
  http2: true                          # Enable HTTP/2
  verify_ssl: true                     # TLS certificate verification
  max_bandwidth: 0                     # Bytes/sec, 0 = unlimited

retry:
  max_attempts: 3                      # Per chunk
  base_delay: 1.0                      # Seconds
  max_delay: 60.0                      # Seconds
  backoff_factor: 2.0
  jitter_factor: 0.5

logging:
  level: "INFO"                        # DEBUG, INFO, WARNING, ERROR
  format: "json"                       # json, text
  file: null                           # Path to log file, null = stderr

progress:
  update_interval: 0.5                 # Seconds between progress updates
  show_speed: true
  show_eta: true
  show_chunk_details: false            # Show individual chunk events
```

### 9.2 Size String Parsing

```python
SIZE_UNITS = {
    'B': 1,
    'KB': 1024,
    'MB': 1024 ** 2,
    'GB': 1024 ** 3,
    'TB': 1024 ** 4,
}

def parse_size(size_str: str) -> int:
    """Parse human-readable size string to bytes.
    
    Examples:
        '8MB'  → 8388608
        '1GB'  → 1073741824
        '512KB' → 524288
    """
    match = re.match(r'^(\d+(?:\.\d+)?)\s*(B|KB|MB|GB|TB)$', size_str.upper())
    if not match:
        raise ConfigurationError(f"Invalid size format: {size_str}")
    return int(float(match.group(1)) * SIZE_UNITS[match.group(2)])
```

---

## 10. Error Handling Specification

### 10.1 Error Categories

| Category | Examples | Retry? | User Action |
|---|---|---|---|
| **Configuration** | Invalid chunk size, bad URL format | No | Fix configuration |
| **Network Transient** | Timeout, connection reset, DNS failure | Yes | Automatic retry |
| **Network Permanent** | 404 Not Found, 403 Forbidden | No | Check URL / credentials |
| **Integrity** | Hash mismatch, truncated chunk | Yes | Automatic re-download |
| **Storage** | Disk full, permission denied | No | Free space / fix permissions |
| **State** | Corrupted state file | Partial | May need to restart download |
| **Server** | Range not supported, file changed | No | Fall back or restart |

### 10.2 Error Response Format

All errors include structured context for debugging:

```json
{
  "error_type": "ChunkHashMismatchError",
  "message": "Chunk 42 hash verification failed",
  "context": {
    "chunk_index": 42,
    "start_byte": 352321536,
    "end_byte": 360710143,
    "expected_hash": "a1b2c3d4e5f6...",
    "computed_hash": "9f8e7d6c5b4a...",
    "attempt": 2,
    "url": "https://example.com/file.iso"
  },
  "is_retryable": true,
  "timestamp": "2026-01-15T10:35:42.123Z"
}
```

---

## 11. Security Specification

### 11.1 TLS Requirements

- TLS 1.2+ required (TLS 1.0/1.1 rejected)
- Certificate verification enabled by default
- Certificate pinning available via configuration

### 11.2 Hash Security

- SHA-256 is the minimum acceptable hash algorithm
- Hash comparisons use constant-time comparison (`hmac.compare_digest`)
- No support for weak algorithms (MD5, SHA-1) even in non-security contexts

### 11.3 File Permissions

```python
# Chunk files: owner read/write only
CHUNK_FILE_PERMISSIONS = 0o600

# State files: owner read/write only
STATE_FILE_PERMISSIONS = 0o600

# Output file: follows umask (typically 0o644)
OUTPUT_FILE_PERMISSIONS = None  # Use system default
```

---

## 12. Platform Compatibility

| Platform | Python Version | File System | Atomic Rename | Tested |
|---|---|---|---|---|
| Linux (x86_64) | 3.10+ | ext4, XFS, Btrfs | ✅ `os.replace()` | ✅ |
| macOS (arm64) | 3.10+ | APFS, HFS+ | ✅ `os.replace()` | ✅ |
| Windows 10+ (x86_64) | 3.10+ | NTFS | ✅ `os.replace()` | ✅ |
| Windows (FAT32) | 3.10+ | FAT32 | ⚠️ Non-atomic | ⚠️ Limited |

### Large File Support

- Files > 2 GB: Supported on all 64-bit platforms
- Files > 4 GB: Requires 64-bit Python and file system support (NTFS, ext4, APFS)
- Maximum tested file size: 1 TB
