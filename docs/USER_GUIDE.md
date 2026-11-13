# user guide and getting started reference — ReliaDL

> **Audience**: End Users
> **Reading time**: ~8 minutes

---

## 1. What Is ReliaDL?

ReliaDL is a tool that downloads large files reliably. Unlike regular download tools, if your internet drops or a download is corrupted, ReliaDL will:

- **Resume** from where it left off (not from the beginning)
- **Detect corruption** in any part of the file automatically
- **Re-download only the broken part**, not the whole file
- **Download faster** by using multiple connections at once

---

## 2. Installation

### Option 1: pip (Recommended)

```bash
pip install ReliaDL
```

### Option 2: From Source

```bash
git clone https://github.com/PxA-Labs/ReliaDL.git
cd ReliaDL
pip install .
```

### Verify Installation

```bash
reliadl --version
# ReliaDL v1.0.0
```

---

## 3. Basic Usage

### 3.1 Download a File

```bash
reliadl download --url "https://example.com/largefile.iso" --output "./largefile.iso"
```

You'll see a progress display:

```
reliadl 0.3.0 — Downloading largefile.iso

  ████████████████████░░░░░░░░░░  68.2%

  Downloaded:   69.1 GB / 100.0 GB
  Speed:        145.3 MB/s
  ETA:          4m 12s
```

### 3.2 Download with Verification

If you know the file's SHA-256 hash (often provided by the download page), include it for guaranteed integrity:

```bash
reliadl download \
  --url "https://example.com/largefile.iso" \
  --output "./largefile.iso" \
  --expected-hash "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
```

When the download finishes, ReliaDL will verify the entire file matches the expected hash.

### 3.3 Resume an Interrupted Download

If your download is interrupted (power outage, network drop, you close the terminal), just resume it:

```bash
reliadl resume --state-file "./.reliadl/largefile.iso.state"
```

ReliaDL will:
1. Load the saved state
2. Check which chunks are already downloaded
3. Download only the remaining chunks
4. Assemble and verify the complete file

### 3.4 Verify a Downloaded File

Already have a file and want to check if it's intact?

```bash
reliadl verify --file "./largefile.iso" --expected-hash "e3b0c44298fc1c149afbf4c8996fb924..."
```

Output:
```
[SUCCESS] File integrity verified
   File:     largefile.iso (100.0 GB)
   Hash:     e3b0c44298fc1c149afbf4c8996fb924...
   Duration: 42.3 seconds
```

Or if it's corrupted:
```
[FAIL] File integrity check FAILED
   File:      largefile.iso (100.0 GB)
   Expected:  e3b0c44298fc1c149afbf4c8996fb924...
   Computed:  9f8e7d6c5b4a3928170615049382716...
```

---

## 4. Advanced Usage

### 4.1 Faster Downloads (More Workers)

By default, ReliaDL uses 4 parallel connections. For faster servers:

```bash
reliadl download --workers 8 --url "https://example.com/file.iso" --output "./file.iso"
```

### 4.2 Custom Chunk Size

The default chunk size is 8 MB. For very large files, larger chunks reduce overhead:

```bash
reliadl download --chunk-size 32MB --url "https://example.com/huge.tar" --output "./huge.tar"
```

### 4.3 Custom Headers (Authentication)

For servers that require authentication:

**Custom request headers are not supported.** There is no `--header` flag and no
configuration key for arbitrary headers; an earlier revision of this guide showed
one. Only the User-Agent can be set:

```bash
export RELIADL_USER_AGENT="my-agent/1.0"
reliadl download --url "https://private.example.com/file.zip" --output "./file.zip"
```

For bearer-token or signed-URL sources, use a pre-signed URL, or the S3/GCS/Azure
adapters described in [CLOUD_ADAPTERS.md](CLOUD_ADAPTERS.md), which sign their own
requests.

### 4.4 Skip Final Verification

If you don't have an expected hash and want to skip the final whole-file verification:

There is no `--no-verify` flag. Whole-file verification is a configuration
setting:

```bash
RELIADL_NO_VERIFY=1 reliadl download \
  --url "https://example.com/file.iso" --output "./file.iso"
```

or in YAML:

```yaml
download:
  verify_on_complete: false
```

> **Note**: Per-chunk verification still runs — only the final whole-file hash check is skipped.

### 4.5 Check Download Status

View the progress of a download from its state file:

```bash
reliadl inspect-state --state-file "./.reliadl/largefile.iso.state"
```

```
Download Status: IN_PROGRESS
  URL:        https://example.com/largefile.iso
  Size:       100.0 GB
  Progress:   68.2% (8714 / 12800 chunks)
  Failed:     3 chunks (will retry)
  Started:    2026-01-15 10:30:00
  Elapsed:    8m 57s
```

### 4.6 Using a Configuration File

Create a `ReliaDL.yaml` in your current directory:

```yaml
download:
  chunk_size: "16MB"
  max_parallel_workers: 8

retry:
  max_attempts: 5

logging:
  level: "INFO"
```

ReliaDL automatically uses `./ReliaDL.yaml` if it exists.

### 4.7 Limit Download Speed

To prevent consuming all available bandwidth on shared networks:

```bash
reliadl download --limit-rate 10MB --url "https://example.com/largefile.iso" --output "./largefile.iso"
```

### 4.8 Corporate Proxy & Custom CA Certificates

Route traffic through an HTTP or SOCKS5 proxy:

There is no `--proxy` flag; the proxy is configured, not passed per invocation.

```bash
export RELIADL_NETWORK__PROXY__HTTPS_PROXY="http://proxy.corp.internal:8080"
reliadl download --url "https://example.com/build.iso" --output "./build.iso"
```

or in YAML:

```yaml
network:
  proxy:
    https_proxy: "http://proxy.corp.internal:8080"
```

SOCKS5 is supported via `socks_proxy`. Proxy credentials go in the URL
(`http://user:pass@host:port`); there is no separate header option.

### 4.9 Direct Sparse Write Mode (Save Disk Space)

If you have limited disk space and want to eliminate temporary chunk files:

```bash
RELIADL_DOWNLOAD__DIRECT_WRITE=true reliadl download \
  --url "https://example.com/huge.iso" --output "./huge.iso"
```

This allocates the exact file size immediately and writes chunks directly to disk positions without requiring temporary chunk files.

### 4.10 Download Using Signed Manifests

Download artifacts with guaranteed pre-authenticated chunk hashes:

Build the manifest once, sign it, then drive the download from it. The manifest
supplies per-chunk and per-64 KB sub-block digests, so a corrupt chunk is
abandoned at the first bad block and only the remainder is re-requested.

```bash
# Publisher side
reliadl manifest generate --file ./ubuntu-server.iso --chunk-size 16MB
reliadl manifest keygen --output ~/.reliadl/release.pem
reliadl manifest sign \
  --manifest ./ubuntu-server.iso.cgmanifest \
  --key ~/.reliadl/release.pem --key-id release-2026

# Consumer side
reliadl manifest verify \
  --manifest ./release.cgmanifest --public-key ./release.pem.pub
reliadl download \
  --url "https://cdn.example.com/ubuntu-server.iso" \
  --output "./ubuntu-server.iso" \
  --manifest "./release.cgmanifest"
```

The manifest fixes the chunk boundaries, so `--adaptive-chunks` has no effect
when one is supplied.

---

## 5. Understanding the Output

### 5.1 Files Created During Download

When you download to `./largefile.iso`, ReliaDL creates:

```
./
├── largefile.iso                              ← Final file (after assembly)
└── .ReliaDL/
    ├── largefile.iso.state                    ← Download state (for resume)
    └── largefile.iso.chunks/
        ├── 00000.chunk                        ← Chunk 0
        ├── 00001.chunk                        ← Chunk 1
        ├── 00002.chunk                        ← Chunk 2
        └── ...
```

After successful download, the `.ReliaDL` directory is cleaned up automatically.

### 5.2 Exit Codes

| Code | Meaning |
|---|---|
| `0` | [SUCCESS] File downloaded and verified |
| `1` | [ERROR] General error — see error message |
| `3` | [NETWORK] Network error — check internet connection |
| `4` | [INTEGRITY] Integrity error — file hash doesn't match |
| `5` | [STORAGE] Storage error — disk full or no permission |
| `10` | [CANCELLED] Cancelled by user (Ctrl+C) |

---

## 6. Frequently Asked Scenarios

### "My download was interrupted — what do I do?"

Just run `resume`:
```bash
reliadl resume --state-file "./.reliadl/filename.state"
```

### "How do I know if my download is intact?"

Use `verify`:
```bash
reliadl verify --file "./file.iso" --expected-hash "EXPECTED_SHA256_HASH"
```

### "It keeps retrying the same chunk and failing"

The server might be having issues. You can:
1. Wait and try again later: `ReliaDL resume state_file`
2. Try with more retries: `ReliaDL resume --retries 10 state_file`
3. Check if the URL is still valid

### "My download is very slow"

Try more parallel workers:
```bash
reliadl download --workers 8 --url URL --output OUTPUT
```

Or check if your network is the bottleneck (run a speed test).

### "How much disk space do I need?"

You need approximately **twice** the file size during download (once for chunks, once for the assembled file). After completion, only the final file remains.

### "Can I download multiple files at once?"

Run multiple `ReliaDL download` commands in separate terminals. Each download is independent.

### "Can I limit the download speed?"

Yes, in your config file:
```yaml
network:
  max_bandwidth: 52428800  # 50 MB/s in bytes
```

---

## 7. Tips for Best Performance

1. **Use more workers** on fast connections: `--workers 8` or `--workers 16`
2. **Use larger chunks** for very large files: `--chunk-size 32MB`
3. **Use smaller chunks** on unreliable connections: `--chunk-size 4MB` (less data to re-download on failure)
4. **Always provide a hash** when available — it guarantees your file is exactly what you expect
5. **Don't delete `.ReliaDL`** directory until the download is complete — it contains your resume data

---

## 8. Getting Help

```bash
# General help
reliadl --help

# Command-specific help
reliadl download --help
reliadl resume --help
reliadl verify --help
reliadl inspect-state --help
```

