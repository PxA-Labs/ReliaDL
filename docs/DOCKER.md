# Docker & Container Deployment — ReliaDL

> **Audience**: DevOps Engineers, Platform Engineers, SRE Teams
> **Target Version**: ReliaDL v0.3.0+

---

## 1. Overview

ReliaDL ships official multi-architecture Docker images published to **GitHub Container Registry (GHCR)**. Images are built for `linux/amd64` and `linux/arm64` and automatically published on every tagged release.

| Registry | Image |
|---|---|
| GHCR | `ghcr.io/pxa-labs/reliadl` |
| Latest tag | `ghcr.io/pxa-labs/reliadl:latest` |
| Versioned tag | `ghcr.io/pxa-labs/reliadl:v0.3.0` |

---

## 2. Quick Start

```bash
# Pull the latest image
docker pull ghcr.io/pxa-labs/reliadl:latest

# Show help
docker run --rm ghcr.io/pxa-labs/reliadl:latest --help

# Run the system doctor check
docker run --rm ghcr.io/pxa-labs/reliadl:latest doctor

# Download a file into a local directory
docker run --rm \
  -v "$(pwd)/downloads:/downloads" \
  ghcr.io/pxa-labs/reliadl:latest \
  download --url https://example.com/file.iso --output /downloads/file.iso
```

---

## 3. Image Details

### Base Image & Size

| Property | Value |
|---|---|
| Base | `python:3.12-alpine` (multi-stage) |
| Target size | < 50 MB |
| Architecture | `linux/amd64`, `linux/arm64` |
| User | `reliadl` (UID/GID 1000:1000) — **non-root** |
| Working directory | `/downloads` |

### Exposed Ports

| Port | Protocol | Purpose |
|---|---|---|
| `9090` | TCP | Prometheus metrics scrape endpoint |

### Build Stages

```
builder  →  python:3.12-alpine
            ├── gcc, musl-dev, libffi-dev, openssl-dev, cargo
            ├── pip install reliadl (with all deps)
            └── output: /install prefix

runtime  →  python:3.12-alpine (clean)
            ├── libffi, openssl (runtime only, no build tools)
            ├── COPY /install from builder
            ├── USER 1000:1000
            └── ENTRYPOINT ["reliadl"]
```

---

## 4. Prometheus Metrics

To scrape Prometheus metrics from inside the container, start your code with the metrics server enabled and expose port 9090:

```bash
docker run --rm \
  -p 9090:9090 \
  -v "$(pwd)/downloads:/downloads" \
  ghcr.io/pxa-labs/reliadl:latest \
  metrics --port 9090
```

Verify:

```bash
curl http://localhost:9090/metrics
```

---

## 5. Docker Compose

```yaml
version: "3.9"

services:
  reliadl:
    image: ghcr.io/pxa-labs/reliadl:latest
    user: "1000:1000"
    ports:
      - "9090:9090"        # Prometheus metrics
    volumes:
      - ./downloads:/downloads
      - ./config.yaml:/etc/reliadl/config.yaml:ro
    environment:
      - RELIADL_LOG_LEVEL=INFO
    command: ["download", "--url", "https://example.com/file.iso", "--output", "/downloads/file.iso"]
    restart: on-failure:3

  prometheus:
    image: prom/prometheus:latest
    ports:
      - "9091:9090"
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
    depends_on:
      - reliadl
```

`prometheus.yml` scrape config:

```yaml
scrape_configs:
  - job_name: reliadl
    static_configs:
      - targets: ['reliadl:9090']
    scrape_interval: 15s
```

---

## 6. Security

- **Non-root runtime**: Container runs as `reliadl` (UID 1000:1000) — no root privileges at runtime.
- **Minimal attack surface**: Build toolchain (`gcc`, `cargo`) is discarded after the builder stage; runtime image contains only `libffi` and `openssl`.
- **OIDC attestations**: Every image published to GHCR includes a signed build provenance attestation verifiable with `cosign`.
- **Supply-chain verification**:

```bash
# Verify build provenance attestation
cosign verify-attestation \
  --type slsaprovenance \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp "https://github.com/PxA-Labs/ReliaDL/.github/workflows/docker.yml" \
  ghcr.io/pxa-labs/reliadl:latest
```

---

## 7. Available Tags

| Tag | Meaning |
|---|---|
| `latest` | Most recent build from `master` |
| `v0.3.0` | Exact release version |
| `v0.3` | Latest patch of minor 0.3 |
| `v0` | Latest patch of major 0 |
| `sha-<short>` | Immutable commit-pinned reference |
