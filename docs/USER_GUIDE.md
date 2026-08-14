# User Guide — ChunkGuard

> **Audience**: End Users
> **Reading time**: ~8 minutes

---

## 1. What Is ChunkGuard?

ChunkGuard is a tool that downloads large files reliably. Unlike regular download tools, if your internet drops or a download is corrupted, ChunkGuard will:

- **Resume** from where it left off (not from the beginning)
- **Detect corruption** in any part of the file automatically
- **Re-download only the broken part**, not the whole file
- **Download faster** by using multiple connections at once

---

## 2. Installation

### Option 1: pip (Recommended)

```bash
pip install chunkguard
```

### Option 2: From Source

```bash
git clone https://github.com/your-org/chunkguard.git
cd chunkguard
pip install .
```

### Verify Installation

```bash
chunkguard --version
# ChunkGuard v1.0.0
```

---

## 3. Basic Usage

### 3.1 Download a File

```bash
chunkguard download "https://example.com/largefile.iso" "./largefile.iso"
```

You'll see a progress display:

```
ChunkGuard v1.0.0 — Downloading largefile.iso

  ████████████████████░░░░░░░░░░  68.2%

  Downloaded:   69.1 GB / 100.0 GB
  Speed:        145.3 MB/s
  ETA:          4m 12s
```

### 3.2 Download with Verification

If you know the file's SHA-256 hash (often provided by the download page), include it for guaranteed integrity:

```bash
chunkguard download \
  --hash "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855" \
  "https://example.com/largefile.iso" \
  "./largefile.iso"
```

When the download finishes, ChunkGuard will verify the entire file matches the expected hash.

### 3.3 Resume an Interrupted Download

If your download is interrupted (power outage, network drop, you close the terminal), just resume it:

```bash
chunkguard resume "./.chunkguard/largefile.iso.state"
```

ChunkGuard will:
1. Load the saved state
2. Check which chunks are already downloaded
3. Download only the remaining chunks
4. Assemble and verify the complete file

### 3.4 Verify a Downloaded File

Already have a file and want to check if it's intact?

```bash
chunkguard verify "./largefile.iso" "e3b0c44298fc1c149afbf4c8996fb924..."
```

Output:
```
✅ File integrity verified
   File:     largefile.iso (100.0 GB)
   Hash:     e3b0c44298fc1c149afbf4c8996fb924...
   Duration: 42.3 seconds
```

Or if it's corrupted:
```
❌ File integrity check FAILED
   File:      largefile.iso (100.0 GB)
   Expected:  e3b0c44298fc1c149afbf4c8996fb924...
   Computed:  9f8e7d6c5b4a3928170615049382716...
```

---

## 4. Advanced Usage

### 4.1 Faster Downloads (More Workers)

By default, ChunkGuard uses 4 parallel connections. For faster servers:

```bash
chunkguard download --workers 8 "https://example.com/file.iso" "./file.iso"
```

### 4.2 Custom Chunk Size

The default chunk size is 8 MB. For very large files, larger chunks reduce overhead:

```bash
chunkguard download --chunk-size 32MB "https://example.com/huge.tar" "./huge.tar"
```

### 4.3 Custom Headers (Authentication)

For servers that require authentication:

```bash
chunkguard download \
  --header "Authorization:Bearer YOUR_TOKEN_HERE" \
  "https://private.example.com/file.zip" \
  "./file.zip"
```

### 4.4 Skip Final Verification

If you don't have an expected hash and want to skip the final whole-file verification:

```bash
chunkguard download --no-verify "https://example.com/file.iso" "./file.iso"
```

> **Note**: Per-chunk verification still runs — only the final whole-file hash check is skipped.

### 4.5 Check Download Status

View the progress of a download from its state file:

```bash
chunkguard status "./.chunkguard/largefile.iso.state"
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

Create a `chunkguard.yaml` in your current directory:

```yaml
download:
  chunk_size: "16MB"
  max_parallel_workers: 8

retry:
  max_attempts: 5

logging:
  level: "INFO"
```

ChunkGuard automatically uses `./chunkguard.yaml` if it exists.

---

## 5. Understanding the Output

### 5.1 Files Created During Download

When you download to `./largefile.iso`, ChunkGuard creates:

```
./
├── largefile.iso                              ← Final file (after assembly)
└── .chunkguard/
    ├── largefile.iso.state                    ← Download state (for resume)
    └── largefile.iso.chunks/
        ├── 00000.chunk                        ← Chunk 0
        ├── 00001.chunk                        ← Chunk 1
        ├── 00002.chunk                        ← Chunk 2
        └── ...
```

After successful download, the `.chunkguard` directory is cleaned up automatically.

### 5.2 Exit Codes

| Code | Meaning |
|---|---|
| `0` | ✅ Success — file downloaded and verified |
| `1` | ❌ General error — see error message |
| `3` | 🌐 Network error — check internet connection |
| `4` | 🔒 Integrity error — file hash doesn't match |
| `5` | 💾 Storage error — disk full or no permission |
| `10` | ⏹️ Cancelled — you pressed Ctrl+C |

---

## 6. Frequently Asked Scenarios

### "My download was interrupted — what do I do?"

Just run `resume`:
```bash
chunkguard resume "./.chunkguard/filename.state"
```

### "How do I know if my download is intact?"

Use `verify`:
```bash
chunkguard verify "./file.iso" "EXPECTED_SHA256_HASH"
```

### "It keeps retrying the same chunk and failing"

The server might be having issues. You can:
1. Wait and try again later: `chunkguard resume state_file`
2. Try with more retries: `chunkguard resume --retries 10 state_file`
3. Check if the URL is still valid

### "My download is very slow"

Try more parallel workers:
```bash
chunkguard download --workers 8 URL OUTPUT
```

Or check if your network is the bottleneck (run a speed test).

### "How much disk space do I need?"

You need approximately **twice** the file size during download (once for chunks, once for the assembled file). After completion, only the final file remains.

### "Can I download multiple files at once?"

Run multiple `chunkguard download` commands in separate terminals. Each download is independent.

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
5. **Don't delete `.chunkguard`** directory until the download is complete — it contains your resume data

---

## 8. Getting Help

```bash
# General help
chunkguard --help

# Command-specific help
chunkguard download --help
chunkguard resume --help
chunkguard verify --help
chunkguard status --help
```
