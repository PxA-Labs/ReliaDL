# observability and monitoring, Metrics & Monitoring — ChunkGuard

> **Audience**: SREs, DevOps, Platform Engineers, System Operators
> **Status**: Production Standard
> **Reading time**: ~15 minutes

---

## 1. Overview

ChunkGuard is designed with cloud-native, enterprise-grade observability and monitoring across three telemetry pillars:
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

> **Status: Library, not wired.** `DownloadMetrics` is implemented and
> unit-tested, but `download_engine.py` never instantiates it, so a running
> transfer increments nothing. A `MetricsServer` started today serves these
> names with zero values indefinitely. The catalog below is what *would* be
> exported once the engine is wired to it; treat it as a specification, not as
> something you can scrape and alert on. Wiring it is tracked as outstanding
> work — it belongs on the per-chunk completion path, which is also the path
> that previously carried a per-chunk `fsync` regression, so it needs a
> before/after measurement rather than a quick patch.

These are the metrics `DownloadMetrics` actually registers. The list is
deliberately short, and it is the complete set — a dashboard or alert rule built
on any other name will never fire, because nothing emits it.

| Metric Name | Type | Labels | Description |
|---|---|---|---|
| `chunkguard_bytes_downloaded_total` | Counter | none | Total bytes successfully downloaded |
| `chunkguard_chunk_retries_total` | Counter | none | Total chunk download attempts that were retried |
| `chunkguard_chunk_download_duration_seconds` | Histogram | none | Wall-clock duration of individual chunk downloads |
| `chunkguard_chunk_throughput_bytes_per_second` | Histogram | none | Observed throughput of individual chunk downloads |
| `chunkguard_active_workers` | Gauge | none | Workers currently transferring a chunk |
| `chunkguard_download_progress_ratio` | Gauge | none | Fraction of the transfer completed, from 0 to 1 |

**On the `chunkguard_` prefix.** It is the default `namespace` argument of
`DownloadMetrics` and predates the rename to ReliaDL. It is kept because changing
it silently breaks every existing dashboard and recording rule. Pass
`DownloadMetrics(registry, namespace="reliadl")` to change it.

**No labels.** Every metric above is unlabelled. Earlier revisions of this
document described labels such as `status`, `download_id` and `mount_point`;
none are implemented, so no metric can be split by them.

### 2.2 Exporter Configuration

ReliaDL exposes an embedded HTTP metrics endpoint. There is no Pushgateway
support — an earlier revision of this document described a `mode` switch and a
`pushgateway` block, and neither key is read by the configuration loader.

```yaml
# reliadl.yaml
telemetry:
  metrics:
    enabled: true
    listen_host: "0.0.0.0"
    listen_port: 9100
    path: "/metrics"
```

The server itself is `MetricsServer` in `reliadl.telemetry`, started from the
code performing the transfer; there is no `reliadl metrics` subcommand. See
[DOCKER.md](DOCKER.md) section 4 for a worked example.

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
      - alert: ReliaDLHighRetryRate
        expr: rate(chunkguard_chunk_retries_total[5m]) > 0.5
        for: 2m
        labels:
          severity: warning
        annotations:
          summary: "ReliaDL chunk retry rate is elevated"
          description: "Sustained chunk retries indicate network drops, 5xx responses, or digest mismatches."

      - alert: ReliaDLDownloadStalled
        expr: chunkguard_active_workers > 0 and rate(chunkguard_bytes_downloaded_total[5m]) == 0
        for: 5m
        labels:
          severity: warning
        annotations:
          summary: "ReliaDL download stalled"
          description: "Active workers present but no bytes transferred for more than 5 minutes."

      - alert: ReliaDLSlowChunks
        expr: histogram_quantile(0.95, rate(chunkguard_chunk_download_duration_seconds_bucket[10m])) > 60
        for: 10m
        labels:
          severity: warning
        annotations:
          summary: "ReliaDL p95 chunk duration above 60s"
          description: "Chunks are completing slowly; check mirror health and bandwidth limits."
```

**There is no integrity alert**, because no metric counts digest mismatches.
`chunk_retries_total` rises when a chunk fails verification, but it rises for
network failures too and cannot distinguish them. An integrity counter is the
single most valuable addition to this catalog, given that integrity is what the
system is for; it does not exist yet, and an alert written against a name from
an earlier revision of this document would never fire.

---

## 6. SRE Operational Runbook

Until the engine is wired to `DownloadMetrics` (see section 2.1), these steps
rely on logs and the CLI rather than on metrics.

### 6.1 Diagnosing Stalled Downloads
1. Inspect the log stream for `event="worker_timeout"` or HTTP 429 rate-limiting.
2. Verify whether the server still advertises `Accept-Ranges`, or whether
   `Retry-After` is active.
3. Run `reliadl inspect-state <state-file>` to see which chunks are outstanding
   and how far each got.
4. Run `reliadl probe --url <url>` to re-check range support and reachability.

### 6.2 Responding to Hash Corruption
1. Run `reliadl manifest verify --manifest <manifest> --file <path>` to establish
   whether the artifact or the manifest is at fault, and whether the Merkle root
   and signature still hold.
2. With a manifest present the engine reports the failing chunk and sub-block
   index in its logs; without one, a whole-file mismatch is attributable to
   nothing more specific than the transfer.
3. If failures persist across retries from one source, re-run with
   `--mirror <alternate-url>` so routing can demote the offending mirror.
4. Run `reliadl verify --file <path> --expected-hash <hash>` on a separate host
   to rule out local disk corruption.
