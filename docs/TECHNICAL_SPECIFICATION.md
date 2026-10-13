# ReliaDL Technical Specification

## 1. HTTP Range Protocol Fundamentals

The ReliaDL system relies on the HTTP `Range` header (RFC 7233) for concurrent chunk downloading and partial recovery.

### 1.1 Range Header Specification
The `Range` header requests specific byte ranges of a resource. ReliaDL uses byte ranges exclusively:

`Range: bytes=<start>-<end>`

Where `<start>` and `<end>` are 0-indexed byte offsets, inclusive.

### 1.2 Accept-Ranges Validation
Before initiating ReliaDL downloads, the client must verify server support via the `Accept-Ranges: bytes` response header from a `HEAD` request. If unsupported, ReliaDL falls back to single-stream download without range parameters.

## 2. BL-DCS: Closed-Loop Chunk Sizing

> **Status: library only.** This controller is implemented in
> `reliadl/algorithms/adaptive_chunker.py` and unit-tested, but it is **not yet
> called by the download engine**, which uses a fixed `chunk_size_bytes`.
>
> Earlier revisions of this section described a Lyapunov drift-plus-penalty
> formulation with a virtual queue and an O(1/V) optimality theorem. **No such
> algorithm exists in the source**, and the theorem was a restatement of a standard
> result (Neely, 2010) rather than a proof about this system. The description below
> is what the code does.

### 2.1 Problem Formulation

Small chunks pay per-request overhead on every range GET; large chunks waste more
bytes when a chunk must be re-fetched. Minimizing per-byte cost over chunk size `S`:

```
J(S) = tau_overhead * mu / S  +  p_seg * S / MSS
```

Setting `dJ/dS = 0` gives a square-root optimum. This is the standard tradeoff
between a fixed per-batch cost and a cost linear in batch size; it is not novel.

### 2.2 Controller

```
S* = clamp( gamma * sqrt( tau_overhead * mu_k * MSS / (8 * p_k + eps) )
            + lambda * mu_k * tau_k,
            S_min, S_max )
```

The `MSS` factor makes the radicand a byte-squared quantity so the root yields
bytes. The second term scales with the bandwidth-delay product so a fast, clean path
is not throttled by request overhead. The result is quantized to a power of two.

The controller is **stateless**: each decision is a pure function of the current
smoothed network snapshot, with no history carried between epochs.

### 2.3 Per-Segment Loss Rescaling

The metrics collector observes failures per *chunk*; the loss model is expressed per
*segment*. The conversion applied before the formula is evaluated:

```
p_seg = 1 - (1 - p_chunk) ** (MSS / S_observed)
```

### 2.4 Known Open Issues

- `gamma` and `lambda` both default to `1.0` and have never been swept or fitted.
- The factor `8.0` in the denominator is not present in the derivation and is
  unexplained; it may be a bits/bytes error.
- `tau_overhead` defaults to 25 ms with no measured source.
- There is **no convergence proof and no optimality bound** for this controller.

### 2.5 Heuristic Chunk Size Guidelines
When BL-DCS is enabled it computes sizes from measured state; the static defaults and operator guidance are:
- High speed / low loss: 10MB - 50MB
- Moderate speed / moderate loss: 2MB - 10MB
- Poor connection / high loss: 512KB - 2MB

## 3. Cryptographic Verification

### 3.1 Per-Chunk Verification

Each chunk is verified against a SHA-256 digest (SHA-384 and SHA-512 are also
supported) computed incrementally over a 64 KB streaming buffer. Digests are compared
with `hmac.compare_digest`, which is constant-time. MD5 and SHA-1 are rejected by
policy (`hash_verifier.py:30`).

On a mismatch the **entire chunk is discarded and re-fetched**. There is currently no
sub-chunk localization on the download path.

### 3.2 Whole-File Verification

Whole-file verification re-reads the assembled file from disk and recomputes the
digest: an O(N) I/O pass (`hash_verifier.py:179-186`, invoked at
`download_engine.py:535`).

> Earlier revisions of this section specified a second layer using LtHash, a
> lattice-based homomorphic hash giving O(1) whole-file verification. **No homomorphic
> hashing is implemented.** If it is added later, it must be cited to Bellare and
> Micciancio (1997) and Lewi et al. (IACR ePrint 2019/227), and the O(1) claim must be
> stated precisely: aggregation is O(1) only *given* per-chunk hashes already computed
> in O(N).

### 3.3 Merkle Tree over Chunk Hashes

`reliadl/manifest.py` builds a binary Merkle tree over chunk hashes with domain
separation, `SHA-256(0x00 || leaf)` and `SHA-256(0x01 || left || right)`. Inclusion
proofs are available via `get_proof` / `verify_inclusion`. The root is carried in
RFC 8785-canonicalized, Ed25519-signed manifests.

**Known weakness.** Odd node counts duplicate the last node (`manifest.py:890`) and the
root does not commit to the leaf count, so two different chunk lists can share a root
(the CVE-2012-2459 shape). Since the root is signed, this must be fixed before the root
is relied on as a security boundary.

**Scope.** The tree is used by the manifest and by the offline `reliadl hash-tree`
audit command. `SubBlockStreamValidator` (`manifest.py:1001-1160`) aborts a stream at
the first bad 64 KB sub-block but has **no production call site**. Sub-chunk
*localization* — binary search down the tree to the corrupt segments — is **not
implemented**; earlier revisions of this document specified it as though it were.


## 4. State Management

ReliaDL persists download state to allow resumption across process restarts or network failures.

### 4.1 State File Schema (.reliadl)
Format: JSON.

Required Fields:
- `url`: Target resource URL
- `file_size`: Total bytes
- `chunk_size`: Base or initial chunk size
- `checksum`: Target whole-file SHA-256 hash
- `algorithm`: Hash algorithm identifier
- `chunks`: Array of chunk metadata objects

Chunk Metadata Object:
- `index`: Sequential identifier
- `start`: Byte offset
- `end`: Byte offset
- `status`: State indicator (PENDING, DOWNLOADING, COMPLETED, CORRUPT)
- `sha256`: Expected chunk hash
- `merkle_root`: Sub-chunk Merkle tree root
- `temp_file`: Path to temporary storage

## 5. Forward Error Correction (Not Implemented)

> Earlier revisions of this section specified predictive XOR parity injection driven
> by a Gilbert-Elliott channel estimator, with zero-RTT recovery. **No forward error
> correction of any kind is implemented in ReliaDL.** The section has been removed
> rather than kept as a specification, because nothing in the source corresponds to it.
>
> Loss recovery is handled by retry with exponential backoff and jitter
> (`download_engine.py:643-652`).


## 6. EXP3.S Mirror Routing

> **Status: library only.** Implemented in `reliadl/algorithms/mirror_bandit.py`;
> not yet called by the download engine.
>
> Earlier revisions described a Whittle-index restless-bandit scheduler with an
> indexability theorem. **The router is EXP3.S** (Auer, Cesa-Bianchi, Freund and
> Schapire, 2002), a different algorithm class: there is no subsidy parameter, no
> two-action MDP and no indexability argument anywhere in the source.

### 6.1 Formulation

Mirror selection is an adversarial bandit: only the reward of the mirror actually
used is observed, and throughput is non-stationary and driven by factors outside our
control (other tenants, routing, cache state).

```
p_{m,t}   = (1 - eta) * w_{m,t} / sum_j w_{j,t}  +  eta / M
r_hat     = r / p_{m,t}
w_{m,t+1} = w_{m,t} * exp(eta * r_hat / M)
```

Weights are maintained in log space and rescaled each round so the largest is 1,
which leaves the selection distribution unchanged while keeping every exponential
representable over long horizons.

### 6.2 Non-Stationary Tracking

The EXP3.S decay mixes weights toward the mean each round,
`w_m <- (1 - alpha) * w_m + (alpha / M) * sum_j w_j`, bounding how much evidence any
arm can accumulate. `alpha = 0` recovers classic EXP3. The default
`alpha = 0.0005` and the `alpha ~ 1/T` relationship are backed by a measured regret
sweep documented in [ALGORITHMS.md](ALGORITHMS.md#2-exp3s-mirror-routing).

`eta` defaults to 0.1 and has **not** been tuned.

### 6.3 Straggler Mitigation Protocol

A chunk is a straggler when its projected time-to-completion, `remaining / throughput`, exceeds a multiple of the minimum across *active* transfers; idle workers are excluded from the baseline. The outstanding range of a straggler is split at the midpoint of its unfetched remainder, so already-received bytes are not re-requested (`algorithms/work_stealer.py`).

## 7. Storage and Assembly

### 7.1 Temporary File Storage
- Chunks are downloaded to temporary files (e.g., `chunk_0.tmp`).
- Temporary files must be created on the same logical volume as the final output path to enable fast concatenation/moving.

### 7.2 Safe Assembly Procedure
1. Verify all chunk statuses are COMPLETED.
2. Initialize target file.
3. Iteratively append each temporary file in index order.
4. Re-read the assembled file and verify its whole-file SHA-256 digest.
5. If verified, remove temporary files and state file.
6. If verification fails, identify corrupt chunk via metadata and revert to DOWNLOADING state.

## 8. Network Operations

### 8.1 Connection Management
- Reuse HTTP connections where possible (HTTP Keep-Alive).
- Enforce strict timeouts (Connect, Read, Total).
- Implement exponential backoff for connection failures, distinct from algorithmic chunk retries.

### 8.2 Backpressure
Workers must implement flow control when writing to disk to prevent memory exhaustion if network throughput significantly exceeds disk write speeds.

## 9. Error Handling Specifications

### 9.1 Classification of Failures
- **Transient Network Errors**: Connection resets, timeouts. Action: Retry with backoff.
- **Protocol Errors**: 403 Forbidden, 404 Not Found. Action: Abort download.
- **Range Errors**: 416 Range Not Satisfiable. Action: Re-evaluate state, potentially abort.
- **Integrity Errors**: Chunk SHA-256 mismatch. Action: discard the whole chunk and re-fetch it under the retry policy.
- **I/O Errors**: Disk full, permissions. Action: Pause download, alert user.

## 10. Performance Tuning Variables

- `max_parallel_workers`: Maximum concurrent download streams.
- `chunk_size_bytes`: Fixed chunk size used by the engine today (BL-DCS is not yet wired in).
- `max_retries`, `retry_base_delay_seconds`, `retry_backoff_factor`, `retry_jitter_factor`:
  Transient-failure retry policy.
- `connect_timeout_seconds`, `read_timeout_seconds`: HTTP timeouts.
- BL-DCS coefficients (`gamma`, `lambda`, `overhead_seconds`) and the EXP3.S parameters
  (`eta`, `decay`) are constructor arguments on the library classes, not yet exposed as
  engine configuration.

## 11. Error Handling Specification

### 11.1 Error Categories

| Category | Examples | Retry? | User Action |
|---|---|---|---|
| **Configuration** | Invalid chunk size, bad URL format | No | Fix configuration |
| **Network Transient** | Timeout, connection reset, DNS failure | Yes | Automatic retry |
| **Network Permanent** | 404 Not Found, 403 Forbidden | No | Check URL / credentials |
| **Integrity** | Hash mismatch, truncated chunk | Yes | Automatic re-download |
| **Storage** | Disk full, permission denied | No | Free space / fix permissions |
| **State** | Corrupted state file | Partial | May need to restart download |
| **Server** | Range not supported, file changed | No | Fall back or restart |

### 11.2 Error Response Format

All errors include structured context for debugging:

```json
{
  "error_type": "ChunkHashMismatchError",
  "message": "Chunk 42 hash verification failed",
  "context": {
    "chunk_index": 42,
    "start_byte": 352321536,
    "end_byte": 360710143,
    "expected_hash": "a1b2c3d4e5f6...",
    "computed_hash": "9f8e7d6c5b4a...",
    "attempt": 2,
    "url": "https://example.com/file.iso"
  },
  "is_retryable": true,
  "timestamp": "2026-01-15T10:35:42.123Z"
}
```

---

## 12. Security Specification

### 12.1 TLS Requirements

- TLS 1.2+ required (TLS 1.0/1.1 rejected)
- Certificate verification enabled by default
- Certificate pinning available via configuration

### 12.2 Hash Security

- SHA-256 is the minimum acceptable hash algorithm
- Hash comparisons use constant-time comparison (`hmac.compare_digest`)
- No support for weak algorithms (MD5, SHA-1) even in non-security contexts

### 12.3 File Permissions

```python
# Chunk files: owner read/write only
CHUNK_FILE_PERMISSIONS = 0o600

# State files: owner read/write only
STATE_FILE_PERMISSIONS = 0o600

# Output file: follows umask (typically 0o644)
OUTPUT_FILE_PERMISSIONS = None  # Use system default
```

---

## 13. Platform Compatibility

| Platform | Python Version | File System | Atomic Rename | Tested |
|---|---|---|---|---|
| Linux (x86_64) | 3.10+ | ext4, XFS, Btrfs | [YES] `os.replace()` | [YES] |
| macOS (arm64) | 3.10+ | APFS, HFS+ | [YES] `os.replace()` | [YES] |
| Windows 10+ (x86_64) | 3.10+ | NTFS | [YES] `os.replace()` | [YES] |
| Windows (FAT32) | 3.10+ | FAT32 | [LIMITED] Non-atomic | [LIMITED] |

### Large File Support

- Files > 2 GB: Supported on all 64-bit platforms
- Files > 4 GB: Requires 64-bit Python and file system support (NTFS, ext4, APFS)
- Maximum tested file size: 1 TB

---

## 14. Direct Sparse File Writing Specification

When `--direct-write` is enabled, ChunkGuard bypasses the temporary chunk file staging directory and directly writes verified byte buffers into pre-allocated sparse target files:

### 14.1 Pre-Allocation Protocol
1. **POSIX Systems (Linux/macOS)**: Uses `posix_fallocate(fd, 0, file_size)` to allocate contiguous disk blocks and prevent mid-transfer disk-full crashes (`ENOSPC`).
2. **Windows (NTFS)**: Uses Win32 `SetFileInformationByHandle` or `SetFileValidData` for fast uninitialized file pre-allocation.

### 14.2 Concurrent Direct Writing (`os.pwrite`)
Each worker writes directly to its chunk offset using positional write operations:

```python
def write_chunk_direct(fd: int, start_byte: int, data: bytes) -> int:
    """
    Thread-safe / Coroutine-safe positional write.
    Does not modify the shared file descriptor seek pointer.
    """
    return os.pwrite(fd, data, start_byte)
```

---

## 15. Bandwidth Throttling Specification (Token Bucket)

### 15.1 Mathematical Model
Let $R$ be the configured rate limit (bytes/sec) and $C = 2 \times R$ be the bucket capacity.
* At time $t$, elapsed time $\Delta t = t - t_{\text{last}}$.
* Tokens added: $T_{\text{new}} = \min(C, T_{\text{current}} + R \cdot \Delta t)$.
* For a chunk read request of size $B$ bytes:
  * If $T_{\text{new}} \ge B$: consume $B$ tokens and return immediately.
  * If $T_{\text{new}} < B$: compute required sleep duration $\Delta t_{\text{sleep}} = \frac{B - T_{\text{new}}}{R}$, await `asyncio.sleep(sleep_time)`, and consume $B$ tokens.

---

## 16. Cross-References

* For full Manifest details: see [MANIFEST_SPECIFICATION.md](MANIFEST_SPECIFICATION.md)
* For Cloud Protocol Adapters: see [CLOUD_ADAPTERS.md](CLOUD_ADAPTERS.md)
* For Telemetry & Tracing: see [OBSERVABILITY.md](OBSERVABILITY.md)
