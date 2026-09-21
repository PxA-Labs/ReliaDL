# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for ReliaDL standalone CLI executable.

Produces a single-file binary that bundles the entire CPython runtime,
reliadl package, and all dependencies.  No system Python required.

Usage (from repo root):
    pip install pyinstaller
    pyinstaller reliadl.spec
Output: dist/reliadl  (or dist/reliadl.exe on Windows)
"""

import sys
from pathlib import Path

# Resolve paths relative to the spec file
REPO_ROOT = Path(SPECPATH)  # noqa: F821 — SPECPATH injected by PyInstaller
SRC_DIR = REPO_ROOT / "src"

block_cipher = None

a = Analysis(
    # Entry point
    [str(SRC_DIR / "main.py")],

    pathex=[str(REPO_ROOT)],

    binaries=[],

    # Bundle the default config and py.typed marker that ship with the package
    datas=[
        (str(SRC_DIR / "default_config.yaml"), "src"),
        (str(SRC_DIR / "py.typed"), "src"),
    ],

    hiddenimports=[
        # pydantic v2 uses dynamic imports for validators
        "pydantic",
        "pydantic.deprecated.class_validators",
        "pydantic._internal._config",
        # cryptography sub-backends resolved at runtime
        "cryptography.hazmat.backends.openssl",
        "cryptography.hazmat.backends.openssl.backend",
        # structlog processors
        "structlog._frames",
        "structlog.stdlib",
        # jsonschema validators
        "jsonschema.validators",
        "jsonschema._validators",
        "jsonschema._format",
        # yaml C extension (optional, graceful fallback if absent)
        "yaml",
        "_yaml",
    ],

    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],

    # Exclude heavy optional packages that are never imported by reliadl
    excludes=[
        "tkinter",
        "matplotlib",
        "numpy",
        "pandas",
        "scipy",
        "PIL",
        "IPython",
        "notebook",
        "pytest",
        "setuptools",
        "pip",
    ],

    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="reliadl",
    debug=False,
    bootloader_ignore_signals=False,
    strip=True,           # strip debug symbols → smaller binary
    upx=False,            # UPX disabled — causes AV false-positives on Windows
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,         # CLI tool — always needs a console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,     # determined by the runner (amd64 / arm64)
    codesign_identity=None,
    entitlements_file=None,
    # Windows: embed a version-info resource
    version=None,
    icon=None,
)
