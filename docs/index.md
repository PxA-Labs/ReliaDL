# ReliaDL

<div align="center">

[![PyPI - Version](https://img.shields.io/pypi/v/reliadl.svg?logo=pypi&logoColor=white&color=blue)](https://pypi.org/project/reliadl/)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/reliadl.svg?logo=python&logoColor=white)](https://pypi.org/project/reliadl/)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](https://github.com/PxA-Labs/ReliaDL/blob/master/LICENSE)
[![CI](https://github.com/PxA-Labs/ReliaDL/actions/workflows/ci.yml/badge.svg)](https://github.com/PxA-Labs/ReliaDL/actions/workflows/ci.yml)
[![CodeQL](https://github.com/PxA-Labs/ReliaDL/actions/workflows/codeql.yml/badge.svg)](https://github.com/PxA-Labs/ReliaDL/actions/workflows/codeql.yml)

**Production-grade, fault-tolerant parallel file download framework with per-chunk SHA-256 cryptographic verification.**

[Get Started](USER_GUIDE.md){ .md-button .md-button--primary }
[CLI Guide](CLI_GUIDE.md){ .md-button }
[API Reference](API_REFERENCE.md){ .md-button }

</div>

---

## What is ReliaDL?

ReliaDL is a **fault-tolerant file download engine** designed to reliably transfer large files over unreliable networks. Instead of downloading a file as a single monolithic stream — where any interruption means starting over — ReliaDL divides the file into small, independently verifiable **chunks**, downloads them in parallel, and reassembles them with cryptographic proof that every byte is correct.

## Key Capabilities

| Feature | Description |
| :--- | :--- |
| 🔐 **Cryptographic Integrity** | Per-chunk SHA-256 validation, homomorphic LtHash aggregation, and 4 KB Merkle tree segment localization |
| 🌐 **Proxy Tunneling** | SOCKS5 (RFC 1928/1929) and HTTP CONNECT corporate proxy tunneling with destination-based TLS verification |
| 🚦 **Traffic Pacing** | Token Bucket rate limiter with single-threaded reservation scheduling preventing thundering herd spikes |
| 📊 **Observability** | Native Prometheus metrics exporter and structured JSON logging engine |
| 🧠 **Stochastic Pacing** | Lyapunov-based dynamic chunk sizing (AdaChunk) and restless multi-armed bandit (Whittle index) scheduling |
| ☁️ **Cloud Adapters** | Plug-and-play streaming adapters for AWS S3, Google Cloud Storage, and Azure Blob Storage |

## Quick Install

```bash
pip install reliadl
```

## Quick Example

```python
import asyncio
from reliadl import DownloadConfig

async def main():
    config = DownloadConfig(
        chunk_size_bytes=8_388_608,   # 8 MB chunks
        max_parallel_workers=4,
        max_retries_per_chunk=3,
    )
    print("ReliaDL configured:", config)

asyncio.run(main())
```

## Why ReliaDL?

| Problem | Traditional Downloader | ReliaDL |
|---|---|---|
| Network drops mid-download | Restart from 0% | Resume from where it stopped |
| Downloaded file is silently corrupted | No detection | Every chunk is hash-verified on arrival |
| Slow single-threaded speed | One connection | Parallel chunks saturate bandwidth |
| Server timeout on large files | Download fails | Small chunks complete within timeout windows |
| Partial corruption in a 10 GB file | Re-download all 10 GB | Re-download only the corrupted chunk |

---

## Documentation Map

<div class="grid cards" markdown>

-   :material-rocket-launch: **Getting Started**

    ---

    New to ReliaDL? Start with the [User Guide](USER_GUIDE.md) or [Project Overview](PROJECT_OVERVIEW.md).

-   :material-console: **CLI Reference**

    ---

    Full command reference, flags, and config file format in the [CLI Guide](CLI_GUIDE.md).

-   :material-code-braces: **API Reference**

    ---

    Python SDK classes, functions, and type signatures in the [API Reference](API_REFERENCE.md).

-   :material-cog: **Architecture**

    ---

    Engine internals, novel algorithms, and data flow in the [Architecture Guide](ARCHITECTURE.md).

-   :material-shield-check: **Security**

    ---

    Cryptographic design, supply chain, and vulnerability reporting in the [Security Policy](SECURITY.md).

-   :material-chart-line: **Observability**

    ---

    Prometheus metrics catalog and structured logging in the [Observability Guide](OBSERVABILITY.md).

</div>
