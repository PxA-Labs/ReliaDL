# Changelog — ReliaDL

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Features
- **ci**: Upgrade Conventional Commit changelog generator and add auto release log workflow ([#110](https://github.com/PxA-Labs/ReliaDL/pull/110))
- **dist**: System package manager distribution (Homebrew, WinGet, Scoop, DEB, RPM)
- Sub-block Merkle localization on the download path
- Reproducible benchmark harness, and two engine bugs it found
- Concurrency-constrained chunk sizing, and real CI gates
- Baseline comparison harness, and the pacing bug it exposed

### Bug Fixes
- **packaging**: Sync Dockerfile, reliadl.spec, and mkdocs.yml with master and reliadl package structure
- **ci**: Resolve template variables for nFPM in packages.yml
- **packaging**: Remove Markdown changelog parsing from nFPM spec and bundle as doc
- Reconcile documentation with implementation, wire algorithms into the engine
- Scope the PR placeholder check to our own source
- Pin mypy to platform = linux so the check is deterministic

### Documentation
- **release**: Add dedicated release log file and release log generator script
- **agents**: Enforce archittmittal-only GitHub account rule
- Correct dependency claims; surface silently swallowed state errors
- Evaluation results from 115 seeded runs

### Maintenance & Dependencies
- **deps**: Update mkdocs-material requirement from >=9.5.0 to >=9.7.7
- **deps**: Bump actions/attest-build-provenance from 2 to 4

### Changed
- **v0.3.0**: Update documentation for v0.3.0 release and standardize formatting (#106)
- **cli**: Add comprehensive CLI guide documentation (#108)

---

## [0.3.0] — 2026-09-15

### Added
- **Enterprise CLI Suite**: Integrated 6 production CLI subcommands (`benchmark`, `probe`, `inspect-state`, `doctor`, `top`, `hash-tree`).
- **Console Executable**: Configured `[project.scripts]` in `pyproject.toml` exposing `reliadl` CLI command upon PyPI installation.
- **Merkle Segment Auditing**: Added 4 KB segment Merkle tree auditing and corruption localization in `reliadl hash-tree`.
- **Pre-Flight Network Probing**: Added infrastructure reachability, HTTP/2, `Accept-Ranges`, and corporate proxy tunnel diagnostic check in `reliadl probe`.
- **Performance Benchmarking**: Added multi-core SHA-256 and Merkle computation throughput benchmarks and latency percentiles in `reliadl benchmark`.
- **Environment Doctor**: Added host OS environment, file descriptor, write permission, and OpenSSL hardware acceleration check in `reliadl doctor`.
- **Telemetry UI Dashboard**: Added live terminal metrics snapshot monitoring in `reliadl top`.

### Changed
- Refactored CLI parser structure into modular `src/cli.py` with full unit test coverage.
- Updated README documentation with comprehensive CLI installation, verification, and usage guides.

---

## [0.2.1] — 2026-09-14

### Fixed
- **Packaging**: Mapped package namespace `reliadl = "src"` in `pyproject.toml` to eliminate `ModuleNotFoundError` when importing after `pip install reliadl`.

---

## [1.0.0] — 2026-08-14

### Added
- **Core download engine** with chunked file downloads via HTTP Range requests
- **SHA-256 per-chunk verification** — every chunk is hash-verified on arrival
- **SHA-256 whole-file verification** — assembled file verified against expected hash
- **Parallel downloads** — configurable worker count (1–32 concurrent connections)
- **Resumable transfers** — persistent state file survives crashes and restarts
- **Automatic retries** — exponential backoff with jitter on transient failures
- **Selective re-download** — only failed/corrupted chunks are retried
- **CLI interface** — `download`, `resume`, `verify`, and `status` commands
- **Configuration system** — YAML config file with environment variable overrides
- **Structured logging** — JSON-formatted logs via structlog
- **Progress reporting** — real-time progress bar with speed, ETA, and chunk status
- **Atomic state file writes** — crash-safe state persistence via temp file + rename
- **Graceful shutdown** — Ctrl+C saves state before exiting; double-Ctrl+C force exits
- **Server capability detection** — HEAD request to detect Range support, file size, ETag
- **ETag change detection** — aborts and warns if file changes during download
- **Fallback mode** — single-stream download when server doesn't support Range requests
- **Chunk size auto-adjustment** — prevents > 100,000 chunks by increasing chunk size
- **Disk space pre-check** — verifies available space before starting download
- **HTTP/2 support** — enabled by default for improved multiplexing
- **TLS enforcement** — TLS 1.2+ required, certificate verification enabled by default

### Security
- SHA-256 (FIPS 180-4) for all hash operations
- Constant-time hash comparison via `hmac.compare_digest`
- File permissions: chunk/state files created with mode 0600
- Credential redaction in log output
- HTTPS downgrade protection (HTTPS→HTTP redirects blocked)

### Documentation
- Project Overview (non-technical)
- System Architecture
- Technical Specification
- API Reference
- Data Flow & State Machine
- Error Handling & Recovery
- Security Considerations
- Manifest Specification (.cgmanifest & Merkle trees)
- Cloud Storage & Protocol Adapters (AWS S3, GCS, Azure Blob, Proxies, HTTP/3)
- Observability, Metrics & Monitoring (Prometheus & OpenTelemetry)
- Deployment & Operations Guide
- User Guide
- Testing Strategy
- Performance Benchmarks
- Contributing Guide
- AI Agents & Mem0 Memory Management Guide
- Glossary
- FAQ

---

## Version History Format

### Types of Changes

- **Added** — new features
- **Changed** — changes in existing functionality
- **Deprecated** — soon-to-be removed features
- **Removed** — removed features
- **Fixed** — bug fixes
- **Security** — vulnerability fixes

