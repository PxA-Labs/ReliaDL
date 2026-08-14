# System Architecture — ChunkGuard

> **Audience**: Software Engineers, Architects, Technical Leads
> **Reading time**: ~20 minutes

---

## 1. Architectural Philosophy

ChunkGuard follows these design principles:

| Principle | Application |
|---|---|
| **Fail-Safe Defaults** | Every operation assumes failure is imminent; success is verified, never assumed |
| **Idempotency** | Any operation can be safely retried without side effects |
| **Separation of Concerns** | Each module has exactly one responsibility |
| **Observable by Default** | Every significant event is logged with structured context |
| **Configuration over Convention** | All tunable parameters are externalized to config |
| **Graceful Degradation** | If advanced features (parallelism, range requests) are unavailable, fall back to simpler modes |

---

## 2. High-Level Architecture

```
┌──────────────────────────────────────────────────────────────────────────┐
│                              CLI Interface                               │
│                          (main.py / click)                               │
├──────────────────────────────────────────────────────────────────────────┤
│                                                                          │
│  ┌────────────────┐    ┌──────────────────┐    ┌──────────────────────┐  │
│  │  Config Layer   │    │  Download Engine  │    │   State Manager     │  │
│  │  (config.py)    │───▶│  (download_      │◀──▶│   (state_           │  │
│  │                 │    │   engine.py)      │    │    manager.py)      │  │
│  └────────────────┘    └────────┬─────────┘    └──────────┬───────────┘  │
│                                 │                          │              │
│                    ┌────────────┼────────────┐             │              │
│                    ▼            ▼            ▼             │              │
│              ┌──────────┐ ┌──────────┐ ┌──────────┐       │              │
│              │ Worker 1 │ │ Worker 2 │ │ Worker N │       │              │
│              │ (async)  │ │ (async)  │ │ (async)  │       │              │
│              └─────┬────┘ └─────┬────┘ └─────┬────┘       │              │
│                    │            │            │             │              │
│                    ▼            ▼            ▼             │              │
│              ┌─────────────────────────────────────┐      │              │
│              │         Chunk Manager               │      │              │
│              │         (chunk_manager.py)           │      │              │
│              └─────────────────┬───────────────────┘      │              │
│                                │                          │              │
│                    ┌───────────┴───────────┐              │              │
│                    ▼                       ▼              │              │
│              ┌──────────────┐       ┌──────────────┐      │              │
│              │ Hash Verifier│       │ Retry Handler│      │              │
│              │ (hash_       │       │ (retry_      │      │              │
│              │  verifier.py)│       │  handler.py) │      │              │
│              └──────────────┘       └──────────────┘      │              │
│                                                           │              │
│              ┌────────────────────────────────────────────┘              │
│              ▼                                                           │
│        ┌──────────────┐                                                  │
│        │File Assembler │                                                 │
│        │(file_         │                                                 │
│        │ assembler.py) │                                                 │
│        └──────────────┘                                                  │
│                                                                          │
├──────────────────────────────────────────────────────────────────────────┤
│                          Cross-Cutting Concerns                          │
│              ┌──────────┐  ┌──────────┐  ┌──────────────┐               │
│              │ Logger   │  │ Models   │  │ Exceptions   │               │
│              │(logger.py│  │(models.py│  │(exceptions.py│               │
│              │)         │  │)         │  │)             │               │
│              └──────────┘  └──────────┘  └──────────────┘               │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 CLI Interface (`main.py`)

**Responsibility**: Parse user commands, validate inputs, orchestrate the download pipeline.

| Command | Purpose | Key Inputs |
|---|---|---|
| `download` | Start a new download | URL, output path, chunk size, parallelism |
| `resume` | Resume an interrupted download | State file path |
| `verify` | Verify a downloaded file | File path, expected hash |
| `status` | Show download progress | State file path |

**Design Decisions**:
- Uses `click` for declarative CLI definition with auto-generated `--help`
- All commands are idempotent — running `download` on an existing state file resumes instead of restarting
- Exit codes follow Unix conventions (0 = success, 1 = error, 2 = usage error)

---

### 3.2 Configuration Layer (`config.py`)

**Responsibility**: Load, validate, and provide type-safe access to all configuration parameters.

```python
@dataclass
class DownloadConfig:
    chunk_size_bytes: int          # Default: 8_388_608 (8 MB)
    max_parallel_workers: int      # Default: 4
    max_retries_per_chunk: int     # Default: 3
    retry_base_delay_seconds: float # Default: 1.0
    retry_max_delay_seconds: float  # Default: 60.0
    retry_backoff_factor: float    # Default: 2.0
    connect_timeout_seconds: float  # Default: 30.0
    read_timeout_seconds: float     # Default: 300.0
    hash_algorithm: str             # Default: "sha256"
    state_directory: str            # Default: ".chunkguard"
    verify_on_complete: bool        # Default: True
    progress_update_interval: float # Default: 0.5
    user_agent: str                 # Default: "ChunkGuard/1.0"
    max_bandwidth_bytes_per_sec: int # Default: 0 (unlimited)
```

**Configuration Precedence** (highest to lowest):
1. CLI arguments
2. Environment variables (`CHUNKGUARD_CHUNK_SIZE`, etc.)
3. Project config file (`./chunkguard.yaml`)
4. User config file (`~/.config/chunkguard/config.yaml`)
5. Built-in defaults

---

### 3.3 Download Engine (`download_engine.py`)

**Responsibility**: Orchestrate the entire download lifecycle — metadata fetch, chunk planning, parallel dispatch, and completion detection.

#### Lifecycle Phases

```
INITIALIZE → METADATA → PLAN → DOWNLOAD → VERIFY → ASSEMBLE → FINALIZE
     │           │         │        │          │         │          │
     │           │         │        │          │         │          └─ Cleanup temp files
     │           │         │        │          │         └─ Concatenate chunks
     │           │         │        │          └─ Per-chunk hash check
     │           │         │        └─ Parallel HTTP Range GETs
     │           │         └─ Divide file into chunk specs
     │           └─ HEAD request for size & range support
     └─ Load/create state file
```

#### Worker Pool Design

```
                    ┌─────────────────────┐
                    │   Download Engine    │
                    │                     │
                    │  ┌───────────────┐  │
                    │  │  Chunk Queue   │  │
                    │  │  (asyncio.Q)   │  │
                    │  └───────┬───────┘  │
                    │          │          │
                    │    ┌─────┼─────┐    │
                    │    ▼     ▼     ▼    │
                    │  ┌───┐┌───┐┌───┐   │
                    │  │W1 ││W2 ││W3 │   │
                    │  └─┬─┘└─┬─┘└─┬─┘   │
                    │    │    │    │      │
                    │    ▼    ▼    ▼      │
                    │  ┌───────────────┐  │
                    │  │ Result Queue  │  │
                    │  └───────────────┘  │
                    └─────────────────────┘
```

- **Chunk Queue**: Populated with pending/failed chunks; workers consume from it
- **Workers**: Async coroutines that download a chunk, hash it, report result
- **Result Queue**: Workers report success/failure; engine updates state
- **Backpressure**: If disk I/O is slow, workers pause to prevent memory exhaustion

---

### 3.4 Chunk Manager (`chunk_manager.py`)

**Responsibility**: Compute chunk boundaries, track chunk lifecycle, provide chunk metadata.

#### Chunk Calculation Algorithm

```
Given:
  file_size = 107_374_182_400  (100 GB)
  chunk_size = 8_388_608       (8 MB)

Computed:
  total_chunks = ceil(file_size / chunk_size) = 12,800
  
  Chunk[0]:  bytes 0 – 8,388,607          (8 MB)
  Chunk[1]:  bytes 8,388,608 – 16,777,215 (8 MB)
  ...
  Chunk[12799]: bytes 107,374,174,208 – 107,374,182,399  (last chunk, possibly smaller)
```

#### Chunk States

```
                ┌──────────┐
                │ PENDING  │ ◄── Initial state
                └────┬─────┘
                     │
                     ▼
               ┌───────────┐
               │DOWNLOADING│ ◄── Worker picked up chunk
               └─────┬─────┘
                     │
              ┌──────┴──────┐
              ▼              ▼
        ┌──────────┐  ┌──────────┐
        │ COMPLETE │  │  FAILED  │
        │(verified)│  │(hash bad │
        └──────────┘  │ or error)│
                      └────┬─────┘
                           │
                           ▼ (retry ≤ max)
                      ┌──────────┐
                      │ PENDING  │ ◄── Re-queued for retry
                      └──────────┘
                           │
                           ▼ (retry > max)
                      ┌──────────┐
                      │ABANDONED │ ◄── Max retries exceeded
                      └──────────┘
```

---

### 3.5 Hash Verifier (`hash_verifier.py`)

**Responsibility**: Compute and verify SHA-256 hashes for chunks and whole files.

#### Design Decisions

| Decision | Rationale |
|---|---|
| **SHA-256 (not MD5/SHA-1)** | MD5 and SHA-1 have known collision attacks; SHA-256 is the minimum secure choice |
| **Streaming hash computation** | Hashing is done as bytes arrive, not after full chunk is in memory — bounded memory usage |
| **Separate verify pass** | Hash is computed during download AND verified independently after write — catches disk corruption |
| **Hex digest format** | Lowercase hex string (64 chars for SHA-256) — human-readable, diff-friendly |

#### Hash Pipeline

```
HTTP Response Stream
        │
        ├──► hash_context.update(chunk_bytes)   ← streaming hash
        │
        └──► disk_write(chunk_bytes)            ← write to temp file
        
After all bytes received:
        │
        ├──► computed_hash = hash_context.hexdigest()
        │
        └──► Compare: computed_hash == expected_hash?
                │
                ├── ✅ Match → chunk verified
                └── ❌ Mismatch → chunk failed
```

---

### 3.6 State Manager (`state_manager.py`)

**Responsibility**: Persist download state to disk for crash recovery and resumability.

#### State File Format

```json
{
  "version": "1.0",
  "download_id": "uuid-v4",
  "url": "https://example.com/file.iso",
  "file_size": 107374182400,
  "chunk_size": 8388608,
  "hash_algorithm": "sha256",
  "expected_file_hash": "abcdef1234...",
  "output_path": "/downloads/file.iso",
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:35:42Z",
  "status": "IN_PROGRESS",
  "chunks": [
    {
      "index": 0,
      "start_byte": 0,
      "end_byte": 8388607,
      "expected_hash": "a1b2c3d4...",
      "status": "COMPLETE",
      "attempts": 1,
      "completed_at": "2026-01-15T10:30:05Z"
    },
    {
      "index": 1,
      "start_byte": 8388608,
      "end_byte": 16777215,
      "expected_hash": "e5f6a7b8...",
      "status": "FAILED",
      "attempts": 3,
      "last_error": "HashMismatchError: expected e5f6..., got 9a8b..."
    }
  ],
  "statistics": {
    "total_bytes_downloaded": 85899345920,
    "chunks_complete": 10200,
    "chunks_failed": 3,
    "chunks_pending": 2597,
    "elapsed_seconds": 342.5
  }
}
```

#### Atomic Write Strategy

To prevent state file corruption on crash:

```
1. Write new state to temporary file:   .chunkguard/file.iso.state.tmp
2. Sync to disk:                         fsync(fd)
3. Atomic rename:                        rename(.tmp → .state)
```

This guarantees the state file is always either the old complete version or the new complete version — never a partial write.

#### State Persistence Frequency

- After every chunk completion/failure
- Every 5 seconds during active downloads (progress snapshot)
- On graceful shutdown (SIGINT/SIGTERM handler)

---

### 3.7 Retry Handler (`retry_handler.py`)

**Responsibility**: Implement retry policy with exponential backoff and jitter.

#### Backoff Formula

```
delay = min(
    base_delay × (backoff_factor ^ attempt_number) + random_jitter,
    max_delay
)

Where:
  base_delay    = 1.0 seconds (configurable)
  backoff_factor = 2.0 (configurable)
  max_delay     = 60.0 seconds (configurable)
  random_jitter = uniform(0, 0.5 × computed_delay)
```

#### Retry Timeline Example

| Attempt | Base Delay | With Jitter (example) | Cumulative Wait |
|---|---|---|---|
| 1 | 1.0s | 1.3s | 1.3s |
| 2 | 2.0s | 2.7s | 4.0s |
| 3 | 4.0s | 5.1s | 9.1s |
| 4 (abandoned) | — | — | — |

#### Retryable vs Non-Retryable Errors

| Retryable | Non-Retryable |
|---|---|
| HTTP 500, 502, 503, 504 | HTTP 400, 401, 403, 404 |
| Connection timeout | Invalid URL |
| Connection reset | Disk full |
| Hash mismatch | Permission denied |
| DNS temporary failure | SSL certificate error (configurable) |

---

### 3.8 File Assembler (`file_assembler.py`)

**Responsibility**: Concatenate verified chunks into the final output file and perform whole-file integrity verification.

#### Assembly Process

```
Chunk Files on Disk:
  .chunkguard/chunks/00000.chunk  (8 MB, verified ✅)
  .chunkguard/chunks/00001.chunk  (8 MB, verified ✅)
  .chunkguard/chunks/00002.chunk  (8 MB, verified ✅)
  ...
  .chunkguard/chunks/12799.chunk  (partial, verified ✅)

Assembly:
  1. Open output file for writing
  2. For i in 0..12799:
       a. Read chunk[i] from disk
       b. Append to output file
       c. Update running SHA-256 hash
  3. Finalize hash
  4. Compare with expected whole-file hash
  5. If match: delete chunk files, mark complete
  6. If mismatch: identify which chunks need re-verification
```

---

## 4. Cross-Cutting Concerns

### 4.1 Data Models (`models.py`)

All data structures are defined as immutable dataclasses or Pydantic models:

- `ChunkSpec` — defines a chunk's byte range and expected hash
- `ChunkResult` — the outcome of downloading a chunk (success/failure + metadata)
- `DownloadState` — complete snapshot of a download's progress
- `DownloadStatistics` — aggregated metrics (speed, ETA, completion %)

### 4.2 Exception Hierarchy (`exceptions.py`)

```
ChunkGuardError (base)
├── ConfigurationError
├── NetworkError
│   ├── ConnectionError
│   ├── TimeoutError
│   └── HTTPError
│       ├── ClientError (4xx)
│       └── ServerError (5xx)
├── IntegrityError
│   ├── ChunkHashMismatchError
│   └── FileHashMismatchError
├── StateError
│   ├── StateCorruptedError
│   └── StateNotFoundError
├── StorageError
│   ├── DiskFullError
│   └── PermissionError
└── AssemblyError
```

### 4.3 Logging (`logger.py`)

Structured JSON logging via `structlog`:

```json
{
  "timestamp": "2026-01-15T10:30:05.123Z",
  "level": "info",
  "event": "chunk_download_complete",
  "download_id": "abc-123",
  "chunk_index": 42,
  "chunk_size_bytes": 8388608,
  "duration_ms": 1523,
  "throughput_mbps": 44.1,
  "hash_verified": true
}
```

---

## 5. Concurrency Model

### Why Async I/O (Not Threads)?

| Factor | Threads | Async I/O (chosen) |
|---|---|---|
| Memory per worker | ~8 MB stack per thread | ~few KB per coroutine |
| Context switching | OS-level, expensive | User-level, cheap |
| I/O bound workloads | Wastes CPU on blocking | Perfect fit — yields during I/O |
| GIL impact (Python) | Limits CPU parallelism | Irrelevant — I/O bound |
| Scalability | ~100s of threads max | ~10,000s of coroutines |
| Debugging | Race conditions, deadlocks | Sequential reasoning within coroutine |

### Concurrency Architecture

```
┌─────────────────────────────────────────┐
│            asyncio Event Loop            │
│                                         │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐ │
│  │Coroutine│  │Coroutine│  │Coroutine│ │
│  │Worker 1 │  │Worker 2 │  │Worker 3 │ │
│  └────┬────┘  └────┬────┘  └────┬────┘ │
│       │            │            │       │
│       ▼            ▼            ▼       │
│  ┌──────────────────────────────────┐   │
│  │     httpx.AsyncClient            │   │
│  │     (connection pool)            │   │
│  └──────────────────────────────────┘   │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │     aiofiles                     │   │
│  │     (async disk I/O)             │   │
│  └──────────────────────────────────┘   │
│                                         │
│  ┌──────────────────────────────────┐   │
│  │     Semaphore (max_workers)      │   │
│  │     Controls concurrency level   │   │
│  └──────────────────────────────────┘   │
└─────────────────────────────────────────┘
```

---

## 6. Key Design Decisions

### Decision Record

| # | Decision | Alternatives Considered | Rationale |
|---|---|---|---|
| D1 | SHA-256 for hashing | MD5, SHA-1, SHA-512, BLAKE3 | SHA-256 balances security and performance; hardware-accelerated; universal tooling support |
| D2 | JSON for state files | SQLite, msgpack, protobuf | Human-readable; debuggable; no binary dependencies; sufficient performance |
| D3 | 8 MB default chunk size | 1 MB, 4 MB, 16 MB, 64 MB | Balances overhead (too many small chunks) vs granularity (too few large chunks); fits typical TCP window |
| D4 | asyncio over threading | threading, multiprocessing | I/O-bound workload; lower memory; no GIL issues; better at scale |
| D5 | Per-chunk hash in manifest | Hash only whole file | Enables selective re-download; pinpoints corruption location |
| D6 | Atomic state file writes | Append-only log, database | Simple; crash-safe; no external dependencies |
| D7 | HTTP Range requests | Custom chunking protocol | Works with any standard HTTP server; CDN-compatible |

---

## 7. Failure Modes & Recovery

| Failure Mode | Detection | Recovery |
|---|---|---|
| Network drops mid-chunk | Connection error / timeout | Retry chunk with backoff |
| Chunk hash mismatch | SHA-256 comparison | Delete chunk, re-download |
| Process crash | State file persists | Resume from state file on restart |
| Disk full | `OSError: ENOSPC` | Pause download, alert user, retry after space freed |
| Server returns wrong bytes | Hash mismatch | Re-download chunk |
| State file corrupted | JSON parse error / CRC check | Rebuild state from existing chunk files on disk |
| Server stops supporting Range | HTTP 200 (not 206) | Fall back to single-stream download |
| All retries exhausted | Attempt counter > max | Mark chunk ABANDONED, report to user |

---

## 8. Security Considerations

See [SECURITY.md](SECURITY.md) for the complete threat model.

**Summary**:
- SHA-256 prevents undetected corruption and man-in-the-middle data tampering (when combined with HTTPS)
- State files contain no credentials
- Chunk files are stored with user-only permissions (0600)
- No execution of downloaded content
- TLS certificate verification is enabled by default

---

## 9. Extensibility Points

The architecture is designed for future extension:

| Extension Point | Mechanism | Future Use |
|---|---|---|
| Protocol Adapters | Abstract `Downloader` interface | S3, Azure Blob, GCS, FTP |
| Hash Algorithms | Pluggable `HashProvider` | BLAKE3, SHA-512, CRC32 (low-security mode) |
| Progress Reporters | Observer pattern | Web UI, Slack notifications, webhook |
| Storage Backends | Abstract `Storage` interface | Cloud storage, NFS, memory (testing) |
| Authentication | Header injection middleware | OAuth, API keys, custom auth |
