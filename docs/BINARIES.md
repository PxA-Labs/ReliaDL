# Standalone CLI Executables — ReliaDL

> **Audience**: End Users, DevOps Engineers, Sysadmins
> **No Python installation required.**

---

## 1. Download

Pre-built binaries are attached to every [GitHub Release](https://github.com/PxA-Labs/ReliaDL/releases).

| Platform | Architecture | Filename |
|---|---|---|
| Linux | x86_64 | `reliadl-linux-amd64` |
| macOS | Apple Silicon (M1/M2/M3) | `reliadl-darwin-arm64` |
| macOS | Intel | `reliadl-darwin-amd64` |
| Windows | x86_64 | `reliadl-windows-amd64.exe` |

Download the binary for your platform from the [latest release](https://github.com/PxA-Labs/ReliaDL/releases/latest).

---

## 2. Verify Before Running

Every release includes a `SHA256SUMS` file and Sigstore `.sig` / `.crt` files. **Always verify checksums before executing a downloaded binary.**

### Quick checksum verification

=== "Linux / macOS"

    ```bash
    # Download the binary and checksum file
    curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/reliadl-linux-amd64
    curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/SHA256SUMS

    # Verify
    sha256sum --check --ignore-missing SHA256SUMS
    # Expected output: reliadl-linux-amd64: OK
    ```

=== "macOS (shasum)"

    ```bash
    curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/reliadl-darwin-arm64
    curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/SHA256SUMS

    shasum -a 256 --check --ignore-missing SHA256SUMS
    ```

=== "Windows (PowerShell)"

    ```powershell
    Invoke-WebRequest -Uri https://github.com/PxA-Labs/ReliaDL/releases/latest/download/reliadl-windows-amd64.exe -OutFile reliadl.exe
    Invoke-WebRequest -Uri https://github.com/PxA-Labs/ReliaDL/releases/latest/download/SHA256SUMS -OutFile SHA256SUMS

    $expected = (Get-Content SHA256SUMS | Select-String "reliadl-windows-amd64.exe").ToString().Split(" ")[0]
    $actual   = (Get-FileHash reliadl.exe -Algorithm SHA256).Hash.ToLower()
    if ($expected -eq $actual) { Write-Host "✅ Checksum OK" } else { Write-Host "❌ Checksum MISMATCH" }
    ```

### Sigstore / Cosign signature verification

```bash
# Install cosign: https://docs.sigstore.dev/system_config/installation/
cosign verify-blob \
  --certificate            reliadl-linux-amd64.crt \
  --signature              reliadl-linux-amd64.sig \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  --certificate-identity-regexp "https://github.com/PxA-Labs/ReliaDL/.github/workflows/binaries.yml" \
  reliadl-linux-amd64
# Output: Verified OK
```

---

## 3. Install

=== "Linux"

    ```bash
    # Make executable and move to PATH
    chmod +x reliadl-linux-amd64
    sudo mv reliadl-linux-amd64 /usr/local/bin/reliadl

    # Verify
    reliadl --version
    ```

=== "macOS"

    ```bash
    chmod +x reliadl-darwin-arm64
    sudo mv reliadl-darwin-arm64 /usr/local/bin/reliadl

    # macOS Gatekeeper — first run requires approval:
    xattr -d com.apple.quarantine /usr/local/bin/reliadl

    reliadl --version
    ```

=== "Windows"

    ```powershell
    # Move to a directory on your PATH, e.g. C:\Tools
    Move-Item reliadl-windows-amd64.exe C:\Tools\reliadl.exe

    # Verify
    reliadl --version
    ```

---

## 4. Quick Usage

```bash
# Show all commands
reliadl --help

# Run system doctor
reliadl doctor

# Probe a server for range-request support
reliadl probe --url https://example.com/file.iso

# Inspect download state
reliadl state --state-dir ./downloads/.reliadl_state

# Show telemetry stats
reliadl stats
```

---

## 5. How the Binary is Built

ReliaDL standalone binaries are compiled with **PyInstaller** on GitHub-hosted runners (no cross-compilation):

| Binary | Runner | Python |
|---|---|---|
| `reliadl-linux-amd64` | `ubuntu-latest` | 3.12 |
| `reliadl-darwin-arm64` | `macos-latest` | 3.12 |
| `reliadl-darwin-amd64` | `macos-13` | 3.12 |
| `reliadl-windows-amd64.exe` | `windows-latest` | 3.12 |

Every binary is:
1. **Smoke-tested** (`reliadl --help` must exit 0) before upload
2. **SHA-256 checksummed** into a consolidated `SHA256SUMS` manifest
3. **Signed** with [Sigstore Cosign](https://docs.sigstore.dev/) (keyless OIDC — no long-lived secrets)
4. **Attested** with SLSA build provenance via `actions/attest-build-provenance`

---

## 6. Supply Chain Verification

All release binaries carry verifiable SLSA build provenance:

```bash
gh attestation verify reliadl-linux-amd64 \
  --repo PxA-Labs/ReliaDL \
  --signer-workflow binaries.yml
```
