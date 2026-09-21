# ─────────────────────────────────────────────────────────────────────────────
# Stage 1: builder — install deps into a clean prefix
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-alpine AS builder

# Install build-time OS deps (needed to compile cryptography wheels on Alpine)
RUN apk add --no-cache \
        gcc \
        musl-dev \
        libffi-dev \
        openssl-dev \
        cargo

WORKDIR /build

# Copy only dependency manifests first to exploit layer caching
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY reliadl/ ./reliadl/

# Install into an isolated prefix so we can COPY just the result
RUN pip install --no-cache-dir --prefix=/install .

# ─────────────────────────────────────────────────────────────────────────────
# Stage 2: runtime — minimal Alpine image, non-root user
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-alpine AS runtime

LABEL org.opencontainers.image.title="ReliaDL"
LABEL org.opencontainers.image.description="Production-grade, fault-tolerant parallel file download framework with per-chunk SHA-256 cryptographic verification."
LABEL org.opencontainers.image.source="https://github.com/PxA-Labs/ReliaDL"
LABEL org.opencontainers.image.licenses="Apache-2.0"
LABEL org.opencontainers.image.vendor="PxA-Labs"

# Install only runtime OS deps (no build toolchain)
RUN apk add --no-cache libffi openssl

# Create a non-root user and group (UID/GID 1000)
RUN addgroup -g 1000 reliadl && \
    adduser -u 1000 -G reliadl -s /sbin/nologin -D reliadl

# Copy the installed package from builder
COPY --from=builder /install /usr/local

# Default download output directory — owned by the non-root user
RUN mkdir -p /downloads && chown 1000:1000 /downloads

# Drop privileges
USER 1000:1000

WORKDIR /downloads

# Prometheus metrics port (MetricsServer default)
EXPOSE 9090

# Health-check: verify the CLI entrypoint is importable
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD python -c "import reliadl; print('ok')" || exit 1

ENTRYPOINT ["reliadl"]
CMD ["--help"]
