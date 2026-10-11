"""Smoke-test an installed ReliaDL distribution outside the source checkout."""

from __future__ import annotations

from importlib.metadata import version

import reliadl
from reliadl.cli import build_parser
from reliadl.download_engine import DownloadEngine
from reliadl.models import DownloadConfig


def main() -> None:
    """Verify the package metadata and primary public entry points."""
    installed_version = version("reliadl")
    if installed_version != reliadl.__version__:
        raise SystemExit(
            f"Version mismatch: distribution={installed_version}, package={reliadl.__version__}"
        )

    if DownloadEngine.__module__ != "reliadl.download_engine":
        raise SystemExit("DownloadEngine is not provided by reliadl.download_engine")

    DownloadConfig()
    parser = build_parser()
    if parser.prog != "reliadl":
        raise SystemExit(f"Unexpected CLI program name: {parser.prog}")

    print(f"ReliaDL {installed_version} installed-wheel smoke test passed")


if __name__ == "__main__":
    main()
