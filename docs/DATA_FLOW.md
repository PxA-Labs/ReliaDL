# Data Flow & State Machine — ChunkGuard

> **Audience**: Software Engineers
> **Reading time**: ~12 minutes

---

## 1. End-to-End Data Flow

### 1.1 Complete Download Sequence Diagram

```
  User            CLI          Download      Chunk        HTTP       Hash       State       File
                               Engine       Manager     Client     Verifier   Manager    Assembler
   │                │              │            │           │          │          │           │
   │  download cmd  │              │            │           │          │          │           │
   │───────────────▶│              │            │           │          │          │           │
   │                │  start()     │            │           │          │          │           │
   │                │─────────────▶│            │           │          │          │           │
   │                │              │            │           │          │          │           │
   │                │              │ ──── Phase 1: Initialize ────    │          │           │
   │                │              │            │           │          │          │           │
   │                │              │  load_or_create_state()│          │          │           │
   │                │              │────────────────────────────────────────────▶│           │
   │                │              │◀───────────────────────────────────────────│           │
   │                │              │            │           │          │          │           │
   │                │              │ ──── Phase 2: Metadata ─────    │          │           │
   │                │              │            │           │          │          │           │
   │                │              │            │  HEAD url  │          │          │           │
   │                │              │────────────────────────▶│          │          │           │
   │                │              │◀────────────────────────│          │          │           │
   │                │              │   (file_size, etag,     │          │          │           │
   │                │              │    accept_ranges)       │          │          │           │
   │                │              │            │           │          │          │           │
   │                │              │ ──── Phase 3: Plan ─────────    │          │           │
   │                │              │            │           │          │          │           │
   │                │              │ compute_chunks()        │          │          │           │
   │                │              │───────────▶│           │          │          │           │
   │                │              │◀───────────│           │          │          │           │
   │                │              │  (chunk_specs[])       │          │          │           │
   │                │              │            │           │          │          │           │
   │                │              │  save_state(PLANNED)   │          │          │           │
   │                │              │────────────────────────────────────────────▶│           │
   │                │              │            │           │          │          │           │
   │                │              │ ──── Phase 4: Download ─────    │          │           │
   │                │              │            │           │          │          │           │
   │                │              │  ┌─── Worker Pool (async) ───┐  │          │           │
   │                │              │  │                           │  │          │           │
   │                │              │  │  for each pending chunk:  │  │          │           │
   │                │              │  │    │                      │  │          │           │
   │                │              │  │    │  GET Range: bytes    │  │          │           │
   │                │              │  │    │──────────────────────│──▶          │           │
   │                │              │  │    │◀─────────────────────│──│          │           │
   │                │              │  │    │  (chunk bytes)       │  │          │           │
   │                │              │  │    │                      │  │          │           │
   │                │              │  │    │  streaming hash      │  │          │           │
   │                │              │  │    │──────────────────────│──│─────────▶│           │
   │                │              │  │    │◀─────────────────────│──│─────────│           │
   │                │              │  │    │  (hash match?)       │  │          │           │
   │                │              │  │    │                      │  │          │           │
   │                │              │  │    │  update_state()      │  │          │           │
   │                │              │  │    │──────────────────────│──│──────────│──────────▶│
   │                │              │  │    │                      │  │          │           │
   │  progress cb   │              │  │                           │  │          │           │
   │◀───────────────│◀─────────────│  └───────────────────────────┘  │          │           │
   │                │              │            │           │          │          │           │
   │                │              │ ──── Phase 5: Assemble ─────    │          │           │
   │                │              │            │           │          │          │           │
   │                │              │                                  │          │  assemble()
   │                │              │──────────────────────────────────│──────────│──────────▶│
   │                │              │                                  │          │           │
   │                │              │                                  │  verify  │           │
   │                │              │◀─────────────────────────────────│──────────│──────────│
   │                │              │  (file_hash, verified)          │          │           │
   │                │              │            │           │          │          │           │
   │                │              │  save_state(COMPLETE)  │          │          │           │
   │                │              │────────────────────────────────────────────▶│           │
   │                │              │            │           │          │          │           │
   │  result        │              │            │           │          │          │           │
   │◀───────────────│◀─────────────│            │           │          │          │           │
   │                │              │            │           │          │          │           │
```

---

## 2. Download State Machine

### 2.1 Top-Level Download States

```
                         ┌──────────┐
                         │          │
        ┌───────────────▶│ PENDING  │
        │                │          │
        │                └────┬─────┘
        │                     │
        │              HEAD request +
        │              chunk planning
        │                     │
        │                     ▼
        │              ┌─────────────┐
        │              │             │
        │              │ IN_PROGRESS │◄──── resume()
        │              │             │
        │              └──┬──────┬───┘
        │                 │      │
        │    all chunks   │      │  unrecoverable
        │    complete     │      │  error
        │                 │      │
        │                 ▼      │
        │           ┌──────────┐ │
        │           │          │ │
        │           │ASSEMBLING│ │
        │           │          │ │
        │           └────┬─────┘ │
        │                │       │
        │           assembly     │
        │           complete     │
        │                │       │
        │                ▼       │
        │           ┌──────────┐ │
        │           │          │ │
        │           │VERIFYING │ │
        │           │          │ │
        │           └──┬────┬──┘ │
        │              │    │    │
        │         hash │    │    │
        │        match │    │    │
        │              │    │ hash mismatch
        │              ▼    │    │
        │        ┌──────────┐   │
        │        │          │   │
        │        │ COMPLETE │   │
        │        │          │   │
        │        └──────────┘   │
        │                       │
        │                       ▼
        │                ┌──────────┐
        │                │          │
        │                │  FAILED  │
        │                │          │
        │                └──────────┘
        │
        │  cancel()
        │                ┌──────────┐
        └────────────────│CANCELLED │
         (from any       │          │
          active state)  └──────────┘
```

### 2.2 State Transition Table

| From State | Event | To State | Side Effects |
|---|---|---|---|
| `PENDING` | Start download | `IN_PROGRESS` | HEAD request, compute chunks, save state |
| `IN_PROGRESS` | All chunks COMPLETE | `ASSEMBLING` | Trigger file assembly |
| `IN_PROGRESS` | Unrecoverable error | `FAILED` | Log error, save state |
| `IN_PROGRESS` | User cancels | `CANCELLED` | Save state, cleanup workers |
| `IN_PROGRESS` | Process crash | `IN_PROGRESS`* | State persisted, resume on restart |
| `ASSEMBLING` | Assembly complete | `VERIFYING` | Start whole-file hash computation |
| `ASSEMBLING` | Assembly error | `FAILED` | Log error, save state |
| `VERIFYING` | Hash matches | `COMPLETE` | Cleanup chunks, save state |
| `VERIFYING` | Hash mismatch | `FAILED` | Identify bad chunks, save state |
| `FAILED` | User retries | `IN_PROGRESS` | Re-queue failed chunks |
| `CANCELLED` | User resumes | `IN_PROGRESS` | Re-queue pending chunks |

*On crash, the state on disk remains `IN_PROGRESS`. On restart, the engine re-validates chunks and resumes.

---

### 2.3 Chunk State Machine

```
     ┌──────────┐
     │          │
     │ PENDING  │◄──────────────────────────────┐
     │          │                                │
     └────┬─────┘                                │
          │                                      │
     worker picks up                        retry (attempt
     chunk from queue                       < max_attempts)
          │                                      │
          ▼                                      │
    ┌───────────┐                                │
    │           │                                │
    │DOWNLOADING│                                │
    │           │                                │
    └──┬─────┬──┘                                │
       │     │                                   │
  bytes│     │ error                             │
  received   │ (timeout,                         │
  + hash     │  connection                       │
  verified   │  reset, etc.)                     │
       │     │                                   │
       │     ▼                                   │
       │  ┌──────────┐      attempt              │
       │  │          │      < max    ────────────┘
       │  │  FAILED  │──────────────┘
       │  │          │
       │  └────┬─────┘
       │       │
       │       │ attempt >= max_attempts
       │       │
       │       ▼
       │  ┌──────────┐
       │  │          │
       │  │ABANDONED │  (terminal — requires manual intervention)
       │  │          │
       │  └──────────┘
       │
       ▼
  ┌──────────┐
  │          │
  │ COMPLETE │  (terminal — chunk verified and stored)
  │          │
  └──────────┘
```

---

## 3. Data Flow Through Components

### 3.1 Chunk Download Data Path

```
┌──────────────────────────────────────────────────────────────────┐
│                    Single Chunk Download Flow                     │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  HTTP Server                                                     │
│      │                                                           │
│      │ TCP stream (TLS encrypted)                                │
│      │                                                           │
│      ▼                                                           │
│  httpx.AsyncClient                                               │
│      │                                                           │
│      │ Decrypted byte chunks (64 KB buffers)                     │
│      │                                                           │
│      ▼                                                           │
│  ┌─────────────────────────────────────┐                         │
│  │      Streaming Tee Pipeline         │                         │
│  │                                     │                         │
│  │  bytes ──┬──▶ hashlib.sha256.update()   ← hash accumulation  │
│  │          │                                                    │
│  │          └──▶ aiofiles.write(bytes)      ← disk write         │
│  │                                                               │
│  │  Memory: O(buffer_size) = O(64 KB)      ← bounded!           │
│  └─────────────────────────────────────┘                         │
│      │                                                           │
│      │ After all bytes received                                  │
│      │                                                           │
│      ▼                                                           │
│  hash_context.hexdigest()                                        │
│      │                                                           │
│      │ computed_hash                                             │
│      │                                                           │
│      ▼                                                           │
│  hmac.compare_digest(computed, expected)                          │
│      │                                                           │
│      ├── ✅ → ChunkResult(COMPLETE)                               │
│      └── ❌ → ChunkResult(FAILED, reason=HASH_MISMATCH)           │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 File Assembly Data Path

```
┌──────────────────────────────────────────────────────────────────┐
│                    File Assembly Flow                             │
├──────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Chunk Files on Disk (ordered by index)                          │
│                                                                  │
│  chunk_00000.dat ─┐                                              │
│  chunk_00001.dat ─┤                                              │
│  chunk_00002.dat ─┤                                              │
│  ...              ├──▶ Sequential Read                           │
│  chunk_12799.dat ─┘        │                                     │
│                            │                                     │
│                            ▼                                     │
│                    ┌───────────────┐                              │
│                    │  Assembly Tee │                              │
│                    │               │                              │
│                    │  bytes ──┬──▶ per_chunk_hash.update()        │
│                    │         │    (re-verify each chunk)          │
│                    │         │                                    │
│                    │         ├──▶ whole_file_hash.update()        │
│                    │         │    (accumulate full file hash)     │
│                    │         │                                    │
│                    │         └──▶ output_file.write()             │
│                    │              (write to final location)       │
│                    └───────────────┘                              │
│                            │                                     │
│                            │ After last chunk processed           │
│                            │                                     │
│                            ▼                                     │
│                    whole_file_hash.hexdigest()                    │
│                            │                                     │
│                            ▼                                     │
│                    Compare with expected file hash                │
│                            │                                     │
│                    ✅ → SUCCESS (rename temp → final)              │
│                    ❌ → FAILURE (identify bad chunks)              │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘
```

---

## 4. Concurrency Flow

### 4.1 Worker Lifecycle

```
Download Engine
      │
      │ creates N worker coroutines
      │
      ▼
  ┌────────────────────────────────────────────────────────────┐
  │                  asyncio Event Loop                        │
  │                                                            │
  │   ┌─────────┐    ┌─────────┐    ┌─────────┐              │
  │   │Worker 0 │    │Worker 1 │    │Worker 2 │    ...        │
  │   └────┬────┘    └────┬────┘    └────┬────┘              │
  │        │              │              │                    │
  │        ▼              ▼              ▼                    │
  │   ┌──────────────────────────────────────────┐           │
  │   │          Chunk Queue (asyncio.Queue)      │           │
  │   │                                           │           │
  │   │  [chunk_5] [chunk_42] [chunk_100] ...     │           │
  │   │                                           │           │
  │   └──────────────────────────────────────────┘           │
  │        │              │              │                    │
  │   get_nowait()   get_nowait()   get_nowait()             │
  │        │              │              │                    │
  │        ▼              ▼              ▼                    │
  │   ┌──────────────────────────────────────────┐           │
  │   │     asyncio.Semaphore(max_workers)        │           │
  │   │     (limits active HTTP connections)      │           │
  │   └──────────────────────────────────────────┘           │
  │        │              │              │                    │
  │   async with sem  async with sem  async with sem         │
  │        │              │              │                    │
  │        ▼              ▼              ▼                    │
  │   download_chunk  download_chunk  download_chunk         │
  │   verify_hash     verify_hash     verify_hash            │
  │   save_to_disk    save_to_disk    save_to_disk           │
  │        │              │              │                    │
  │        ▼              ▼              ▼                    │
  │   report_result   report_result   report_result          │
  │   update_state    update_state    update_state           │
  │                                                            │
  └────────────────────────────────────────────────────────────┘
```

### 4.2 Retry Flow Within Worker

```
Worker picks up chunk from queue
         │
         ▼
    attempt = 0
         │
         ▼
   ┌─────────────┐
   │  Try Download│◄────────────────────────────┐
   └──────┬──────┘                               │
          │                                      │
     ┌────┴────┐                                 │
     │ Success?│                                 │
     └────┬────┘                                 │
          │                                      │
     ┌────┴────┐                                 │
     │         │                                 │
    YES       NO                                 │
     │         │                                 │
     ▼         ▼                                 │
  Report    attempt += 1                         │
  SUCCESS       │                                │
            ┌───┴────┐                           │
            │attempt │                           │
            │< max?  │                           │
            └───┬────┘                           │
                │                                │
           ┌────┴────┐                           │
           │         │                           │
          YES       NO                           │
           │         │                           │
           ▼         ▼                           │
      compute    Report                          │
      delay()    ABANDONED                       │
           │                                     │
           ▼                                     │
      asyncio.sleep(delay)                       │
           │                                     │
           └─────────────────────────────────────┘
```

---

## 5. Resume Flow

### 5.1 Resume After Crash

```
  User runs: chunkguard resume state_file.state
         │
         ▼
  ┌──────────────────────────────────┐
  │  Load state file from disk       │
  │  Parse JSON → DownloadState      │
  └──────────┬───────────────────────┘
             │
             ▼
  ┌──────────────────────────────────┐
  │  Validate state file integrity   │
  │  - Check version compatibility   │
  │  - Verify JSON structure         │
  └──────────┬───────────────────────┘
             │
             ▼
  ┌──────────────────────────────────┐
  │  Re-validate completed chunks    │
  │  For each COMPLETE chunk:        │
  │    - File exists on disk?        │
  │    - File size correct?          │
  │    - (Optional) Re-hash chunk    │
  │  If validation fails:            │
  │    → Mark chunk as PENDING       │
  └──────────┬───────────────────────┘
             │
             ▼
  ┌──────────────────────────────────┐
  │  Reset DOWNLOADING → PENDING     │
  │  (These were interrupted)        │
  └──────────┬───────────────────────┘
             │
             ▼
  ┌──────────────────────────────────┐
  │  Check server file unchanged     │
  │  - HEAD request                  │
  │  - Compare ETag / Last-Modified  │
  │  - Compare Content-Length        │
  │  If changed:                     │
  │    → Warn user, restart          │
  └──────────┬───────────────────────┘
             │
             ▼
  ┌──────────────────────────────────┐
  │  Queue pending & failed chunks   │
  │  Resume normal download flow     │
  └──────────────────────────────────┘
```

---

## 6. Error Propagation Flow

```
  Error occurs in Worker
         │
         ▼
  ┌──────────────────────────────────┐
  │  Classify error                  │
  │  - Is it retryable?             │
  │  - What's the HTTP status?       │
  │  - What exception type?          │
  └──────────┬───────────────────────┘
             │
        ┌────┴────┐
        │         │
   Retryable  Non-Retryable
        │         │
        ▼         ▼
   Increment   Mark chunk
   attempt     ABANDONED
   counter         │
        │         ▼
        ▼    Log error with
   Compute   full context
   backoff       │
   delay         ▼
        │    Check: are ALL
        ▼    chunks ABANDONED?
   Sleep &       │
   retry    ┌────┴────┐
             │         │
            NO        YES
             │         │
             ▼         ▼
         Continue   Mark download
         (other     as FAILED
         chunks     Notify user
         may        via callback
         succeed)   & exit code
```
