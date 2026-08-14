# Project Overview — ChunkGuard

> **Audience**: Everyone (stakeholders, product managers, executives, developers, end-users)
> **Reading time**: ~10 minutes

---

## 1. What Is ChunkGuard?

ChunkGuard is a **fault-tolerant file download system** designed to reliably transfer large files over unreliable networks. Instead of downloading a file as a single monolithic stream — where any interruption means starting over — ChunkGuard divides the file into small, independently verifiable **chunks**, downloads them in parallel, and reassembles them into the original file with cryptographic proof that every byte is correct.

### The Problem We Solve

Downloading large files (operating system images, datasets, backups, media libraries) over the internet is fragile:

| Problem | Traditional Downloader | ChunkGuard |
|---|---|---|
| Network drops mid-download | Restart from 0% | Resume from where it stopped |
| Downloaded file is silently corrupted | No detection until you try to use it | Every chunk is hash-verified on arrival |
| Slow single-threaded speed | One connection, one stream | Parallel chunk downloads saturate bandwidth |
| Server timeout on large files | Download fails entirely | Small chunks complete within timeout windows |
| Partial corruption in a 10 GB file | Re-download all 10 GB | Re-download only the corrupted 8 MB chunk |

### Real-World Analogy

Imagine shipping a 1,000-page book across the country. The traditional approach is to ship the entire book in one box — if the box is damaged, you resend the entire book. ChunkGuard is like shipping each chapter in its own sealed, numbered envelope with a tamper-evident seal. If envelope #7 is damaged, you only resend chapter 7. You can even ship multiple envelopes simultaneously via different routes.

---

## 2. Why Does ChunkGuard Exist?

### Business Drivers

1. **Bandwidth Cost Reduction**: Re-downloading only failed chunks (typically < 1% of total data) instead of entire files saves significant egress bandwidth costs.
2. **Time Savings**: A 50 GB dataset that fails at 95% takes 47.5 GB to restart traditionally. ChunkGuard retries only the failed ~400 MB chunk.
3. **Reliability SLA Compliance**: Systems that depend on file delivery (CI/CD pipelines, data warehouses, content distribution) need guaranteed delivery.
4. **User Experience**: End-users expect downloads to "just work" even on flaky connections (mobile, satellite, developing-market infrastructure).

### Technical Drivers

1. **Data Integrity**: Silent corruption (bit rot, network injection, truncation) must be detectable and correctable without human intervention.
2. **Scalability**: The system must handle files from kilobytes to terabytes with the same architecture.
3. **Observability**: Operators need real-time visibility into download progress, chunk status, and failure rates.

---

## 3. How Does It Work? (Simplified)

### Step-by-Step Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        ChunkGuard Flow                          │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. USER provides:  URL + expected file hash (optional)         │
│         │                                                       │
│         ▼                                                       │
│  2. METADATA FETCH:  HEAD request → file size, server support   │
│         │                                                       │
│         ▼                                                       │
│  3. CHUNK PLAN:  Divide file into N chunks (e.g., 8 MB each)   │
│         │                                                       │
│         ▼                                                       │
│  4. PARALLEL DOWNLOAD:  Download chunks concurrently            │
│         │                 (configurable parallelism)             │
│         ▼                                                       │
│  5. PER-CHUNK VERIFY:  SHA-256 hash each chunk on arrival       │
│         │                                                       │
│         ├── ✅ Hash matches → mark chunk COMPLETE                │
│         │                                                       │
│         └── ❌ Hash mismatch → mark chunk FAILED → retry        │
│                                                                 │
│  6. REASSEMBLE:  Concatenate verified chunks in order           │
│         │                                                       │
│         ▼                                                       │
│  7. FINAL VERIFY:  SHA-256 of entire assembled file             │
│         │                                                       │
│         ├── ✅ Match → download COMPLETE                         │
│         │                                                       │
│         └── ❌ Mismatch → identify & retry failed chunks        │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### Key Concepts

| Concept | What It Means |
|---|---|
| **Chunk** | A fixed-size byte range of the original file (default 8 MB) |
| **Chunk Hash** | A SHA-256 fingerprint of that chunk's bytes — any change produces a completely different hash |
| **Manifest** | A file listing all chunks, their byte ranges, and expected hashes |
| **State File** | A persistent record of which chunks are complete, failed, or pending — enables resume |
| **Parallel Workers** | Multiple simultaneous HTTP connections downloading different chunks |
| **Exponential Backoff** | When a retry is needed, wait progressively longer (1s → 2s → 4s → 8s) to avoid overwhelming the server |

---

## 4. Who Is This For?

### Primary Users

| User Persona | Use Case |
|---|---|
| **DevOps / SREs** | Reliable artifact delivery in CI/CD pipelines |
| **Data Engineers** | Downloading large datasets for ML training pipelines |
| **System Administrators** | Distributing OS images, firmware updates, backup archives |
| **Content Distributors** | Delivering media files, game patches, software updates |
| **End Users** | Downloading large files on unreliable connections |

### Stakeholders

| Stakeholder | Interest |
|---|---|
| **Product Managers** | Feature completeness, user satisfaction |
| **Engineering Leads** | Architecture quality, maintainability |
| **Security Team** | Hash algorithm strength, state file integrity |
| **Operations Team** | Monitoring, alerting, failure recovery |
| **Finance / Business** | Bandwidth cost reduction, SLA compliance |

---

## 5. Project Scope

### In Scope (v1.0)

- ✅ Chunked downloads via HTTP/HTTPS Range requests
- ✅ SHA-256 per-chunk and whole-file verification
- ✅ Configurable chunk size (1 MB – 256 MB)
- ✅ Parallel downloads with configurable concurrency (1–32 workers)
- ✅ Persistent state for resume-after-crash
- ✅ Automatic retries with exponential backoff + jitter
- ✅ CLI interface for download, resume, and verify operations
- ✅ Structured JSON logging for observability
- ✅ Progress reporting (percentage, speed, ETA)
- ✅ Configurable via YAML configuration file

### Out of Scope (v1.0)

- ❌ GUI / graphical interface
- ❌ Upload (reverse direction)
- ❌ BitTorrent / peer-to-peer protocols
- ❌ FTP / SFTP / S3 protocols (HTTP/HTTPS only)
- ❌ Streaming / real-time data
- ❌ Encryption at rest (chunks stored as plaintext)
- ❌ Multi-file / directory batch downloads (planned v2.0)

---

## 6. Success Metrics

| Metric | Target | Measurement |
|---|---|---|
| **Download Reliability** | 99.9% successful completion rate | Completed downloads / attempted downloads |
| **Corruption Detection Rate** | 100% of injected corruptions detected | Fault injection testing |
| **Resume Success Rate** | 100% of interrupted downloads resume correctly | Kill-and-resume testing |
| **Bandwidth Waste on Retry** | < 2% of total file size re-downloaded on failure | Failed chunk bytes / total file bytes |
| **Parallel Speedup** | ≥ 3x throughput vs single-stream on 4 workers | Benchmark testing |
| **Time to First Byte** | < 2 seconds from command to first chunk downloading | Stopwatch measurement |

---

## 7. Risk Summary

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Server doesn't support Range requests | Medium | High — falls back to single-stream | Detect via HEAD request; warn user; graceful fallback |
| Hash collision (two different inputs → same SHA-256) | Negligible | Catastrophic | SHA-256 collision probability is ~2⁻¹²⁸ — considered computationally infeasible |
| State file corruption | Low | Medium — must restart download | Atomic writes with temporary file + rename; CRC on state file |
| Rate limiting by server | Medium | Medium — download slows/fails | Exponential backoff with jitter; configurable rate limiting |
| Disk full during download | Low | High — incomplete file | Pre-check available space; graceful error on ENOSPC |

---

## 8. Technology Stack

| Layer | Technology | Rationale |
|---|---|---|
| **Language** | Python 3.10+ | Cross-platform, rich ecosystem, excellent HTTP libraries |
| **HTTP Client** | `httpx` (async) | HTTP/2 support, connection pooling, async-native |
| **Hashing** | `hashlib` (stdlib) | FIPS-compliant SHA-256, hardware-accelerated on modern CPUs |
| **Concurrency** | `asyncio` + `aiofiles` | Non-blocking I/O for parallel chunk downloads |
| **State Persistence** | JSON files | Human-readable, no external database dependency |
| **CLI Framework** | `click` | Declarative CLI with rich help text |
| **Logging** | `structlog` | Structured JSON logging for machine-parseable observability |
| **Configuration** | `pydantic` + YAML | Type-safe configuration with validation |
| **Testing** | `pytest` + `pytest-asyncio` | Industry-standard async test framework |

---

## 9. Roadmap

| Version | Target | Features |
|---|---|---|
| **v1.0** | Current | Core chunked download, verification, resume, retries, CLI |
| **v1.1** | +2 months | Bandwidth throttling, proxy support, custom headers |
| **v1.2** | +4 months | S3 / Azure Blob / GCS protocol adapters |
| **v2.0** | +6 months | Multi-file batch downloads, directory mirroring, daemon mode |
| **v2.1** | +8 months | Web dashboard, REST API, webhook notifications |
| **v3.0** | +12 months | Peer-assisted downloads (hybrid CDN), deduplication |

---

## 10. How to Get Started

- **Users**: Read the [User Guide](USER_GUIDE.md) for step-by-step instructions
- **Developers**: Read the [Architecture](ARCHITECTURE.md) and [Technical Specification](TECHNICAL_SPECIFICATION.md)
- **Operations**: Read the [Deployment Guide](DEPLOYMENT_GUIDE.md)
- **Contributors**: Read the [Contributing Guide](CONTRIBUTING.md)
