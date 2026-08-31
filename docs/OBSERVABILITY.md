# Observability, Metrics & Monitoring — ChunkGuard

> **Audience**: SREs, DevOps, Platform Engineers, System Operators
> **Status**: Production Standard
> **Reading time**: ~15 minutes

---

## 1. Overview

ChunkGuard is designed with cloud-native, enterprise-grade observability across three telemetry pillars:
1. **Metrics**: Real-time Prometheus metrics for scrapers and Prometheus Pushgateway.
2. **Distributed Tracing**: OpenTelemetry (OTel) instrumentation for end-to-end distributed tracing across microservices.
3. **Structured Logging**: JSON-formatted log streams compatible with Datadog, Elasticsearch/Logstash/Kibana (ELK), Splunk, and AWS CloudWatch.

```
                     ┌───────────────────────────────┐
                     │     ChunkGuard Telemetry      │
                     └───────────────┬───────────────┘
                                     │
         ┌───────────────────────────┼───────────────────────────┐
         │                           │                           │
         ▼                           ▼                           ▼
┌───────────────────┐       ┌───────────────────┐       ┌───────────────────┐
│ Prometheus Metrics│       │  OpenTelemetry    │       │ Structured JSON   │
│ (Port 9100 / Push)│       │  (OTLP Exporter)  │       │ Logs (structlog)  │
└────────┬──────────┘       └────────┬──────────┘       └────────┬──────────┘
         │                           │                           │
         ▼                           ▼                           ▼
[ Grafana / PromQL ]        [ Jaeger / Datadog ]        [ ELK / CloudWatch ]
```

---

## 2. Prometheus Metrics Specification

### 2.1 Metrics Catalog

| Metric Name | Type | Labels | Description |
|---|---|---|---|
| `chunkguard_download_total` | Counter | `status` (success/failure) | Total number of download sessions initiated |
| `chunkguard_download_duration_seconds` | Histogram | `status`, `target_ext` | Total duration of download sessions in seconds |
| `chunkguard_chunks_total` | Counter | `status` (completed/failed/abandoned) | Total chunk downloads processed |
| `chunkguard_chunk_duration_seconds` | Histogram | `status`, `attempt` | Latency distribution of individual chunk downloads |
| `chunkguard_bytes_downloaded_total` | Counter | `direction` (inbound) | Cumulative payload bytes transferred |
| `chunkguard_download_throughput_bytes_per_second` | Gauge | `download_id` | Instantaneous download speed |
| `chunkguard_active_workers` | Gauge | `download_id` | Number of currently downloading coroutine workers |
| `chunkguard_hash_verification_failures_total` | Counter | `level` (chunk/file) | Number of SHA-256 integrity verification failures |
| `chunkguard_retry_attempts_total` | Counter | `reason` (timeout/5xx/corrupted) | Count of retry attempts triggered by retry policy |
| `chunkguard_disk_space_available_bytes` | Gauge | `mount_point` | Free disk space on the target partition |

### 2.2 Exporter Configuration

ChunkGuard can expose an embedded HTTP metrics endpoint or push to a Prometheus Pushgateway (ideal for ephemeral batch and CI/CD jobs):

```yaml
# chunkguard.yaml
telemetry:
  metrics:
    enabled: true
    mode: "pull"                        # "pull" (server) or "push" (pushgateway)
    listen_host: "0.0.0.0"
    listen_port: 9100
    path: "/metrics"
    pushgateway:
      url: "http://pushgateway.internal:9091"
      job_name: "chunkguard_batch"
      interval_seconds: 10
```

---

## 3. OpenTelemetry (OTel) Distributed Tracing

### 3.1 Trace Hierarchy

A complete ChunkGuard download produces a structured span hierarchy:

```
[ Span: chunkguard.download ] (root)
  │
  ├── [ Span: chunkguard.metadata_fetch ] (HEAD request)
  │
  ├── [ Span: chunkguard.chunk_plan ] (Compute boundaries)
  │
  ├── [ Span: chunkguard.worker_pool ]
  │     ├── [ Span: chunkguard.chunk_download (index=0) ]
  │     │     ├── [ Span: http.range_get (bytes 0-8388607) ]
  │     │     └── [ Span: hash.verify_stream ]
  │     ├── [ Span: chunkguard.chunk_download (index=1) ]
  │     └── [ Span: chunkguard.chunk_download (index=2) ]
  │
  ├── [ Span: chunkguard.assemble ] (Disk concatenation / sparse commit)
  │
  └── [ Span: chunkguard.whole_file_verify ] (Final SHA-256 pass)
```

### 3.2 Span Attributes Dictionary

* `chunkguard.download_id`: UUIDv4 of the download session
* `chunkguard.file_size_bytes`: Total byte size of the target resource
* `chunkguard.chunk_index`: Zero-based index of the specific chunk
* `chunkguard.byte_range`: String range (e.g. `bytes=8388608-16777215`)
* `chunkguard.sha256_computed`: Computed 64-character hex digest
* `chunkguard.retry_count`: Attempt number for the given chunk

---

## 4. Structured Logging Specification

Logs are output as single-line JSON objects conforming to the schema:

```json
{
  "timestamp": "2026-08-24T08:00:15.123456Z",
  "level": "INFO",
  "logger": "chunkguard.download_engine",
  "event": "chunk_download_complete",
  "download_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
  "chunk_index": 12,
  "size_bytes": 8388608,
  "duration_ms": 245.8,
  "throughput_mbps": 272.9,
  "sha256": "5e884898da28047151d0e56f8dc6292773603d0d6aabbdd62a11ef721d1542d8",
  "verified": true,
  "worker_id": 3
}
```

---

## 5. Prometheus Alerting Rules

```yaml
# chunkguard-alerts.rules.yaml
groups:
  - name: chunkguard_alerts
    rules:
      - alert: ChunkGuardHighFailureRate
        expr: rate(chunkguard_chunks_total{status="failed"}[5m]) / rate(chunkguard_chunks_total[5m]) > 0.10
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "ChunkGuard chunk failure rate exceeds 10%"
          description: "Download {{ $labels.download_id }} is experiencing high network drops or checksum failures."

      - alert: ChunkGuardIntegrityViolation
        expr: increase(chunkguard_hash_verification_failures_total{level="file"}[1m]) > 0
        for: 0m
        labels:
          severity: critical
        annotations:
          summary: "ChunkGuard whole-file hash mismatch detected"
          description: "Assembled artifact failed final cryptographic validation. Possible upstream tampering."

      - alert: ChunkGuardDownloadStalled
        expr: chunkguard_active_workers > 0 and chunkguard_download_throughput_bytes_per_second == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ChunkGuard download stalled"
          description: "Active workers present but 0 bytes transferred for > 5 minutes."
```

---

## 6. SRE Operational Runbook

### 6.1 Diagnosing Stalled Downloads
1. Check Prometheus metric `chunkguard_download_throughput_bytes_per_second`.
2. Inspect log stream for `event="worker_timeout"` or `event="http_429_rate_limited"`.
3. Verify if server `Accept-Ranges` was revoked or rate-limiting headers (`Retry-After`) are active.
4. Execute `chunkguard status <state-file> --json` to inspect worker queue backpressure.

### 6.2 Responding to Hash Corruption Alerts
1. Review `chunkguard_hash_verification_failures_total`.
2. Check if failure occurred at the chunk level (`level="chunk"`) or final file level (`level="file"`).
3. If persistent across retries, verify whether CDN edge nodes have stale or inconsistent cache revisions.
4. Run `chunkguard verify --file <path> --expected-hash <hash>` on a separate host to rule out local disk drive bit rot.
