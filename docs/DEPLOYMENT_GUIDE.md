# Deployment & Operations Guide — ChunkGuard

> **Audience**: DevOps Engineers, System Administrators
> **Reading time**: ~10 minutes

---

## 1. System Requirements

### 1.1 Minimum Requirements

| Resource | Minimum | Recommended |
|---|---|---|
| **Python** | 3.10+ | 3.12+ (best asyncio performance) |
| **RAM** | 128 MB free | 512 MB free |
| **Disk Space** | 2× target file size | 2.5× target file size (chunks + output + buffer) |
| **Network** | Any internet connection | 10+ Mbps recommended |
| **OS** | Linux, macOS, Windows 10+ | Linux (best async I/O performance) |
| **CPU** | 1 core | 2+ cores (hash computation + I/O) |

### 1.2 Disk Space Calculation

```
Required disk space = file_size              (final output)
                    + file_size              (chunk files during download)
                    + ~10 MB                 (state files, logs)
                    + buffer                 (10% safety margin)

Example for a 100 GB file:
  Required = 100 GB + 100 GB + 10 MB + 20 GB = ~220 GB
  
After assembly and cleanup:
  Final usage = 100 GB (output file only)
```

---

## 2. Installation

### 2.1 From PyPI (Recommended)

```bash
pip install chunkguard
```

### 2.2 From Source

```bash
git clone https://github.com/your-org/chunkguard.git
cd chunkguard
pip install -e ".[dev]"
```

### 2.3 Docker

```dockerfile
FROM python:3.12-slim

RUN pip install --no-cache-dir chunkguard

ENTRYPOINT ["chunkguard"]
```

```bash
docker build -t chunkguard .
docker run -v /downloads:/downloads chunkguard download \
  --url "https://example.com/file.iso" \
  --output "/downloads/file.iso"
```

### 2.4 Dependencies

```
httpx[http2]>=0.27.0     # HTTP client with HTTP/2 support
aiofiles>=24.1.0          # Async file I/O
click>=8.1.0              # CLI framework
pydantic>=2.5.0           # Configuration validation
structlog>=24.1.0         # Structured logging
pyyaml>=6.0.0             # YAML config parser
rich>=13.7.0              # Terminal progress bars & formatting
```

---

## 3. Configuration

### 3.1 Configuration File Locations

Checked in order (first found wins):

1. `--config` CLI flag
2. `CHUNKGUARD_CONFIG` environment variable
3. `./chunkguard.yaml` (current directory)
4. `~/.config/chunkguard/config.yaml` (user config)
5. Built-in defaults

### 3.2 Production Configuration Example

```yaml
# /etc/chunkguard/config.yaml — Production Configuration

download:
  chunk_size: "16MB"                    # Larger chunks for stable networks
  max_parallel_workers: 8               # Saturate available bandwidth
  verify_on_complete: true
  cleanup_chunks_on_complete: true
  pre_check_disk_space: true

network:
  connect_timeout: 15                   # Faster failure detection
  read_timeout: 120                     # Shorter timeout for responsive servers
  max_redirects: 3
  user_agent: "ChunkGuard/1.0 (Production)"
  http2: true
  verify_ssl: true
  max_bandwidth: 0                      # No limit in production

retry:
  max_attempts: 5                       # More retries for reliability
  base_delay: 2.0
  max_delay: 120.0                      # Longer max delay for rate limiting
  backoff_factor: 2.0
  jitter_factor: 0.5

logging:
  level: "INFO"
  format: "json"
  file: "/var/log/chunkguard/download.log"

progress:
  update_interval: 1.0
  show_speed: true
  show_eta: true
  show_chunk_details: false
```

### 3.3 Environment Variable Reference

```bash
# Core settings
export CHUNKGUARD_CHUNK_SIZE="16MB"
export CHUNKGUARD_WORKERS=8
export CHUNKGUARD_RETRIES=5

# Network
export CHUNKGUARD_CONNECT_TIMEOUT=15
export CHUNKGUARD_READ_TIMEOUT=120
export CHUNKGUARD_VERIFY_SSL=true
export CHUNKGUARD_HTTP2=true

# Logging
export CHUNKGUARD_LOG_LEVEL=INFO
export CHUNKGUARD_LOG_FORMAT=json
export CHUNKGUARD_LOG_FILE=/var/log/chunkguard/download.log
```

---

## 4. Monitoring

### 4.1 Log-Based Monitoring

ChunkGuard emits structured JSON logs that integrate with any log aggregation system:

**Key Log Events to Monitor**:

| Event | Level | Meaning | Action |
|---|---|---|---|
| `download_started` | INFO | New download initiated | Track |
| `download_completed` | INFO | Download finished successfully | Track completion rate |
| `download_failed` | ERROR | Download failed permanently | Alert |
| `chunk_download_complete` | DEBUG | Individual chunk done | Track throughput |
| `chunk_download_failed_retrying` | WARNING | Chunk failed, will retry | Monitor retry rate |
| `chunk_abandoned` | ERROR | Chunk exceeded max retries | Alert |
| `hash_mismatch_chunk` | WARNING | Chunk integrity failure | Monitor integrity rate |
| `hash_mismatch_file` | CRITICAL | Whole file integrity failure | Immediate alert |
| `disk_space_low` | WARNING | Available space below threshold | Alert |
| `state_file_write_failed` | ERROR | Could not persist state | Alert |
| `server_etag_changed` | WARNING | File changed on server | Investigate |

### 4.2 Metrics for Dashboards

| Metric | Type | Description |
|---|---|---|
| `chunkguard_downloads_total` | Counter | Total downloads attempted |
| `chunkguard_downloads_completed` | Counter | Successfully completed downloads |
| `chunkguard_downloads_failed` | Counter | Failed downloads |
| `chunkguard_chunks_downloaded_total` | Counter | Total chunks downloaded |
| `chunkguard_chunks_retried_total` | Counter | Chunks that required retry |
| `chunkguard_chunks_abandoned_total` | Counter | Chunks that exceeded max retries |
| `chunkguard_bytes_downloaded_total` | Counter | Total bytes transferred |
| `chunkguard_download_duration_seconds` | Histogram | Download duration distribution |
| `chunkguard_chunk_download_duration_seconds` | Histogram | Per-chunk duration |
| `chunkguard_download_speed_bytes_per_second` | Gauge | Current download speed |
| `chunkguard_hash_mismatches_total` | Counter | Hash verification failures |
| `chunkguard_active_workers` | Gauge | Currently active download workers |

### 4.3 Example Grafana Dashboard Panels

```
┌─────────────────────────────────────────────────────────────┐
│  Download Success Rate (24h)    │  Active Downloads          │
│  ████████████████████ 99.7%     │  ▓▓▓░░░░░  3/8             │
├─────────────────────────────────┤─────────────────────────────│
│  Throughput (MB/s)              │  Chunk Retry Rate           │
│  ▁▂▃▅▇█▇▅▃▂▁ (peak: 450 MB/s) │  ▁▁▁▂▁▁▁▁ 0.3%            │
├─────────────────────────────────┤─────────────────────────────│
│  Hash Mismatches (7d)           │  Disk Space Remaining       │
│  ●●○○○○○ 2 total               │  ████████████░░░ 78%        │
└─────────────────────────────────┴─────────────────────────────┘
```

---

## 5. Operational Procedures

### 5.1 Starting a Download

```bash
# Basic
chunkguard download \
  "https://releases.example.com/app-v2.0.iso" \
  "/data/downloads/app-v2.0.iso"

# Production (with hash, more workers, custom config)
chunkguard download \
  --hash "sha256:e3b0c44298fc1c149afbf4c8996fb924..." \
  --workers 8 \
  --chunk-size 16MB \
  --config /etc/chunkguard/config.yaml \
  "https://releases.example.com/app-v2.0.iso" \
  "/data/downloads/app-v2.0.iso"
```

### 5.2 Monitoring a Running Download

```bash
# Check status from state file
chunkguard status /data/downloads/.chunkguard/app-v2.0.iso.state

# JSON output for scripting
chunkguard status --json /data/downloads/.chunkguard/app-v2.0.iso.state
```

### 5.3 Resuming After Failure

```bash
# Resume with same settings
chunkguard resume /data/downloads/.chunkguard/app-v2.0.iso.state

# Resume with more workers
chunkguard resume --workers 16 /data/downloads/.chunkguard/app-v2.0.iso.state
```

### 5.4 Post-Download Verification

```bash
chunkguard verify \
  /data/downloads/app-v2.0.iso \
  "e3b0c44298fc1c149afbf4c8996fb924..."
```

### 5.5 Cleanup

```bash
# Remove state and chunk files for a completed download
rm -rf /data/downloads/.chunkguard/app-v2.0.iso.*

# Cleanup all completed state files (keeps failed for inspection)
find /data/downloads/.chunkguard/ -name "*.state" -exec \
  sh -c 'grep -q "COMPLETE" "$1" && rm -rf "${1%.state}"*' _ {} \;
```

---

## 6. Troubleshooting

### 6.1 Common Issues

| Symptom | Likely Cause | Solution |
|---|---|---|
| Download immediately fails | Server returns 403/404 | Check URL, authentication, server access |
| Very slow download | Single worker, small chunk size | Increase `--workers` and `--chunk-size` |
| Many chunk retries | Unreliable network or server throttling | Increase `--retries`, check network stability |
| "Disk full" error | Insufficient space | Need 2× file size during download; free space |
| Resume fails with "file changed" | Server file updated since last attempt | Delete state and restart |
| Hash mismatch on every retry | Server serving different content | Verify URL correctness, check CDN cache consistency |
| State file parse error | Corrupted state file | Delete `.state` file, restart download |
| SSL certificate error | Expired or untrusted certificate | Update CA certificates, or `--no-verify-ssl` (NOT recommended) |

### 6.2 Debug Mode

```bash
# Enable verbose debug logging
chunkguard download --verbose \
  "https://example.com/file.iso" \
  "/data/file.iso" 2>&1 | tee /var/log/chunkguard/debug.log
```

### 6.3 Network Diagnostics

```bash
# Test if server supports Range requests
curl -I -H "Range: bytes=0-0" https://example.com/file.iso

# Expected: HTTP 206 Partial Content
# If HTTP 200: Server doesn't support Range requests

# Test connectivity
curl -v --head https://example.com/file.iso
```

---

## 7. Automation & CI/CD Integration

### 7.1 Shell Script Example

```bash
#!/bin/bash
set -euo pipefail

URL="https://artifacts.example.com/build-${BUILD_ID}/artifact.tar.gz"
OUTPUT="/artifacts/artifact.tar.gz"
EXPECTED_HASH="${ARTIFACT_SHA256}"

echo "Downloading artifact for build ${BUILD_ID}..."

chunkguard download \
  --hash "sha256:${EXPECTED_HASH}" \
  --workers 4 \
  --retries 5 \
  "${URL}" "${OUTPUT}"

echo "✅ Download complete and verified"
```

### 7.2 GitHub Actions Example

```yaml
- name: Download large artifact
  run: |
    pip install chunkguard
    chunkguard download \
      --hash "sha256:${{ env.ARTIFACT_HASH }}" \
      --workers 4 \
      "${{ env.ARTIFACT_URL }}" \
      "./artifacts/model.bin"
```

### 7.3 Docker Compose Service

```yaml
services:
  downloader:
    image: chunkguard:latest
    volumes:
      - ./downloads:/downloads
      - ./config:/config:ro
    command: >
      download
        --config /config/chunkguard.yaml
        --hash "sha256:${FILE_HASH}"
        "${FILE_URL}"
        "/downloads/${FILE_NAME}"
    restart: "no"
```
