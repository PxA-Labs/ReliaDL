#!/usr/bin/env python3
"""
scripts/build_binary.py
-----------------------
Local helper to compile the reliadl standalone binary using PyInstaller.

Usage (from repo root):
    python scripts/build_binary.py [--clean] [--output-name NAME]

The produced binary is written to dist/ and smoke-tested with `reliadl --help`.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).parent.parent
DIST_DIR = REPO_ROOT / "dist"
SPEC_FILE = REPO_ROOT / "reliadl.spec"


def platform_suffix() -> str:
    """Return a filename suffix matching the CI matrix naming convention."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    arch_map = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
    arch = arch_map.get(machine, machine)

    if system == "windows":
        return f"windows-{arch}.exe"
    if system == "darwin":
        return f"darwin-{arch}"
    return f"linux-{arch}"


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build reliadl standalone binary")
    parser.add_argument("--clean", action="store_true", help="Remove build/ and dist/ before building")
    parser.add_argument("--output-name", default=None, help="Override the output binary filename")
    args = parser.parse_args(argv)

    if args.clean:
        for d in (REPO_ROOT / "build", DIST_DIR):
            if d.exists():
                shutil.rmtree(d)
                print(f"Removed {d}")

    # Ensure PyInstaller is available
    result = subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--version"],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        print("PyInstaller not found — installing…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])

    # Run PyInstaller
    print("Building standalone binary…")
    ret = subprocess.call(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm"],
        cwd=REPO_ROOT,
    )
    if ret != 0:
        print("PyInstaller build failed", file=sys.stderr)
        return ret

    # Locate the produced binary
    raw_binary = DIST_DIR / "reliadl"
    if platform.system() == "Windows":
        raw_binary = DIST_DIR / "reliadl.exe"

    if not raw_binary.exists():
        print(f"Expected binary not found: {raw_binary}", file=sys.stderr)
        return 1

    # Rename to platform-specific filename
    suffix = platform_suffix()
    output_name = args.output_name or f"reliadl-{suffix}"
    output_path = DIST_DIR / output_name
    raw_binary.rename(output_path)
    print(f"Binary: {output_path}  ({output_path.stat().st_size / 1_048_576:.1f} MB)")

    # Smoke-test
    print("Smoke-testing --help…")
    test = subprocess.run([str(output_path), "--help"], capture_output=True, text=True)
    if test.returncode != 0:
        print(f"Smoke test FAILED:\n{test.stderr}", file=sys.stderr)
        return 1
    print("Smoke test passed ✓")

    # Generate SHA-256 checksum
    digest = sha256_of(output_path)
    checksum_line = f"{digest}  {output_name}\n"
    checksum_file = DIST_DIR / "SHA256SUMS"
    with checksum_file.open("a") as fh:
        fh.write(checksum_line)
    print(f"SHA-256: {digest}")
    print(f"Appended to {checksum_file}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
