# Frequently Asked Questions — ReliaDL

> **Audience**: Everyone

---

## General

### What is ReliaDL?
ReliaDL is a fault-tolerant file download tool that splits large files into small, independently verifiable chunks, downloads them in parallel, verifies each chunk using SHA-256 hashing, and reassembles them into the original file. If any chunk is corrupted or fails, only that chunk is re-downloaded.

### Why not just use `wget` or `curl`?
Traditional download tools download files as a single stream. If the download is interrupted at 95%, you restart from 0%. If the file is silently corrupted, you won't know until you try to use it. ReliaDL solves both problems with chunked downloads and cryptographic verification.

### What programming language is ReliaDL written in?
Python 3.10+, using asyncio for concurrent downloads and the standard library's `hashlib` for SHA-256 hashing.

### Is ReliaDL free?
Yes. ReliaDL is released under the Apache License 2.0 — free for personal and commercial use with built-in patent protection.

---

## Downloads

### How large a file can ReliaDL handle?
ReliaDL has been tested with files up to 1 TB. There is no hard upper limit — the architecture supports arbitrarily large files by automatically adjusting chunk size to keep the chunk count manageable (≤ 100,000).

### How fast is ReliaDL compared to a normal download?
With 4 parallel workers, ReliaDL is typically 3–4× faster than a single-stream download. With 8 workers on a fast connection, speedups of 4–5× are common. The exact improvement depends on your network, the server, and disk speed.

### Does ReliaDL work with any server?
ReliaDL works with any HTTP/HTTPS server. For optimal performance (chunked + parallel downloads), the server must support HTTP Range requests (most modern servers and CDNs do). If the server doesn't support Range requests, ReliaDL falls back to a single-stream download with whole-file verification.

### How do I know if a server supports Range requests?
ReliaDL detects this automatically via a HEAD request before downloading. If you want to check manually:
```bash
curl -I -H "Range: bytes=0-0" https://example.com/file.iso
```
If you see `HTTP/1.1 206 Partial Content`, Range requests are supported.

### What happens if my internet drops during a download?
ReliaDL saves its state to a file after every chunk. When your internet is back, run `ReliaDL resume state_file` to pick up exactly where you left off. Only the incomplete chunk needs to be re-downloaded.

### Can I pause and resume a download?
Yes. Press Ctrl+C to pause (saves state). Run `ReliaDL resume state_file` to resume later — even days or weeks later, as long as the file hasn't changed on the server.

### What if the file changes on the server while I'm downloading?
ReliaDL tracks the server's ETag (version identifier). If the ETag changes between chunks, ReliaDL detects this, warns you, and requires a fresh download to avoid mixing old and new file versions.

---

## Integrity & Security

### How does ReliaDL detect corruption?
Every chunk is hashed using SHA-256 as its bytes arrive over the network. The computed hash is compared against the expected hash. SHA-256 is a cryptographic hash function where even a single bit change produces a completely different hash — making any corruption immediately detectable.

### What is SHA-256?
SHA-256 (Secure Hash Algorithm 256-bit) is a cryptographic hash function standardized by NIST (FIPS 180-4). It takes any input and produces a unique 64-character hexadecimal "fingerprint." It's used in TLS, Bitcoin, digital signatures, and file integrity verification worldwide.

### Can SHA-256 be fooled?
In theory, a hash collision (two different inputs producing the same hash) is possible but requires approximately 2¹²⁸ operations — more energy than the sun produces in its entire lifetime. For practical purposes, SHA-256 collisions are impossible.

### Why not use MD5 instead?
MD5 has known collision attacks — researchers have demonstrated practical collision generation since 2004. This means an attacker could craft a malicious file with the same MD5 hash as a legitimate file. SHA-256 has no known practical attacks.

### Is my download encrypted?
ReliaDL uses whatever transport the URL provides. HTTPS URLs are encrypted via TLS. HTTP URLs are not encrypted. Always use HTTPS for security. ReliaDL warns you if you use an HTTP URL.

### Where should I get the expected hash?
The expected hash should come from a **trusted source different from the download server**. Typically:
- The software project's official website
- A GPG-signed checksum file
- A package manager's repository metadata
- Your internal build system

**Never** trust a hash that comes from the same server as the file — if the server is compromised, both could be modified.

---

## Configuration & Tuning

### What's the best chunk size?
The default (8 MB) works well for most cases. Use smaller chunks (2–4 MB) on unreliable connections (less to re-download on failure). Use larger chunks (16–64 MB) on fast, stable connections (less overhead per chunk).

### How many workers should I use?
Start with the default (4). On fast connections to CDNs, try 8. Going above 16 rarely helps and may trigger server-side rate limiting. On slow or unreliable connections, 2 workers may be more reliable.

### Can I limit download speed?
Yes. Set `max_bandwidth` in your config file (in bytes per second):
```yaml
network:
  max_bandwidth: 10485760  # 10 MB/s
```

### How much disk space do I need?
During download: approximately 2× the file size (chunks + assembled output). After completion: just the file itself (chunks are cleaned up automatically).

### Can I download to a network drive or external disk?
Yes, but performance may be reduced depending on the drive's write speed. USB 2.0 external drives (30 MB/s) will bottleneck before most internet connections.

---

## Troubleshooting

### My download keeps failing on the same chunk
1. Check if the server is returning consistent content (CDN cache inconsistency)
2. Try with more retries: `--retries 10`
3. Try with a different chunk size to shift the byte boundaries
4. Check your network for intermittent issues
5. Try from a different network/VPN

### The progress bar shows 100% but the file hash doesn't match
This means all chunks downloaded successfully, but the assembled file doesn't match the expected hash. This can happen if:
1. The expected hash is wrong (double-check the source)
2. The file changed on the server during download (ETag check should catch this)
3. Disk corruption occurred after download (run `ReliaDL verify` again)

### "Server does not support Range requests" — what do I do?
ReliaDL will automatically fall back to a single-stream download. You lose parallel downloads and per-chunk retry, but whole-file hash verification still works. Contact the server administrator to request Range request support.

### State file is corrupted — can I recover?
1. Check for a backup at `filename.state.bak`
2. If no backup, you can try to rebuild: ReliaDL can re-verify existing chunk files on disk
3. As a last resort, delete the state file and chunk directory, then start a fresh download

### My antivirus/firewall is blocking ReliaDL
ReliaDL makes multiple simultaneous HTTP connections, which some security software flags as suspicious. You may need to whitelist ReliaDL or its Python process. The tool only makes outbound HTTPS requests — it does not listen on any ports or accept incoming connections.

### How does Direct Write mode save disk space?
When `--direct-write` is enabled, ChunkGuard uses `posix_fallocate` (or NTFS sparse file allocation) to pre-allocate the exact file size, and coroutine workers write chunks directly to their byte offsets via `os.pwrite()`. This avoids the temporary chunk directory, reducing disk space from 2.1× to exactly 1.0× of file size and eliminating post-download assembly delay.

### Can I download directly from private AWS S3, GCS, or Azure Blob buckets?
Yes. ChunkGuard provides native cloud adapters. Pass `s3://bucket/key`, `gs://bucket/object`, or `az://container/blob` URLs and ChunkGuard will authenticate using IAM roles, service accounts, or environment credentials and execute chunked range downloads.

### What is a `.cgmanifest` file?
A ChunkGuard Manifest is a cryptographically signed (Ed25519) JSON file that lists the SHA-256 hash of every chunk, the overall Merkle tree root, and alternate mirror URLs. It allows downloading and verifying chunks with pre-authenticated guarantees without trusting the origin server on first download.

---

## Comparison

### ReliaDL vs wget
| Feature | wget | ReliaDL |
|---|---|---|
| Resume | ✅ (single-stream) | ✅ (per-chunk) |
| Parallel downloads | ❌ | ✅ (configurable) |
| Hash verification | ❌ (manual) | ✅ (automatic per-chunk + whole-file) |
| Selective re-download | ❌ | ✅ |
| Corruption detection | ❌ | ✅ |

### ReliaDL vs aria2
| Feature | aria2 | ReliaDL |
|---|---|---|
| Parallel downloads | ✅ | ✅ |
| Per-chunk hash verification | ❌ | ✅ |
| Selective re-download | Partial | ✅ |
| BitTorrent support | ✅ | ❌ |
| Simplicity | Complex | Simple |

### ReliaDL vs BitTorrent
| Feature | BitTorrent | ReliaDL |
|---|---|---|
| Peer-to-peer | ✅ | ❌ (HTTP only) |
| Per-piece hash verification | ✅ | ✅ |
| Requires torrent file/magnet | ✅ | ❌ (just a URL) |
| Works with any HTTP server | ❌ | ✅ |
| Firewall friendly | ❌ (needs ports) | ✅ (outbound HTTPS only) |

