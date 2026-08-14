# Error Handling & Recovery — ChunkGuard

> **Audience**: Software Engineers, SREs
> **Reading time**: ~10 minutes

---

## 1. Error Taxonomy

### 1.1 Exception Hierarchy

```
ChunkGuardError (base)
│
├── ConfigurationError
│   Description: Invalid or missing configuration values
│   Retryable:   No
│   User Action:  Fix configuration file or CLI arguments
│   Examples:     chunk_size < 1MB, invalid URL format, workers > 32
│
├── NetworkError
│   ├── ConnectionError
│   │   Description: Unable to establish TCP connection
│   │   Retryable:   Yes
│   │   Examples:     DNS resolution failure, connection refused, network unreachable
│   │
│   ├── TimeoutError
│   │   Description: Operation exceeded time limit
│   │   Retryable:   Yes
│   │   Examples:     Connect timeout (30s), read timeout (300s)
│   │
│   └── HTTPError
│       ├── ClientError (4xx)
│       │   Description: Client-side request problem
│       │   Retryable:   No (except 408, 429)
│       │   Examples:     404 Not Found, 403 Forbidden, 401 Unauthorized
│       │
│       └── ServerError (5xx)
│           Description: Server-side processing problem
│           Retryable:   Yes
│           Examples:     500 Internal Error, 502 Bad Gateway, 503 Unavailable
│
├── IntegrityError
│   ├── ChunkHashMismatchError
│   │   Description: Downloaded chunk's SHA-256 doesn't match expected value
│   │   Retryable:   Yes
│   │   Data:        chunk_index, expected_hash, computed_hash
│   │   Recovery:    Delete chunk file, re-download
│   │
│   └── FileHashMismatchError
│       Description: Assembled file's SHA-256 doesn't match expected value
│       Retryable:   Partial (re-verify and re-download bad chunks)
│       Data:        expected_hash, computed_hash
│       Recovery:    Re-verify each chunk, re-download mismatched ones
│
├── StateError
│   ├── StateNotFoundError
│   │   Description: Requested state file doesn't exist
│   │   Retryable:   No
│   │   Recovery:    Start a new download instead of resume
│   │
│   └── StateCorruptedError
│       Description: State file is unreadable or structurally invalid
│       Retryable:   Partial
│       Recovery:    Attempt to rebuild state from chunk files on disk
│
├── StorageError
│   ├── DiskFullError
│   │   Description: No space left on device (ENOSPC)
│   │   Retryable:   No (until space is freed)
│   │   Recovery:    Pause download, alert user, resume after space freed
│   │
│   └── PermissionError
│       Description: Cannot read/write to path
│       Retryable:   No
│       Recovery:    Fix file/directory permissions
│
└── AssemblyError
    Description: Error during chunk concatenation
    Retryable:   Partial
    Recovery:    Re-verify chunks, retry assembly
```

---

## 2. Error Handling Strategy by Component

### 2.1 Download Engine

| Error | Handling |
|---|---|
| HEAD request fails | Retry up to 3 times → fall back to blind download (unknown file size) |
| Server doesn't support Range | Log warning, fall back to single-stream download |
| File changed during download (ETag mismatch) | Abort, restart with fresh state |
| All workers fail simultaneously | Pause, retry all after backoff period |
| Progress callback throws exception | Log warning, disable callback, continue download |

### 2.2 Per-Chunk Download

| Error | Handling |
|---|---|
| Connection drops mid-chunk | Discard partial data, mark FAILED, retry |
| Timeout waiting for data | Close connection, mark FAILED, retry |
| HTTP 429 Too Many Requests | Use `Retry-After` header or 60s backoff |
| HTTP 5xx | Exponential backoff retry |
| HTTP 4xx (not 408/429) | Mark ABANDONED immediately (non-retryable) |
| Hash mismatch | Delete chunk file, mark FAILED, retry |
| Chunk size mismatch | Treat as corruption, mark FAILED, retry |

### 2.3 State Management

| Error | Handling |
|---|---|
| State file write fails | Retry write to different temp file name |
| Atomic rename fails | Fall back to write-in-place (less safe) |
| State file JSON parse error | Attempt recovery from backup state file |
| State file version mismatch | Attempt migration; if impossible, error out |

### 2.4 File Assembly

| Error | Handling |
|---|---|
| Missing chunk file | Mark chunk PENDING, re-download before retry assembly |
| Chunk hash fails during assembly | Mark chunk FAILED, re-download, retry assembly |
| Disk full during assembly | Pause, alert user, cleanup temp files |
| Whole-file hash mismatch | Re-verify all chunks to find the corrupted one(s) |

---

## 3. Error Recovery Procedures

### 3.1 Automatic Recovery (No User Intervention)

```
┌─────────────────────────────────────────────────────────────────┐
│                    Automatic Recovery Flow                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Error detected                                                  │
│       │                                                          │
│       ▼                                                          │
│  Is error retryable?                                             │
│       │                                                          │
│  ┌────┴────┐                                                     │
│  YES       NO → escalate to user                                 │
│  │                                                               │
│  ▼                                                               │
│  attempt < max_retries?                                          │
│  │                                                               │
│  ┌────┴────┐                                                     │
│  YES       NO → mark ABANDONED, continue with other chunks       │
│  │                                                               │
│  ▼                                                               │
│  Compute backoff delay                                           │
│  │                                                               │
│  ▼                                                               │
│  Sleep (delay with jitter)                                       │
│  │                                                               │
│  ▼                                                               │
│  Retry operation                                                 │
│  │                                                               │
│  ▼                                                               │
│  Log outcome (success/failure + attempt number)                  │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 Manual Recovery Procedures

| Scenario | Recovery Steps |
|---|---|
| **All retries exhausted** | 1. Check network connectivity<br>2. Check server status<br>3. Run `chunkguard resume state_file` to retry failed chunks |
| **State file corrupted** | 1. Check if `.state.bak` backup exists<br>2. If not, delete state file and chunk dir<br>3. Start fresh download |
| **Disk full** | 1. Free disk space<br>2. Run `chunkguard resume state_file` |
| **File changed on server** | 1. Delete state file and chunks<br>2. Start fresh download with new URL/hash |
| **Whole-file hash mismatch** | 1. Run `chunkguard resume state_file` (auto-detects and re-downloads bad chunks)<br>2. If persists, start fresh download |

---

## 4. Error Logging

### 4.1 Log Levels by Error Type

| Error Type | Log Level | Included Context |
|---|---|---|
| Retryable error (attempt N of M) | `WARNING` | error_type, chunk_index, attempt, max_attempts, next_retry_delay |
| Non-retryable error | `ERROR` | error_type, chunk_index, message, stack_trace |
| Chunk abandoned (max retries) | `ERROR` | chunk_index, total_attempts, all_error_messages |
| Download failed | `CRITICAL` | download_id, total_chunks, failed_chunks, abandoned_chunks |
| Automatic recovery succeeded | `INFO` | chunk_index, recovery_type, attempt_number |

### 4.2 Error Log Example

```json
{
  "timestamp": "2026-01-15T10:35:42.123Z",
  "level": "warning",
  "event": "chunk_download_failed_retrying",
  "download_id": "abc-123",
  "chunk_index": 42,
  "attempt": 2,
  "max_attempts": 3,
  "error_type": "ChunkHashMismatchError",
  "error_message": "Hash mismatch: expected a1b2c3, got 9f8e7d",
  "expected_hash": "a1b2c3d4e5f6...",
  "computed_hash": "9f8e7d6c5b4a...",
  "chunk_size_bytes": 8388608,
  "retry_delay_seconds": 2.7,
  "next_attempt_at": "2026-01-15T10:35:44.823Z"
}
```

---

## 5. Graceful Shutdown

### 5.1 Signal Handling

| Signal | Behavior |
|---|---|
| `SIGINT` (Ctrl+C, first) | Graceful shutdown: finish active chunks, save state, exit |
| `SIGINT` (Ctrl+C, second) | Force shutdown: cancel active downloads, save state, exit |
| `SIGTERM` | Same as first SIGINT — graceful shutdown |
| `SIGKILL` | Process killed — state file should be recoverable from last save |

### 5.2 Graceful Shutdown Sequence

```
Signal received (SIGINT / SIGTERM)
       │
       ▼
  Set shutdown_requested = True
       │
       ▼
  Stop accepting new chunk work
       │
       ▼
  Wait for active chunk downloads to complete
  (up to read_timeout seconds)
       │
       ▼
  Save current state to state file
       │
       ▼
  Close HTTP client connection pool
       │
       ▼
  Log shutdown summary:
    - Chunks completed this session
    - Chunks still pending
    - Resume command for user
       │
       ▼
  Exit with code 10 (CANCELLED)
```

---

## 6. Monitoring & Alerting Integration

### 6.1 Key Metrics for Alerting

| Metric | Alert Threshold | Severity |
|---|---|---|
| Chunk failure rate | > 10% of chunks failed in last 5 min | Warning |
| Chunk abandoned count | Any chunk reaches max retries | Error |
| Download stalled | No progress for > 5 minutes | Warning |
| State file write failure | Any failure | Error |
| Disk space remaining | < 2x remaining download size | Warning |
| Whole-file hash mismatch | Any occurrence | Critical |

### 6.2 Health Check Endpoint (Future)

```json
GET /health
{
  "status": "healthy",
  "active_downloads": 1,
  "total_chunks_pending": 4083,
  "total_chunks_complete": 8714,
  "total_chunks_failed": 3,
  "disk_space_available_bytes": 536870912000,
  "uptime_seconds": 3600
}
```
