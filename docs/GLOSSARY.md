# Glossary — ReliaDL

> **Audience**: Everyone
> **Purpose**: Consistent terminology across all documentation

---

| Term | Definition |
|---|---|
| **Abandoned** | A chunk state indicating that all retry attempts have been exhausted without success. Requires manual intervention (re-run `resume` command) or fresh download. |
| **Assembly** | The process of concatenating all verified chunks in sequential order to produce the final output file. Includes re-verification of each chunk hash during concatenation. |
| **Atomic Write** | A write operation that either completes fully or not at all — no partial writes. Achieved via writing to a temporary file, fsyncing, then atomically renaming to the final path. |
| **Avalanche Effect** | A property of cryptographic hash functions where a single bit change in the input produces a drastically different hash output. This is what makes corruption detectable. |
| **Backoff** | A retry strategy where the delay between retries increases over time. ReliaDL uses **exponential backoff** (delay doubles each attempt) with **jitter** (random variation to prevent thundering herd). |
| **Backpressure** | A flow control mechanism that slows down data producers (network downloads) when data consumers (disk writes) can't keep up. Prevents memory exhaustion. |
| **Byte Range** | A contiguous section of a file specified by a start byte (inclusive) and end byte (inclusive). Used in HTTP Range headers to request specific portions of a file. |
| **CDN** | Content Delivery Network — a geographically distributed network of servers that cache and serve files closer to users. CDNs typically support Range requests. |
| **Chunk** | A fixed-size segment of the original file. Default size is 8 MB. Each chunk is independently downloadable, hashable, and verifiable. The last chunk may be smaller than the configured chunk size. |
| **Chunk Hash** | The SHA-256 cryptographic hash of a single chunk's bytes. Used to verify that the chunk was downloaded correctly. |
| **Chunk Index** | A zero-based integer uniquely identifying a chunk's position in the file. Chunk 0 contains the first bytes of the file. |
| **ChunkSpec** | A data structure defining a chunk's byte range (start_byte, end_byte), index, and expected hash. Created during the planning phase. |
| **Collision** | When two different inputs produce the same hash output. For SHA-256, this requires ~2¹²⁸ operations and is considered computationally infeasible. |
| **Constant-Time Comparison** | A string comparison method that takes the same amount of time regardless of how many characters match. Prevents timing attacks when comparing hashes. Implemented via `hmac.compare_digest()`. |
| **Direct Write** | A zero-assembly download mode where chunk bytes are written directly to pre-allocated disk offsets via `os.pwrite()`, avoiding temporary chunk files. |
| **Download Engine** | The core orchestrator component that manages the entire download lifecycle: metadata fetch, chunk planning, parallel dispatch, and completion detection. |
| **Download State** | A complete snapshot of a download's progress, including all chunk statuses, byte ranges, computed hashes, and statistics. Serialized to a JSON state file for persistence. |
| **Ed25519** | A high-speed, secure elliptic curve signature scheme (EdDSA using Curve25519) used to cryptographically sign ChunkGuard manifests. |
| **ETag** | An HTTP response header containing a version identifier for the resource. Used by ChunkGuard to detect if the file changes on the server during a download. |
| **Exponential Backoff** | A retry delay strategy where the wait time doubles after each failed attempt: 1s → 2s → 4s → 8s. Prevents overwhelming a struggling server. |
| **Fault Tolerance** | The system's ability to continue operating correctly when components fail. ReliaDL tolerates network failures, chunk corruption, and process crashes. |
| **File Assembler** | The component responsible for concatenating verified chunks into the final output file and performing whole-file hash verification. |
| **File Hash** | The SHA-256 hash of the entire assembled file. Used as the final integrity check after all chunks are concatenated. |
| **Graceful Degradation** | The ability to fall back to a simpler mode when advanced features are unavailable. Example: falling back to single-stream download when the server doesn't support Range requests. |
| **Graceful Shutdown** | An orderly shutdown process: finish active chunk downloads, save current state, close connections, exit. Triggered by Ctrl+C (first press). |
| **Hash Mismatch** | When a computed hash doesn't match the expected hash. Indicates data corruption or tampering. The chunk/file must be re-downloaded. |
| **Hash Verifier** | The component responsible for computing SHA-256 hashes and comparing them against expected values. |
| **HEAD Request** | An HTTP request that retrieves only response headers (no body). Used to determine file size, Range support, and ETag without downloading any data. |
| **Hex Digest** | The hash output represented as a lowercase hexadecimal string. SHA-256 produces a 64-character hex digest. Example: `e3b0c44298fc1c14...` |
| **HTTP CONNECT** | An HTTP method used to establish a raw TCP tunnel through forward proxies, typically for end-to-end encrypted HTTPS transfers. |
| **HTTP Range Request** | An HTTP mechanism (RFC 7233) that allows requesting a specific byte range of a resource. Uses the `Range` header in the request and returns `206 Partial Content`. |
| **Idempotent** | An operation that produces the same result regardless of how many times it's executed. All ReliaDL operations are idempotent — safe to retry. |
| **Jitter** | Random variation added to retry delays to prevent multiple workers from retrying at exactly the same time (thundering herd problem). |
| **Manifest (`.cgmanifest`)** | A machine-readable, cryptographically signed catalog defining chunk boundaries, SHA-256 hashes, Merkle tree root, and mirror endpoints. |
| **Mem0** | An intelligent AI agent memory management layer utilized in ChunkGuard development for retaining developer context and configuration facts. |
| **Merkle Tree** | A cryptographic binary tree where leaf nodes represent chunk hashes and parent nodes represent parent hashes. Used in manifests for single-root authentication. |
| **OpenTelemetry** | An open observability framework used by ChunkGuard to generate distributed tracing spans and metrics for APM monitoring. |
| **Partial Content** | HTTP status code 206, indicating the server is returning only the requested byte range (not the entire file). |
| **posix_fallocate** | A POSIX system call that pre-allocates contiguous physical storage for a file on disk, avoiding `ENOSPC` errors and disk fragmentation. |
| **Progress Report** | A snapshot of download progress at a point in time, including bytes downloaded, speed, ETA, and chunk statuses. |
| **Range Support** | A server's ability to handle HTTP Range requests and return partial content. Detected via the `Accept-Ranges: bytes` header in a HEAD response. |
| **Retry Handler** | The component implementing retry logic with exponential backoff and jitter. Classifies errors as retryable or non-retryable. |
| **Retry Policy** | Configuration defining retry behavior: max attempts, base delay, max delay, backoff factor, jitter factor, and retryable error types. |
| **Selective Re-download** | The ability to re-download only specific failed or corrupted chunks, rather than the entire file. The key advantage of chunked downloads. |
| **Semaphore** | A concurrency primitive that limits the number of simultaneous operations. Used to cap the number of active download workers. |
| **SHA-256** | Secure Hash Algorithm 2 with 256-bit output. A NIST-standardized cryptographic hash function (FIPS 180-4). Produces a unique 64-character hexadecimal fingerprint for any input. |
| **SOCKS5** | An internet protocol (RFC 1928) that routes network packets between client and server via a proxy, supporting authentication and remote DNS. |
| **Sparse File** | A type of file that reserves logical address space without allocating physical disk blocks until bytes are written. |
| **Streaming Hash** | Computing a hash incrementally as data arrives, rather than hashing all data at once. Enables bounded memory usage regardless of chunk size. |
| **State File** | A JSON file persisting the complete download state to disk. Enables resuming after crashes. Located at `.ReliaDL/<filename>.state`. |
| **State Manager** | The component responsible for reading, writing, and validating state files. Uses atomic writes for crash safety. |
| **Thundering Herd** | A problem where many processes retry simultaneously after a failure, overwhelming the recovering server. Mitigated by adding random jitter to retry delays. |
| **Token Bucket** | A traffic-shaping rate-limiting algorithm that regulates byte streaming throughput by accumulating and consuming capacity tokens. |
| **Trust-on-First-Download** | When no chunk hashes are pre-known, the system trusts the first download's hashes and uses them to verify subsequent retries. |
| **Worker** | An async coroutine that downloads, hashes, and stores a single chunk. Multiple workers run concurrently within the asyncio event loop. |
| **Worker Pool** | The set of concurrent workers managed by the Download Engine. Size is configurable via `max_parallel_workers`. |

