# Performance — ReliaDL

> **Audience**: Engineers, Operations
> **Reading time**: ~8 minutes

---

## 1. Performance Characteristics

### 1.1 Throughput Model

```
Effective Throughput = min(
    network_bandwidth,
    server_throughput,
    disk_write_speed,
    cpu_hash_speed
) × parallelism_factor × (1 - overhead_fraction)
```

**Typical bottlenecks** (in order of likelihood):

1. **Network bandwidth** — most common limiter
2. **Server-side rate limiting** — per-IP or per-connection limits
3. **Disk I/O** — spinning disks cap at ~150 MB/s; NVMe SSDs at ~3,000 MB/s
4. **CPU (hashing)** — SHA-256 at ~2,000 MB/s with hardware acceleration

### 1.2 Parallelism Scaling

| Workers | Relative Throughput* | Notes |
|---|---|---|
| 1 | 1.0× (baseline) | Single connection — limited by per-connection throughput |
| 2 | 1.8× | Near-linear scaling |
| 4 | 3.2× | Typical sweet spot for most servers |
| 8 | 4.5× | Good for fast servers and networks |
| 16 | 5.0× | Diminishing returns — server/network saturation |
| 32 | 5.2× | Usually no benefit; may trigger rate limiting |

*Illustrative model, **not measured**. Shows the expected shape of diminishing returns as the server or path saturates; the exact values are not from an experiment.

### 1.3 Chunk Size Impact

| Chunk Size | Overhead per Chunk | Best For |
|---|---|---|
| 1 MB | ~15 ms (HTTP overhead + hash) | Unreliable networks (minimize re-download cost) |
| 4 MB | ~20 ms | Mobile / flaky WiFi |
| **8 MB** (default) | ~25 ms | General purpose |
| 16 MB | ~35 ms | Stable networks, large files |
| 32 MB | ~50 ms | Fast networks, very large files |
| 64 MB | ~80 ms | Datacenter-to-datacenter transfers |

### 1.4 Per-Chunk Overhead Breakdown

```
Per chunk, the system incurs:
  ├── HTTP request setup:     ~5 ms  (connection reuse via keep-alive)
  ├── TLS negotiation:        ~0 ms  (connection pooling, already established)
  ├── SHA-256 hash computation:
  │     1 MB chunk:           ~0.5 ms (with SHA-NI)
  │     8 MB chunk:           ~4 ms
  │     64 MB chunk:          ~32 ms
  ├── Disk write:
  │     8 MB to SSD:          ~1 ms
  │     8 MB to HDD:          ~50 ms
  ├── State file update:      ~2 ms  (JSON serialize + atomic write)
  └── Progress callback:      ~0.1 ms
  
  Total per 8 MB chunk:       ~12 ms (SSD) to ~62 ms (HDD)
  Overhead fraction:          ~0.15% (SSD) to ~0.8% (HDD)
```

---

## 2. Memory Usage

### 2.1 Memory Model

```
Total Memory ≈ base_overhead 
             + (workers × buffer_per_worker)
             + hash_state_per_worker
             + state_object

Where:
  base_overhead       ≈ 30 MB   (Python runtime + libraries)
  buffer_per_worker   ≈ 128 KB  (2 × 64 KB read buffers)
  hash_state_per_worker ≈ 0.2 KB (SHA-256 internal state)
  state_object        ≈ 0.5 KB × num_chunks
```

### 2.2 Memory Usage by File Size

| File Size | Chunks (8 MB) | State Object | Total Memory (4 workers) |
|---|---|---|---|
| 100 MB | 13 | ~7 KB | ~31 MB |
| 1 GB | 128 | ~64 KB | ~31 MB |
| 10 GB | 1,280 | ~640 KB | ~32 MB |
| 100 GB | 12,800 | ~6.4 MB | ~37 MB |
| 1 TB | 128,000* | ~64 MB* | ~95 MB |

*Chunk size auto-increased to keep count ≤ 100,000.

**Key insight**: Memory usage is bounded by buffer sizes, NOT by file or chunk size. A 1 TB download uses ~95 MB of RAM.

---

## 3. Disk I/O Patterns

### 3.1 Write Pattern During Download

```
Parallel random writes (one per worker):
  Worker 0 → chunk_00042.dat  (sequential within chunk)
  Worker 1 → chunk_00043.dat  (sequential within chunk)
  Worker 2 → chunk_00044.dat  (sequential within chunk)
  Worker 3 → chunk_00045.dat  (sequential within chunk)

Each worker writes sequentially within its own chunk file.
Workers write to different files, so there's no contention.
```

### 3.2 Read Pattern During Assembly

```
Sequential read (one file at a time):
  Read chunk_00000.dat → write to output.bin
  Read chunk_00001.dat → write to output.bin
  Read chunk_00002.dat → write to output.bin
  ...

Perfectly sequential — optimal for both SSD and HDD.
```

### 3.3 State File I/O

```
State file writes:
  - Every chunk completion (~25 ms between writes for 8 MB chunks at 300 MB/s)
  - Every 5 seconds (progress snapshot)
  - On shutdown
  
  Size: ~100 bytes per chunk → 1.2 MB for 12,800 chunks (100 GB file)
  Impact: Negligible — single JSON write, atomic rename
```

### 3.4 Direct Sparse Writes vs Staged Assembly Performance

| Metric | Staged Chunks Mode | Direct Sparse Mode (`--direct-write`) |
|---|---|---|
| **Peak Storage Overhead** | 2.1× File Size | **1.0× File Size** |
| **Post-Download Assembly Delay** | ~40s per 100 GB (NVMe) | **0.0s (Instant finalization)** |
| **Total Disk Write Operations** | 2× (Write chunks + Read/Write Assembly) | **1× (Write directly to target offset)** |
| **Random I/O Overhead** | Isolated chunk files | Positional `pwrite()` per chunk |
| **Filesystem Compatibility** | All filesystems (ext4, NTFS, APFS, FAT32) | POSIX `posix_fallocate` / NTFS Sparse |

---

## 4. Network Efficiency

### 4.1 Bandwidth Utilization

```
Ideal:   100% bandwidth utilization
Reality: ~85-95% for 4 workers on a stable connection

Lost bandwidth:
  ├── HTTP headers:           ~200 bytes per request  (~0.003% for 8 MB chunk)
  ├── TLS overhead:           ~50 bytes per record     (~0.001%)
  ├── TCP overhead:           ~40 bytes per segment     (~0.06%)
  ├── Inter-chunk gap:        ~5 ms between chunks      (~0.05% at 100 MB/s)
  └── Retry overhead:         variable (only on failures)
  
  Total protocol overhead:   < 0.2%
```

### 4.2 Bandwidth Waste on Failure

| Failure Scenario | Bytes Wasted | As % of 100 GB File |
|---|---|---|
| 1 chunk fails, retry succeeds | 8 MB | 0.008% |
| 5 chunks fail, all retry succeed | 40 MB | 0.04% |
| 1 chunk fails 3 times, succeeds on 3rd | 24 MB | 0.024% |
| Downloader with no resume support, failing at 95% | **95 GB** | **95%** |

---

## 5. Benchmark Results

> **No measured benchmark results are currently published.**
>
> A previous revision of this document contained throughput, chunk-size and
> SHA-256 tables attributed to an AWS EC2 / S3 / CloudFront test environment.
> **No script, harness, or code path in this repository could have produced those
> numbers** — the download engine has no S3 code path, and no benchmark harness
> exists. They have been removed rather than corrected, because there is no raw
> data to correct them against.
>
> Every figure in §1-§4 above is an **analytical model or an order-of-magnitude
> estimate**, not a measurement. Treat them as design guidance only. None of them
> may be cited as experimental results.
>
> Measured results will appear here only when accompanied by a committed harness,
> fixed seeds, repetition counts, and raw data under `benchmarks/`.


## 6. Tuning Guide

### 6.1 Maximize Throughput

```yaml
# For fast, stable connections (datacenter, fiber)
download:
  chunk_size: "16MB"       # Reduce per-chunk overhead
  max_parallel_workers: 8  # Saturate bandwidth
```

### 6.2 Maximize Reliability

```yaml
# For unreliable connections (mobile, satellite)
download:
  chunk_size: "2MB"        # Minimize re-download cost on failure
  max_parallel_workers: 2  # Reduce server load

retry:
  max_attempts: 10         # More retries before giving up
  max_delay: 300.0         # Wait longer between retries
```

### 6.3 Minimize Memory

```yaml
# For constrained environments (IoT, containers)
download:
  chunk_size: "4MB"        # Smaller state object
  max_parallel_workers: 1  # Minimum memory per worker
```

### 6.4 Bandwidth Limiting

```yaml
# Avoid saturating shared connections
network:
  max_bandwidth: 10485760  # 10 MB/s
```

---

## 7. Capacity Planning

### 7.1 Estimated Download Times

| File Size | 10 Mbps | 100 Mbps | 1 Gbps | 10 Gbps |
|---|---|---|---|---|
| 100 MB | 80s | 8s | 1s | < 1s |
| 1 GB | 13 min | 80s | 8s | 1s |
| 10 GB | 2.2 hr | 13 min | 80s | 8s |
| 100 GB | 22 hr | 2.2 hr | 13 min | 80s |
| 1 TB | 9 days | 22 hr | 2.2 hr | 13 min |

*Assumes 4 workers achieving ~80% of theoretical bandwidth.

### 7.2 Disk Space Requirements

```
During download:  2.1 × file_size  (chunks + partial output + state)
After completion:  1.0 × file_size  (output only, chunks cleaned up)
```

