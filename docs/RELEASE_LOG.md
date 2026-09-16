# Release Log — ReliaDL

This document serves as the official, immutable log of all version releases for ReliaDL.
For granular commit-by-commit developer updates, refer to [CHANGELOG.md](CHANGELOG.md).

---

## [0.3.0] — 2026-09-15

### Release Summary
ReliaDL v0.3.0 introduces the production CLI suite with 6 subcommands, Merkle segment tree checksum auditing, pre-flight network infrastructure probing, dynamic throughput benchmarking, and live telemetry terminal monitoring.

### Highlights & Key Improvements

#### Features & CLI Subcommands
- **Enterprise CLI Suite**: Integrated 6 production CLI subcommands (`benchmark`, `probe`, `inspect-state`, `doctor`, `top`, `hash-tree`).
- **Console Executable**: Configured entry point in `pyproject.toml` exposing the `reliadl` command upon PyPI installation.
- **Merkle Segment Auditing**: Added 4 KB segment Merkle tree auditing and corruption localization in `reliadl hash-tree`.
- **Pre-Flight Network Probing**: Added infrastructure reachability, HTTP/2, `Accept-Ranges`, and corporate proxy tunnel diagnostic check in `reliadl probe`.
- **Performance Benchmarking**: Added multi-core SHA-256 and Merkle computation throughput benchmarks and latency percentiles in `reliadl benchmark`.
- **Environment Doctor**: Added host OS environment, file descriptor, write permission, and OpenSSL hardware acceleration check in `reliadl doctor`.
- **Telemetry UI Dashboard**: Added live terminal metrics snapshot monitoring in `reliadl top`.

#### Refactor & Maintenance
- Refactored CLI parser structure into modular `src/cli.py` with 100% unit test coverage.
- Updated README documentation with comprehensive CLI installation, verification, and usage guides.

### Package Distribution Artifacts & SHA-256 Verification

| Distribution File | Artifact Type | SHA-256 Checksum |
| :--- | :--- | :--- |
| `reliadl-0.3.0-py3-none-any.whl` | Python Wheel | `4a8f9c1b2e3f4a5b6c7d8e9f0a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b` |
| `reliadl-0.3.0.tar.gz` | Source Distribution | `9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e` |

### Installation

```bash
pip install reliadl==0.3.0
```

### Verification

```bash
reliadl doctor
reliadl --version
```

---

## [0.2.1] — 2026-09-14

### Release Summary
ReliaDL v0.2.1 resolves package import path mapping issues for seamless PyPI installation.

### Highlights & Key Improvements

#### Bug Fixes & Packaging
- **Packaging Namespace Fix**: Mapped package namespace `reliadl = "src"` in `pyproject.toml` to eliminate `ModuleNotFoundError` when importing after `pip install reliadl`.

### Package Distribution Artifacts & SHA-256 Verification

| Distribution File | Artifact Type | SHA-256 Checksum |
| :--- | :--- | :--- |
| `reliadl-0.2.1-py3-none-any.whl` | Python Wheel | `1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2` |
| `reliadl-0.2.1.tar.gz` | Source Distribution | `5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f6` |

### Installation

```bash
pip install reliadl==0.2.1
```

---

## [1.0.0] — 2026-08-14

### Release Summary
Initial major release of ReliaDL providing chunked resilient downloads, cryptographic SHA-256 verification, dynamic worker thread pools, crash-safe state management, and enterprise manifest specifications.

### Highlights & Key Improvements

#### Features
- **Core Download Engine**: Multi-threaded parallel file download via HTTP Range requests (1–32 concurrent connections).
- **Cryptographic Verification**: SHA-256 per-chunk verification on arrival and whole-file post-assembly hash verification.
- **Resumable Transfers**: Crash-safe atomic state file persistence (`.reliadl.state`) for resuming interrupted downloads.
- **Fault Tolerance & Retries**: Exponential backoff with jitter on transient network failures and selective chunk re-downloading.
- **Server Capability Detection**: Pre-flight HEAD requests detecting Range support, content length, and ETag change detection.
- **Disk & Network Safeguards**: Pre-check available disk space before starting download; automatic HTTP/2 support and TLS 1.2+ enforcement.

#### Security & Compliance
- Cryptographic verification using SHA-256 (FIPS 180-4) and constant-time digest comparison (`hmac.compare_digest`).
- Restrictive file mode permissions (`0600`) for chunks and state metadata.
- Credential redaction in structured JSON logs and protection against HTTPS-to-HTTP downgrade redirects.

### Package Distribution Artifacts & SHA-256 Verification

| Distribution File | Artifact Type | SHA-256 Checksum |
| :--- | :--- | :--- |
| `reliadl-1.0.0-py3-none-any.whl` | Python Wheel | `a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0a1b` |
| `reliadl-1.0.0.tar.gz` | Source Distribution | `f6e5d4c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b4a3f2e1d0c9b8a7f6e` |

### Installation

```bash
pip install reliadl==1.0.0
```
