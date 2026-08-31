# ReliaDL Technical Specification

## 1. HTTP Range Protocol Fundamentals

The ReliaDL system relies on the HTTP `Range` header (RFC 7233) for concurrent chunk downloading and partial recovery.

### 1.1 Range Header Specification
The `Range` header requests specific byte ranges of a resource. ReliaDL uses byte ranges exclusively:

`Range: bytes=<start>-<end>`

Where `<start>` and `<end>` are 0-indexed byte offsets, inclusive.

### 1.2 Accept-Ranges Validation
Before initiating ReliaDL downloads, the client must verify server support via the `Accept-Ranges: bytes` response header from a `HEAD` request. If unsupported, ReliaDL falls back to single-stream download without range parameters.

## 2. AdaChunk: Adaptive Chunk Sizing

ReliaDL employs AdaChunk, an adaptive chunk sizing algorithm based on online optimization via a Lyapunov drift-plus-penalty framework, superseding static chunk sizing.

### 2.1 Problem Formulation
The goal is to dynamically select chunk sizes $B_t$ for each slot $t$ to minimize expected retransmission costs while maintaining a minimum target goodput $G_{\text{min}}$.

The network state vector at time $t$ is defined as:
$s_t = (\text{RTT}_t, \sigma_{\text{RTT}}, p_t, G_t, \text{BDP}_t)$
Where $p_t$ is the packet loss rate and BDP is the Bandwidth-Delay Product.

### 2.2 Lyapunov Optimization Framework
We define a virtual queue $Q_t$ to track goodput deficit:
$Q_{t+1} = \max(Q_t - G(B_t, s_t) + G_{\text{min}}, 0)$

The per-slot optimization minimizes the drift-plus-penalty:
$B_t^* = \text{argmin}_B \left[ V \cdot C(B, s_t) - Q_t \cdot G(B, s_t) \right]$
Where $V$ is a control parameter weighting cost minimization against queue stability.

#### Retransmission Cost Model
$C(B, s) = B \cdot (1 - (1-p)^{B/\text{MSS}})$

#### Goodput Model
$G(B, s) = \frac{B \cdot (1-p_{\text{retry}})}{T_{\text{dl}} + T_{\text{overhead}}}$

### 2.3 AdaChunk Pseudocode
```python
def optimize_chunk_size(V, Q, s_t, B_candidates):
    best_B = None
    min_objective = float('inf')
    
    for B in B_candidates:
        cost = compute_cost(B, s_t)
        goodput = compute_goodput(B, s_t)
        
        objective = V * cost - Q * goodput
        if objective < min_objective:
            min_objective = objective
            best_B = B
            
    return best_B

def update_queue(Q_t, G_t, G_min):
    return max(Q_t - G_t + G_min, 0)
```

### 2.4 Convergence Proof Sketch
By bounding the Lyapunov drift $\Delta(L) \leq B + Q_t (G_{\text{min}} - \mathbb{E}[G(B_t, s_t)])$, it can be shown via standard drift-plus-penalty analysis that the time-average cost satisfies $\limsup_{T \to \infty} \frac{1}{T} \sum_{t=0}^{T-1} \mathbb{E}[C(B_t)] \leq C^* + O(1/V)$, achieving an $O(1/V)$ optimality gap while ensuring mean rate stability of the virtual queue.

### 2.5 Heuristic Chunk Size Guidelines
While AdaChunk determines precise sizes dynamically, initial candidate sets are governed by:
- High speed / low loss: 10MB - 50MB
- Moderate speed / moderate loss: 2MB - 10MB
- Poor connection / high loss: 512KB - 2MB

## 3. Cryptographic Verification

ReliaDL utilizes a dual-layer cryptographic verification system incorporating SHA-256 for per-chunk verification and LtHash for $O(1)$ whole-file verification, alongside Sub-Chunk Merkle Localization for fine-grained corruption recovery.

### 3.1 Dual-Layer Verification
1. **Per-Chunk Verification**: SHA-256 hash validation for individual chunk integrity.
2. **Whole-File Verification (LtHash)**: A lattice-based homomorphic hash function defined as $H(x) = A \cdot x \bmod p$.
   - **Homomorphic Property**: $H(x \Vert y) = H(x) + H(y)$.
   - **Performance**: Enables $O(1)$ assembly verification without re-reading the assembled file on disk. The sum of chunk LtHashes equals the LtHash of the entire file.
   - **Security**: Collision resistance is based on the Short Integer Solution (SIS) problem hardness.

### 3.2 Sub-Chunk Merkle Localization
To avoid re-downloading entire corrupt chunks, ReliaDL constructs hierarchical Merkle trees within chunks.

#### Tree Construction
- Leaf node size: $s = 4096$ bytes.
- Tree depth: $h = \lceil \log_2(B/s) \rceil$ levels.

#### Localization Algorithm
1. The server provides the Merkle root of the chunk.
2. Upon verification failure, the client requests intermediate nodes to localize the mismatch via binary search.
3. Requires $O(\log(B/s))$ hash comparisons.

#### Expected Retransmission Savings
$E[S] = B - (s \cdot \text{num\_errors} + \text{overhead} \cdot \log(B/s))$

## 4. State Management

ReliaDL persists download state to allow resumption across process restarts or network failures.

### 4.1 State File Schema (.reliadl)
Format: JSON.

Required Fields:
- `url`: Target resource URL
- `file_size`: Total bytes
- `chunk_size`: Base or initial chunk size
- `checksum`: Target whole-file hash (LtHash/SHA-256)
- `algorithm`: Hash algorithm identifier
- `chunks`: Array of chunk metadata objects
- `adachunk_state`: Network history matrix and virtual queue value

Chunk Metadata Object:
- `index`: Sequential identifier
- `start`: Byte offset
- `end`: Byte offset
- `status`: State indicator (PENDING, DOWNLOADING, COMPLETED, CORRUPT)
- `sha256`: Expected chunk hash
- `lthash_value`: Expected LtHash component
- `merkle_root`: Sub-chunk Merkle tree root
- `parity_group_id`: ID for predictive parity recovery
- `temp_file`: Path to temporary storage

## 5. Predictive Parity Injection

ReliaDL uses predictive forward error correction via lightweight XOR parity chunks to enable zero-RTT recovery.

### 5.1 Gilbert-Elliott Channel Model
Network burst packet loss is modeled using a two-state Markov chain (Good $G$, Bad $B$):
- Transition probabilities: $p_{GB}$ and $p_{BG}$.
- Error probabilities: $e_G = 0$, $e_B = 1$.

### 5.2 Parity Chunk Construction
For a group of $k$ data chunks, a parity chunk is constructed:
$P_j = \bigoplus_{i=1}^{k} \text{data\_chunk}_i$

### 5.3 Adaptive Injection Rate
The injection rate $r^*(t)$ (number of parity blocks per group) is dynamically adjusted based on the estimated loss probability $\hat{p}(t)$:
$r^*(t) = \left\lceil \frac{k \cdot \hat{p}(t)}{1 - \hat{p}(t)} \right\rceil$

### 5.4 Zero-RTT Recovery Protocol
If a single chunk within a parity group fails verification and a parity chunk is available, the client can reconstruct the corrupted chunk via XOR without issuing a retransmission request (zero additional RTT).

## 6. Whittle Index Scheduler

Worker-to-source allocation is formulated as a Restless Multi-Armed Bandit (RMAB) problem, solved using the Whittle Index policy to mitigate stragglers and optimize parallel downloads.

### 6.1 RMAB Formulation
Each chunk represents an arm with an evolving state based on download progress and connection quality.

### 6.2 Whittle Index Computation
For each chunk, the Whittle index $W(s)$ is the subsidy required to make the scheduler indifferent between serving and not serving the chunk in state $s$. Active workers are assigned to the chunks with the highest Whittle indices.

### 6.3 Indexability Conditions
The system assumes indexability holds, meaning the set of states for which it is optimal to remain idle monotonically increases with the subsidy.

### 6.4 Straggler Mitigation Protocol
Chunks assigned to workers exhibiting prolonged low throughput (triggering state transitions in the RMAB) have their active Whittle index re-evaluated. If preempted, the chunk is returned to the pool for reallocation.

## 7. Storage and Assembly

### 7.1 Temporary File Storage
- Chunks are downloaded to temporary files (e.g., `chunk_0.tmp`).
- Temporary files must be created on the same logical volume as the final output path to enable fast concatenation/moving.

### 7.2 Safe Assembly Procedure
1. Verify all chunk statuses are COMPLETED.
2. Initialize target file.
3. Iteratively append each temporary file in index order.
4. Verify homomorphic LtHash aggregation sum matches the whole-file LtHash.
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
- **Integrity Errors**: Hash mismatch. Action: Sub-chunk Merkle localization or Parity recovery.
- **I/O Errors**: Disk full, permissions. Action: Pause download, alert user.

## 10. Performance Tuning Variables

- `WORKER_COUNT`: Maximum concurrent download streams.
- `BDP_ESTIMATE_INITIAL`: Initial Bandwidth-Delay Product estimate.
- `LYAPUNOV_V`: V parameter for AdaChunk drift-plus-penalty.
- `MAX_RETRIES`: Hard limit on transient network error retries.
- `PARITY_GROUP_SIZE`: $k$ parameter for Predictive Parity (default 8).

## 10. Error Handling Specification

### 10.1 Error Categories

| Category | Examples | Retry? | User Action |
|---|---|---|---|
| **Configuration** | Invalid chunk size, bad URL format | No | Fix configuration |
| **Network Transient** | Timeout, connection reset, DNS failure | Yes | Automatic retry |
| **Network Permanent** | 404 Not Found, 403 Forbidden | No | Check URL / credentials |
| **Integrity** | Hash mismatch, truncated chunk | Yes | Automatic re-download |
| **Storage** | Disk full, permission denied | No | Free space / fix permissions |
| **State** | Corrupted state file | Partial | May need to restart download |
| **Server** | Range not supported, file changed | No | Fall back or restart |

### 10.2 Error Response Format

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

## 11. Security Specification

### 11.1 TLS Requirements

- TLS 1.2+ required (TLS 1.0/1.1 rejected)
- Certificate verification enabled by default
- Certificate pinning available via configuration

### 11.2 Hash Security

- SHA-256 is the minimum acceptable hash algorithm
- Hash comparisons use constant-time comparison (`hmac.compare_digest`)
- No support for weak algorithms (MD5, SHA-1) even in non-security contexts

### 11.3 File Permissions

```python
# Chunk files: owner read/write only
CHUNK_FILE_PERMISSIONS = 0o600

# State files: owner read/write only
STATE_FILE_PERMISSIONS = 0o600

# Output file: follows umask (typically 0o644)
OUTPUT_FILE_PERMISSIONS = None  # Use system default
```

---

## 12. Platform Compatibility

| Platform | Python Version | File System | Atomic Rename | Tested |
|---|---|---|---|---|
| Linux (x86_64) | 3.10+ | ext4, XFS, Btrfs | ✅ `os.replace()` | ✅ |
| macOS (arm64) | 3.10+ | APFS, HFS+ | ✅ `os.replace()` | ✅ |
| Windows 10+ (x86_64) | 3.10+ | NTFS | ✅ `os.replace()` | ✅ |
| Windows (FAT32) | 3.10+ | FAT32 | ⚠️ Non-atomic | ⚠️ Limited |

### Large File Support

- Files > 2 GB: Supported on all 64-bit platforms
- Files > 4 GB: Requires 64-bit Python and file system support (NTFS, ext4, APFS)
- Maximum tested file size: 1 TB

---

## 13. Direct Sparse File Writing Specification

When `--direct-write` is enabled, ChunkGuard bypasses the temporary chunk file staging directory and directly writes verified byte buffers into pre-allocated sparse target files:

### 13.1 Pre-Allocation Protocol
1. **POSIX Systems (Linux/macOS)**: Uses `posix_fallocate(fd, 0, file_size)` to allocate contiguous disk blocks and prevent mid-transfer disk-full crashes (`ENOSPC`).
2. **Windows (NTFS)**: Uses Win32 `SetFileInformationByHandle` or `SetFileValidData` for fast uninitialized file pre-allocation.

### 13.2 Concurrent Direct Writing (`os.pwrite`)
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

## 14. Bandwidth Throttling Specification (Token Bucket)

### 14.1 Mathematical Model
Let $R$ be the configured rate limit (bytes/sec) and $C = 2 \times R$ be the bucket capacity.
* At time $t$, elapsed time $\Delta t = t - t_{\text{last}}$.
* Tokens added: $T_{\text{new}} = \min(C, T_{\text{current}} + R \cdot \Delta t)$.
* For a chunk read request of size $B$ bytes:
  * If $T_{\text{new}} \ge B$: consume $B$ tokens and return immediately.
  * If $T_{\text{new}} < B$: compute required sleep duration $\Delta t_{\text{sleep}} = \frac{B - T_{\text{new}}}{R}$, await `asyncio.sleep(sleep_time)`, and consume $B$ tokens.

---

## 15. Cross-References

* For full Manifest details: see [MANIFEST_SPECIFICATION.md](MANIFEST_SPECIFICATION.md)
* For Cloud Protocol Adapters: see [CLOUD_ADAPTERS.md](CLOUD_ADAPTERS.md)
* For Telemetry & Tracing: see [OBSERVABILITY.md](OBSERVABILITY.md)
