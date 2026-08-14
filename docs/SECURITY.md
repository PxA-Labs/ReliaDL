# Security Considerations — ChunkGuard

> **Audience**: Security Engineers, Auditors, DevOps
> **Reading time**: ~10 minutes

---

## 1. Threat Model

### 1.1 Assets Under Protection

| Asset | Sensitivity | Description |
|---|---|---|
| Downloaded file content | Variable (depends on file) | The actual bytes being transferred |
| File integrity | High | Assurance that content matches the original |
| State files | Low-Medium | Download progress metadata (no secrets) |
| Network credentials | High | Auth tokens passed via headers |
| System resources | Medium | CPU, disk, bandwidth (abuse prevention) |

### 1.2 Trust Boundaries

```
┌─────────────────────────────────────────────────────────────────┐
│  TRUSTED BOUNDARY: Local System                                  │
│                                                                  │
│  ┌──────────────┐     ┌──────────────┐     ┌────────────────┐   │
│  │ ChunkGuard   │     │ State Files  │     │ Chunk/Output   │   │
│  │ Process      │     │ (.state)     │     │ Files          │   │
│  └──────┬───────┘     └──────────────┘     └────────────────┘   │
│         │                                                        │
├─────────┼────────────────────────────────────────────────────────┤
│         │  TRUST BOUNDARY: Network                               │
│         │                                                        │
│         │  TLS (HTTPS)                                           │
│         │                                                        │
│         ▼                                                        │
│  ┌──────────────┐                                                │
│  │ HTTP Server  │  ◄── potentially untrusted                     │
│  │ / CDN        │                                                │
│  └──────────────┘                                                │
│                                                                  │
│  ┌──────────────┐                                                │
│  │ Network Path │  ◄── potentially hostile                       │
│  │ (ISP, WiFi)  │                                                │
│  └──────────────┘                                                │
└─────────────────────────────────────────────────────────────────┘
```

### 1.3 Threat Matrix

| # | Threat | Category | Likelihood | Impact | Mitigation |
|---|---|---|---|---|---|
| T1 | Man-in-the-Middle modifies bytes in transit | Tampering | Low (with HTTPS) | High | TLS encryption + SHA-256 verification |
| T2 | Server serves wrong/malicious content | Spoofing | Low | High | Expected hash verification against trusted source |
| T3 | DNS hijacking redirects to malicious server | Spoofing | Low | High | TLS certificate verification + expected hash |
| T4 | Chunk corruption due to network bit errors | Integrity | Medium | Medium | Per-chunk SHA-256 verification |
| T5 | Hash collision allows substitution | Integrity | Negligible | Critical | SHA-256 collision is computationally infeasible (~2¹²⁸) |
| T6 | State file manipulation to skip verification | Tampering | Low | High | State file permissions (0600), integrity checks |
| T7 | Denial of Service via resource exhaustion | Availability | Medium | Medium | Configurable limits, backpressure, disk space checks |
| T8 | Credential leakage in logs | Information Disclosure | Low | High | Headers with "auth"/"token"/"key" are redacted in logs |
| T9 | Path traversal via server-provided filenames | Elevation | Low | High | Output paths are user-specified, never derived from server |
| T10 | Symlink attacks in chunk directory | Elevation | Low | Medium | Resolve symlinks before writing, create dirs with restricted perms |

---

## 2. Security Controls

### 2.1 Transport Security

| Control | Implementation | Default |
|---|---|---|
| TLS version | Minimum TLS 1.2; TLS 1.0/1.1 rejected | Enforced |
| Certificate verification | `httpx` default CA bundle (Mozilla) | Enabled |
| Certificate pinning | Configurable via CA file path | Disabled |
| HTTPS enforcement | Warning logged if HTTP (not HTTPS) URL is used | Warning only |
| Redirect following | Follows up to 5 redirects; logs each redirect | Enabled |
| HTTP → HTTPS redirect | Followed and logged | Allowed |
| HTTPS → HTTP redirect | **Blocked** — logged as security warning | Blocked |

### 2.2 Data Integrity

| Control | Implementation |
|---|---|
| Per-chunk hash verification | SHA-256 computed during streaming download; compared immediately |
| Whole-file hash verification | SHA-256 computed during assembly; compared after |
| Hash comparison method | `hmac.compare_digest()` — constant-time comparison |
| Hash algorithm strength | SHA-256 minimum; MD5/SHA-1 explicitly rejected |
| Double verification | Hash computed during download AND re-verified during assembly |

### 2.3 File System Security

| Control | Implementation |
|---|---|
| Chunk file permissions | Created with mode 0600 (owner read/write only) |
| State file permissions | Created with mode 0600 |
| State directory permissions | Created with mode 0700 (owner only) |
| Temporary file names | Include random suffix to prevent prediction |
| Atomic writes | Write to temp file → fsync → rename (prevents partial writes) |
| Output path sanitization | User-specified only; never derived from server headers |
| Symlink resolution | All paths resolved with `Path.resolve()` before writing |
| Directory traversal prevention | Chunk paths validated to be within state directory |

### 2.4 Log Security

| Control | Implementation |
|---|---|
| Credential redaction | HTTP headers containing "auth", "token", "key", "secret", "password" are replaced with `[REDACTED]` in logs |
| URL redaction | Query parameters containing sensitive keys are redacted |
| File path exposure | Full paths are logged (necessary for debugging); no PII expected |
| Log file permissions | Created with mode 0600 |
| Structured logging | Machine-parseable JSON format prevents log injection attacks |

---

## 3. Cryptographic Details

### 3.1 SHA-256 Properties

```
Algorithm:          SHA-256 (Secure Hash Algorithm 2, 256-bit)
Standard:           FIPS 180-4 (NIST)
Output:             256 bits = 32 bytes = 64 hex characters
Block size:         512 bits = 64 bytes
Collision resistance: 2¹²⁸ operations (birthday attack bound)
Preimage resistance:  2²⁵⁶ operations
Second preimage:      2²⁵⁶ operations

Status (2026):      Considered secure for all integrity verification use cases
Known attacks:      None practical
```

### 3.2 Why Not Other Algorithms?

| Algorithm | Status | Reason Not Used |
|---|---|---|
| **MD5** | ❌ Broken | Practical collision attacks since 2004 |
| **SHA-1** | ❌ Broken | Collision demonstrated (SHAttered, 2017) |
| **CRC32** | ❌ Not cryptographic | Trivially forged; only for error detection, not security |
| **SHA-512** | ✅ Secure but slower | No meaningful security benefit over SHA-256 for integrity; slower on 32-bit |
| **BLAKE3** | ✅ Secure and fast | Not in Python stdlib; adds dependency; not yet NIST-approved |
| **SHA-256** | ✅ **Selected** | Secure, stdlib, hardware-accelerated, universally supported |

### 3.3 Hardware Acceleration

Modern CPUs include SHA-NI (SHA New Instructions) that accelerate SHA-256:

```
Platform              SHA-256 Throughput    SHA-NI Available
Intel (Sunny Cove+)   ~2,000 MB/s          Yes (since Ice Lake, 2019)
AMD (Zen+)            ~2,500 MB/s          Yes (since Zen, 2017)
Apple (M1+)           ~3,000 MB/s          Yes (ARMv8 SHA extensions)
Fallback (no SHA-NI)  ~400 MB/s            No (software only)
```

Python's `hashlib` automatically uses hardware acceleration when available via OpenSSL.

---

## 4. Attack Scenarios & Mitigations

### 4.1 Scenario: Compromised CDN

```
Attack:   CDN edge server returns modified file content
Detection: SHA-256 hash of downloaded chunk ≠ expected hash
Mitigation:
  1. Per-chunk hash catches modification immediately
  2. Chunk is marked FAILED and re-downloaded
  3. If CDN consistently returns bad data, chunk is ABANDONED after max retries
  4. User is notified of persistent integrity failure
  5. Whole-file hash provides final verification after assembly
  
Prerequisite: User must provide expected_hash from a trusted source
              (not from the same CDN that serves the file)
```

### 4.2 Scenario: Network Bit Flip

```
Attack:   Random bit error during transit (RAM error, noisy link)
Detection: SHA-256 of received bytes ≠ expected hash
Mitigation:
  1. Even a single bit flip changes the SHA-256 hash completely (avalanche effect)
  2. Chunk is automatically re-downloaded
  3. TCP checksums catch most transport errors, SHA-256 catches the rest
```

### 4.3 Scenario: State File Tampering

```
Attack:   Attacker modifies state file to mark all chunks as COMPLETE
          without actually downloading them
Detection: 
  1. Chunk files don't exist on disk → re-download triggered
  2. Chunk files exist but wrong size → re-download triggered  
  3. Whole-file hash verification catches any assembly from bad chunks
Mitigation:
  1. State file permissions (0600) prevent unauthorized writes
  2. Assembly re-verifies each chunk hash
  3. Whole-file hash is final gate
```

### 4.4 Scenario: Malicious Redirect

```
Attack:   Server redirects HTTP→HTTPS to a malicious server
Detection:
  1. TLS certificate verification catches domain mismatch
  2. HTTPS→HTTP downgrade redirect is blocked
  3. Final hash verification catches content substitution
Mitigation:
  1. Certificate verification enabled by default
  2. HTTPS downgrade protection
  3. Expected hash comparison (user-provided)
```

---

## 5. Security Checklist for Operators

- [ ] Always use HTTPS URLs (not HTTP)
- [ ] Always provide `--hash` from a trusted source (not the download server itself)
- [ ] Keep Python and OpenSSL updated for latest TLS/hash support
- [ ] Restrict file permissions on download directories
- [ ] Review logs for persistent integrity failures (may indicate active attack)
- [ ] Use `verify_ssl: true` in configuration (default)
- [ ] Do not store authentication tokens in config files (use environment variables)
- [ ] Monitor for unusual retry rates (may indicate network-level attack)

---

## 6. Compliance Notes

| Standard | Relevance | ChunkGuard Compliance |
|---|---|---|
| **FIPS 140-2** | Cryptographic module validation | Uses Python `hashlib` backed by OpenSSL (FIPS-validated builds available) |
| **NIST SP 800-131A** | Cryptographic algorithm recommendations | SHA-256 is approved through 2030+ |
| **SOC 2 Type II** | Security controls for data integrity | Per-chunk + whole-file verification; audit logging |
| **GDPR** | Data protection | No PII stored; downloads are user-initiated; logs contain no PII |
