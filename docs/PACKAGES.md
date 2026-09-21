# System Package Managers & OS Distribution — ReliaDL

> **Audience**: Systems Administrators, DevOps Engineers, and End Users  
> **Target Version**: ReliaDL v0.3.0+  

---

## 1. Overview

To reduce installation friction for command-line users and automation environments, ReliaDL is distributed across major operating system package managers:

| Package Manager | Platform | Command |
| :--- | :--- | :--- |
| **Homebrew** | macOS (Apple Silicon & Intel) / Linux | `brew install pxa-labs/tap/reliadl` |
| **Windows Package Manager (WinGet)** | Windows 10 & 11 (x64) | `winget install PxA-Labs.ReliaDL` |
| **Scoop** | Windows (x64) | `scoop bucket add pxa-labs https://github.com/PxA-Labs/scoop-bucket`<br>`scoop install reliadl` |
| **Debian / Ubuntu (.deb)** | Linux (`amd64`, `arm64`) | `sudo apt install ./reliadl_0.3.0_amd64.deb` |
| **RHEL / Fedora / CentOS (.rpm)** | Linux (`x86_64`, `aarch64`) | `sudo dnf install ./reliadl-0.3.0-1.x86_64.rpm` |

---

## 2. Homebrew (macOS & Linux)

ReliaDL maintains an official Homebrew tap repository at `PxA-Labs/homebrew-tap`.

### Quick Installation

```bash
# Add custom tap and install ReliaDL
brew tap pxa-labs/tap
brew install reliadl

# Or in a single one-liner
brew install pxa-labs/tap/reliadl
```

### Verification & Upgrade

```bash
reliadl --version
brew upgrade reliadl
```

---

## 3. Windows Package Managers

### 3.1 WinGet (Official Microsoft Community Repository)

WinGet manifests are hosted in the `microsoft/winget-pkgs` catalog under package ID `PxA-Labs.ReliaDL`.

```powershell
# Search for ReliaDL
winget search PxA-Labs.ReliaDL

# Install portable binary
winget install PxA-Labs.ReliaDL

# Update to latest version
winget upgrade PxA-Labs.ReliaDL
```

### 3.2 Scoop (Command-Line Installer for Windows)

```powershell
# Add PxA-Labs Scoop bucket
scoop bucket add pxa-labs https://github.com/PxA-Labs/scoop-bucket

# Install ReliaDL
scoop install reliadl

# Update
scoop update reliadl
```

---

## 4. Linux Native Packages (.deb & .rpm)

Pre-built native package archives are built using `nFPM` during GitHub Actions CI/CD and attached to every [GitHub Release](https://github.com/PxA-Labs/ReliaDL/releases).

### 4.1 Debian / Ubuntu (.deb)

```bash
# Download the .deb package for your architecture
curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/reliadl_0.3.0_amd64.deb

# Install via apt (automatically resolves system dependencies)
sudo apt install ./reliadl_0.3.0_amd64.deb

# Verify installation
reliadl --version
```

### 4.2 Fedora / RHEL / Rocky Linux / openSUSE (.rpm)

```bash
# Download the .rpm package for your architecture
curl -LO https://github.com/PxA-Labs/ReliaDL/releases/latest/download/reliadl-0.3.0-1.x86_64.rpm

# Install via dnf / zypper
sudo dnf install ./reliadl-0.3.0-1.x86_64.rpm

# Verify installation
reliadl --version
```

---

## 5. Automated CI/CD Packaging Pipeline

Packaging is managed by `.github/workflows/packages.yml`:
1. **Validation**: Validates Ruby syntax for Homebrew formulas and YAML/JSON schemas for WinGet, Scoop, and nFPM.
2. **Build**: Compiles standalone binaries for `amd64` and `arm64`, and packages them into `.deb` and `.rpm` containers via `nFPM`.
3. **Publishing**: Automatically calculates cryptographic SHA-256 sums and attaches `.deb` and `.rpm` files directly to GitHub Releases.
