# Changelog — ChunkGuard

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [Unreleased]

### Planned
- Bandwidth throttling support
- HTTP proxy support
- Custom header presets
- S3 protocol adapter

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
- Deployment & Operations Guide
- User Guide
- Testing Strategy
- Performance Benchmarks
- Contributing Guide
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
