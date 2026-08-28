# ReliaDL: Adaptive Fault-Tolerant Chunked Transfer with Homomorphic Verification and Stochastic Scheduling

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
[![Status: Research Prototype](https://img.shields.io/badge/Status-Research_Prototype-orange.svg)]()
[![Version: 2.0.0](https://img.shields.io/badge/Version-2.0.0-green.svg)]()

---

## Overview

**ReliaDL** is a research-oriented, fault-tolerant file transfer framework that formulates the reliable download problem as a constrained stochastic optimization over non-stationary channels. Unlike conventional download managers that employ static chunking and reactive retransmission (ARQ), ReliaDL introduces five novel algorithmic contributions spanning adaptive coding theory, algebraic verification, and optimal resource allocation.

The central research question addressed by this work is:

> *How can we minimize the expected data retransmission cost while guaranteeing byte-level integrity under non-stationary, bursty channel conditions, subject to throughput and resource constraints?*

ReliaDL provides both a theoretical framework with provable performance bounds and a practical Python-based reference implementation suitable for empirical evaluation.

---

## Novel Algorithmic Contributions

1. **AdaChunk -- Lyapunov-Based Adaptive Chunk Sizing.** Formulates chunk size selection as an online stochastic optimization problem. Using the Lyapunov drift-plus-penalty framework (Neely, 2010), AdaChunk dynamically adjusts chunk boundaries based on real-time network state observations, achieving an O(1/V) optimality gap with O(V) queue backlog tradeoff.

2. **LtHash -- Homomorphic Hash Aggregation.** Employs lattice-based homomorphic hashing to enable O(1) whole-file integrity verification post-assembly without re-reading the file from disk. The homomorphic property H(x || y) = H(x) + H(y) mod p allows algebraic accumulation of per-chunk hashes during download.

3. **Sub-Chunk Merkle Localization.** Embeds hierarchical Merkle trees within each chunk at 4 KB segment granularity. Upon detecting a chunk-level hash mismatch, the system localizes corruption to specific 4 KB segments in O(log(B/s)) comparisons, reducing retransmission by up to 99.95%.

4. **Predictive Parity Injection.** Implements proactive XOR-based forward error correction (FEC) with injection rate governed by a Gilbert-Elliott two-state Markov channel model estimator. Enables zero-RTT chunk recovery without network round trips under moderate loss conditions.

5. **Whittle Index Worker Scheduling.** Models multi-source worker allocation as a restless multi-armed bandit (RMAB) problem. Employs Whittle index policies (Whittle, 1988) for asymptotically optimal stochastic scheduling of download workers across heterogeneous sources.

---

## Key Mathematical Formulations

### AdaChunk Optimization Objective

```
min  lim_{T->inf} (1/T) * sum_{t=0}^{T-1} E[C(B_t, s_t)]
s.t. lim_{T->inf} (1/T) * sum_{t=0}^{T-1} E[G(B_t, s_t)] >= G_min

Where:
  B_t           = chunk size at time t (decision variable)
  s_t           = (RTT_t, sigma_RTT, p_t, G_t, BDP_t)  (network state)
  C(B, s)       = B * (1 - (1-p)^{B/MSS})              (retransmission cost)
  G(B, s)       = B*(1-p_retry) / (T_dl + T_overhead)   (goodput)
  Q_{t+1}       = max(Q_t - G(B_t, s_t) + G_min, 0)    (virtual queue)
  B_t*          = argmin V*C(B,s_t) - Q_t*G(B,s_t)      (per-slot decision)
```

### LtHash Homomorphic Aggregation

```
H_file = sum_{i=0}^{k-1} H_LtHash(chunk_i) mod p

Where H(x) = A * x mod p, A in Z_p^{n x m}, satisfying:
  H(x || y) = H(x) + H(y) mod p  (homomorphic property)
  Collision resistance reduces to SIS hardness
```

### Whittle Index Policy

```
W_i(s) = inf{ w : V_active(s, w) = V_passive(s, w) }

At each slot, activate K arms with highest W_i(s_i(t))
Achieves asymptotic optimality as N -> infinity (Weber & Weiss, 1990)
```

---

## Repository Structure

```
ReliaDL/
|-- README.md                              Project documentation entry point
|-- LICENSE                                Apache 2.0 License
|
|-- docs/
|   |-- PROJECT_OVERVIEW.md               Research problem formulation and scope
|   |-- ARCHITECTURE.md                   System architecture and component design
|   |-- NOVEL_ALGORITHMS.md               Core algorithmic contributions (full detail)
|   |-- TECHNICAL_SPECIFICATION.md        Protocol and algorithm specifications
|   |-- API_REFERENCE.md                  Python SDK and CLI API documentation
|   |-- DATA_FLOW.md                      State machines and data path diagrams
|   |-- ERROR_HANDLING.md                 Error taxonomy and recovery strategies
|   |-- SECURITY.md                       Threat model and cryptographic analysis
|   |-- DEPLOYMENT_GUIDE.md              Installation and operations guide
|   |-- USER_GUIDE.md                     End-user usage guide
|   |-- TESTING_STRATEGY.md              Test plans and evaluation methodology
|   |-- PERFORMANCE.md                    Benchmarks and complexity analysis
|   |-- CONTRIBUTING.md                   Contribution guidelines
|   |-- CHANGELOG.md                      Version history
|   |-- GLOSSARY.md                       Terminology index
|   |-- FAQ.md                            Frequently asked questions
|
|-- src/
|   |-- main.py                           CLI entry point
|   |-- download_engine.py                Asynchronous download orchestrator
|   |-- chunk_manager.py                  Chunk partitioning and boundary logic
|   |-- hash_verifier.py                  SHA-256 streaming verification
|   |-- state_manager.py                  Atomic state file persistence
|   |-- retry_handler.py                  Exponential backoff and retry policies
|   |-- file_assembler.py                 Chunk reassembly and final verification
|   |-- config.py                         Configuration parser and validation
|   |-- models.py                         Data models and schemas
|   |-- exceptions.py                     Exception hierarchy
|   |-- logger.py                         Structured JSON logging
|   |-- adachunk_optimizer.py             Lyapunov adaptive chunk sizing [Novel]
|   |-- homomorphic_hasher.py             LtHash homomorphic verification [Novel]
|   |-- merkle_localizer.py               Sub-chunk Merkle localization [Novel]
|   |-- parity_encoder.py                 Predictive parity FEC [Novel]
|   |-- whittle_scheduler.py              RMAB Whittle index scheduling [Novel]
|
|-- tests/
|   |-- unit/                             Unit test modules
|   |-- integration/                      Integration test modules
|   |-- benchmarks/                       Algorithmic performance benchmarks
|   |-- fixtures/                         Mock data and server fixtures
|
|-- config/
    |-- default_config.yaml               Default system configuration
```

---

## Quick Start

### Installation

```bash
git clone https://github.com/PxA-Labs/ReliaDL.git
cd ReliaDL
pip install -r requirements.txt
```

### Usage

```bash
# Execute a download with adaptive chunking and homomorphic verification
python -m src.main download \
  --url "https://example.com/dataset.tar.gz" \
  --output "./downloads/dataset.tar.gz" \
  --adachunk \
  --whittle

# Resume an interrupted transfer
python -m src.main resume \
  --state-file "./downloads/.reliadl/dataset.tar.gz.state"

# Verify file integrity
python -m src.main verify \
  --file "./downloads/dataset.tar.gz" \
  --expected-hash "sha256:abcdef1234567890..."
```

---

## Documentation Index

| Document | Audience | Description |
|---|---|---|
| [Project Overview](docs/PROJECT_OVERVIEW.md) | Researchers, Engineers | Problem formulation, research scope, and contributions |
| [Novel Algorithms](docs/NOVEL_ALGORITHMS.md) | Researchers | Full mathematical specification of all novel algorithms |
| [Architecture](docs/ARCHITECTURE.md) | System Architects | Component design and architectural decisions |
| [Technical Specification](docs/TECHNICAL_SPECIFICATION.md) | Implementers | Detailed protocols, schemas, and algorithm specifications |
| [API Reference](docs/API_REFERENCE.md) | Developers | Python SDK and CLI documentation |
| [Data Flow](docs/DATA_FLOW.md) | Core Maintainers | State transition models and execution sequences |
| [Error Handling](docs/ERROR_HANDLING.md) | Reliability Engineers | Exception taxonomy and fault escalation |
| [Security](docs/SECURITY.md) | Security Analysts | Threat model and cryptographic assurances |
| [Performance](docs/PERFORMANCE.md) | Performance Engineers | Benchmarks, complexity analysis, and tuning |
| [Deployment Guide](docs/DEPLOYMENT_GUIDE.md) | DevOps Engineers | Installation, configuration, and operations |
| [User Guide](docs/USER_GUIDE.md) | End Users | Command syntax and workflow examples |
| [Testing Strategy](docs/TESTING_STRATEGY.md) | QA Engineers | Test plans, fault injection, and coverage |
| [Contributing](docs/CONTRIBUTING.md) | Contributors | Development setup and contribution standards |
| [Glossary](docs/GLOSSARY.md) | All Readers | Index of technical terms |
| [FAQ](docs/FAQ.md) | All Readers | Common technical and operational questions |

---

## References

- Neely, M. J. (2010). *Stochastic Network Optimization with Application to Communication and Queueing Systems.* Morgan and Claypool.
- Whittle, P. (1988). Restless bandits: Activity allocation in a changing world. *Journal of Applied Probability*, 25(A), 287-298.
- Weber, R. R., & Weiss, G. (1990). On an index policy for restless bandits. *Journal of Applied Probability*, 27(3), 637-648.
- Merkle, R. C. (1987). A digital signature based on a conventional encryption function. *CRYPTO '87*, LNCS 293, 369-378.
- Elliott, E. O. (1963). Estimates of error rates for codes on burst-noise channels. *Bell System Technical Journal*, 42(5), 1977-1997.
- Fielding, R., & Reschke, J. (2014). Hypertext Transfer Protocol (HTTP/1.1): Range Requests. *RFC 7233*.
- Luby, M., et al. (2011). RaptorQ Forward Error Correction Scheme. *RFC 6330*.

---

## License

This project is licensed under the Apache License 2.0. See the [LICENSE](LICENSE) file for the complete terms.
