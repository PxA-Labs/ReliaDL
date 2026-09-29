# Command Line Interface (CLI) Guide — ReliaDL

> **Audience**: Systems Administrators, DevOps Engineers, and End Users  
> **Target Version**: ReliaDL v0.3.0+  

---

## 1. Overview & Executable Binary Setup

ReliaDL provides an enterprise-grade Command Line Interface (CLI) designed for parallel high-throughput chunked file downloads, network probing, state diagnostic inspection, host environment audits, telemetry monitoring, and Merkle tree segment verification.

### Invocation Modes

After installing ReliaDL via `pip install reliadl`, the CLI can be invoked in two ways:

1. **Standalone Binary Script** (Recommended):
   ```bash
   reliadl <subcommand> [options]
   ```

2. **Python Module Execution**:
   ```bash
   python -m reliadl.cli <subcommand> [options]
   # or
   python -m reliadl.main <subcommand> [options]
   ```

---

## 2. Global Options & Flags

Global options apply across all CLI subcommands:

| Flag | Short | Description |
|---|---|---|
| `--version` | | Display installed ReliaDL software version string and exit |
| `--verbose` | `-v` | Enable verbose DEBUG level logging output |
| `--help` | `-h` | Display usage instructions and available subcommands |

Example:
```bash
reliadl --version
# Output: reliadl 0.3.0

reliadl --verbose doctor
```

---

## 3. Core Transfer Commands

### 3.1 `download` — Execute Parallel Chunked File Transfer

Initiates a high-speed parallel file download with optional adaptive chunk sizing (AdaChunk) and multi-armed bandit mirror scheduling (Whittle index).

**Syntax**:
```bash
reliadl download --url URL --output OUTPUT_PATH [OPTIONS]
```

**Parameters**:

| Parameter | Required | Description |
|---|---|---|
| `--url` | Yes | Source HTTP/HTTPS URL of target payload |
| `--output`, `-o` | Yes | Target destination file path |
| `--adachunk` | No | Enable dynamic AdaChunk Lyapunov chunk size optimization |
| `--whittle` | No | Enable Whittle index multi-armed bandit mirror scheduling |
| `--expected-hash`, `--sha256` | No | Expected SHA-256 of the whole file; the download fails if it does not match |
| `--workers`, `-j` | No | Parallel connections, 1–32 (default: `download.max_parallel_workers`, 4) |
| `--chunk-size` | No | Chunk size such as `8MB`, 1MB–256MB (default: `download.chunk_size`) |
| `--limit-rate` | No | Bandwidth cap per second such as `10MB`; `0` is unlimited |
| `--config` | No | Path to a ReliaDL YAML configuration file |

**How it works**: the server is probed with `HEAD` (or a one-byte range `GET` if `HEAD` is refused) for its size, `Accept-Ranges`, and `ETag`. The file is split into chunks, and a pool of workers fetches them concurrently with `Range` requests, retrying transient failures (timeouts, resets, 429, 5xx) with exponential backoff. Bytes are written straight into a pre-allocated `<output>.part` file and each chunk is SHA-256 hashed as it streams. After every chunk completes, the session is checkpointed to `.ReliaDL/<name>.state` next to the output. When all chunks are done the whole file is hashed and `<output>.part` is renamed to the output path.

Servers that do not support byte ranges are rejected for now; single-stream fallback is tracked in #42.

**Interrupting**: `Ctrl+C` (or `SIGTERM`) stops the transfer, saves the checkpoint, prints the `resume` command, and exits with status `130`.

**Example**:
```bash
reliadl download \
  --url "https://cdn.example.com/datasets/models_v2.tar.gz" \
  --output "./downloads/models_v2.tar.gz" \
  --adachunk \
  --whittle
```

---

### 3.2 `resume` — Resume Interrupted Session

Resumes an interrupted transfer session using an atomic `.state` state file checkpoint.

**Syntax**:
```bash
reliadl resume --state-file STATE_FILE_PATH
```

**Parameters**:

| Parameter | Required | Description |
|---|---|---|
| `--state-file` | Yes | Absolute or relative path to `.state` checkpoint file |
| `--workers`, `--chunk-size`, `--limit-rate`, `--config` | No | As for `download`; the chunk layout recorded in the state file is kept |

Before continuing, the remote file is probed again: if its size or `ETag` has changed, the resume is refused. Every chunk the state file marks complete is re-hashed from `<output>.part`, and any that no longer match are downloaded again.

**Example**:
```bash
reliadl resume --state-file "./downloads/.ReliaDL/models_v2.tar.gz.state"
```

---

### 3.3 `verify` — Cryptographic Whole-File Digest Check

Computes the SHA-256 hash digest of a target file and compares it against an expected hexadecimal digest in constant time.

**Syntax**:
```bash
reliadl verify --file FILE_PATH --expected-hash EXPECTED_HEX_DIGEST
```

**Parameters**:

| Parameter | Required | Description |
|---|---|---|
| `--file` | Yes | Target file path to verify |
| `--expected-hash` | Yes | Expected SHA-256 hex string (accepts `sha256:` prefix) |

**Example**:
```bash
reliadl verify \
  --file "./downloads/models_v2.tar.gz" \
  --expected-hash "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

---

## 4. Enterprise Diagnostic & Benchmarking Commands

### 4.1 `doctor` — Host OS Environment & Storage Audit

Audits the host operating system, Python runtime, file descriptor limits, OpenSSL hardware acceleration, and positional write storage permissions.

**Syntax**:
```bash
reliadl doctor
```

**Diagnostic Checks**:
- Python Runtime Version (requires Python 3.10+)
- Cryptography OpenSSL Hardware Acceleration (`AVX2` / `SHA-NI`)
- Disk Positional Write & Storage Write Permissions
- File Descriptor / Handle Allocation Limits (`ulimit -n` / Win32 handles)

**Example Output**:
```text
======================================================================
 RELIADL SYSTEM ENVIRONMENT & STORAGE DIAGNOSTIC DOCTOR
======================================================================
[PASS] Python Runtime Compatible (3.13.14)
[PASS] OpenSSL / Cryptography Acceleration Active (v50.0.1)
[PASS] Positional IO & Storage Write Permissions Confirmed
[INFO] OS Platform: Windows (11)
[PASS] Windows Win32 Storage Handle Allocation Active
----------------------------------------------------------------------
 HEALTH CHECK PASSED: System environment is fully operational.
======================================================================
```

---

### 4.2 `benchmark` — High-Throughput Crypto & Network Audit

Measures raw multi-core SHA-256 digest throughput, segment Merkle tree generation rates, and latency percentiles (`p50`, `p90`, `p99`).

**Syntax**:
```bash
reliadl benchmark [--duration SECONDS] [--block-size KB] [--url TARGET_URL]
```

**Parameters**:

| Parameter | Default | Description |
|---|---|---|
| `--duration` | `6.0` | Test duration in seconds |
| `--block-size` | `64` | Cryptographic buffer block size in KB |
| `--url` | None | Optional HTTP endpoint to measure network round-trip time (RTT) |

**Example**:
```bash
reliadl benchmark --duration 10 --block-size 64
```

---

### 4.3 `probe` — Infrastructure Pre-Flight Diagnostic

Tests target endpoints for HTTP range request support (`Accept-Ranges`), HTTP/2 multiplexing, Content-Length header declaration, and corporate proxy tunnel authentication.

**Syntax**:
```bash
reliadl probe --url TARGET_URL [--proxy PROXY_URL] [--timeout SECONDS]
```

**Parameters**:

| Parameter | Default | Description |
|---|---|---|
| `--url` | Required | Target endpoint URL to probe |
| `--proxy` | None | Optional SOCKS5 or HTTP CONNECT proxy URL |
| `--timeout` | `10.0` | Connection probe timeout in seconds |

**Example**:
```bash
reliadl probe \
  --url "https://storage.corp.internal/artifacts/model.bin" \
  --proxy "socks5h://service_account:secret@proxy.corp.internal:1080"
```

---

### 4.4 `inspect-state` — Checkpoint & Manifest Diagnostic

Audits and parses `.state` binary checkpoint files or `.cgmanifest` catalogs without downloading or mutating data.

**Syntax**:
```bash
reliadl inspect-state --state-file FILE_PATH [--json]
```

**Parameters**:

| Parameter | Default | Description |
|---|---|---|
| `--state-file` | Required | Path to `.state` checkpoint file or `.cgmanifest` manifest catalog |
| `--json` | `false` | Output diagnostic findings as formatted JSON |

**Example**:
```bash
reliadl inspect-state --state-file "./downloads/.reliadl/model.bin.state" --json
```

---

### 4.5 `top` — Real-Time Telemetry Dashboard

Displays a live terminal monitoring interface querying local Prometheus telemetry exporters.

**Syntax**:
```bash
reliadl top [--metrics-port PORT] [--once]
```

**Parameters**:

| Parameter | Default | Description |
|---|---|---|
| `--metrics-port` | `9090` | Target Prometheus metrics HTTP port |
| `--once` | `false` | Output single metrics snapshot and exit |

**Example**:
```bash
reliadl top --metrics-port 9090 --once
```

---

### 4.6 `hash-tree` — Segment Merkle Tree Cryptographic Audit

Computes segment leaf hashes, constructs a domain-separated binary Merkle tree, and verifies file integrity against an expected Merkle tree root.

**Syntax**:
```bash
reliadl hash-tree --file FILE_PATH [--merkle-root HEX_ROOT] [--block-size BYTES]
```

**Parameters**:

| Parameter | Default | Description |
|---|---|---|
| `--file` | Required | Target file path to audit |
| `--merkle-root` | None | Expected 64-character Merkle tree root hex digest |
| `--block-size` | `4096` | Segment block size in bytes (default 4 KB) |

**Example**:
```bash
reliadl hash-tree \
  --file "./downloads/models_v2.tar.gz" \
  --merkle-root "0c87a05c634ecf1ef2a43d5b1f30e650022efd9910aac1768faf69e1f1b92396" \
  --block-size 4096
```

---

## 5. Exit Codes Reference

ReliaDL CLI returns standardized exit codes for automation scripts and CI/CD pipelines:

| Exit Code | Classification | Meaning |
|---|---|---|
| `0` | `SUCCESS` | Command executed successfully; validation passed |
| `1` | `ERROR` | General failure, invalid configuration, or verification failure |

---

## 6. Troubleshooting & Common Errors

### Command Not Found (`reliadl: command not found`)
- **Cause**: Python `Scripts/` directory is not included in system `PATH`.
- **Solution**: Execute using `python -m reliadl.cli <subcommand>` or append your Python Scripts directory (`site-packages/../Scripts`) to your operating system PATH environment variable.

### Range Header Warning (`Accept-Ranges: None`)
- **Cause**: Target HTTP server does not support parallel range requests.
- **Solution**: ReliaDL will fallback to single-stream download mode automatically.

### State File Not Found (`State file not found`)
- **Cause**: Resume command provided invalid state file path.
- **Solution**: Inspect the default hidden state directory (`./downloads/.reliadl/`) using `reliadl inspect-state`.
