#!/usr/bin/env python3
"""
Release Log Generator & Updater for ReliaDL.
Maintains docs/RELEASE_LOG.md with detailed, user-facing release notes
and artifact SHA-256 checksum tables across version releases.
"""
import sys
import argparse
from pathlib import Path
from datetime import datetime

HEADER_TEMPLATE = """# Release Log — ReliaDL

This document serves as the official, immutable log of all version releases for ReliaDL.
For granular commit-by-commit developer updates, refer to [CHANGELOG.md](CHANGELOG.md).

---
"""

def format_release_entry(version: str, release_date: str, highlights: str, artifacts: list = None) -> str:
    """
    Formats a single release entry according to ReliaDL documentation standards.
    """
    if not version.startswith("v") and not version.startswith("["):
        clean_version = f"[{version.lstrip('v')}]"
    else:
        clean_version = version if version.startswith("[") else f"[{version.lstrip('v')}]"

    entry = f"\n## {clean_version} — {release_date}\n\n"
    
    if highlights:
        entry += f"{highlights.strip()}\n\n"

    if artifacts:
        entry += "### Package Distribution Artifacts & SHA-256 Verification\n\n"
        entry += "| Distribution File | Artifact Type | SHA-256 Checksum |\n"
        entry += "| :--- | :--- | :--- |\n"
        for art in artifacts:
            filename = art.get("filename", "")
            art_type = art.get("type", "Distribution Package")
            sha256 = art.get("sha256", "Pending")
            entry += f"| `{filename}` | {art_type} | `{sha256}` |\n"
        entry += "\n"

    version_str = version.lstrip('v').strip('[]')
    entry += f"### Installation\n\n```bash\npip install reliadl=={version_str}\n```\n\n---"
    return entry

def update_release_log(file_path: str, version: str, release_date: str, highlights: str, artifacts: list = None) -> bool:
    """
    Prepends a new release entry into docs/RELEASE_LOG.md directly below the header.
    """
    path = Path(file_path)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        content = HEADER_TEMPLATE
    else:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()

    # Create formatted entry
    entry = format_release_entry(version, release_date, highlights, artifacts)

    # If the version is already present, do not duplicate
    clean_ver = version.lstrip('v').strip('[]')
    if f"## [{clean_ver}]" in content:
        print(f"Version {clean_ver} already exists in {file_path}.", file=sys.stderr)
        return False

    # Insert after header rule if present
    if "---" in content:
        parts = content.split("---", 1)
        new_content = parts[0] + "---\n" + entry + "\n" + parts[1].lstrip("\n")
    else:
        new_content = HEADER_TEMPLATE + entry + "\n"

    with open(path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Successfully updated release log at {file_path} for version {clean_ver}")
    return True

def main():
    parser = argparse.ArgumentParser(description="Update RELEASE_LOG.md for ReliaDL releases.")
    parser.add_argument("--version", required=True, help="Release version (e.g. 0.3.0 or v0.3.0)")
    parser.add_argument("--date", default=datetime.utcnow().strftime("%Y-%m-%d"), help="Release date YYYY-MM-DD")
    parser.add_argument("--highlights", default="### Release Summary\nOfficial release release update.", help="Markdown text for highlights")
    parser.add_argument("--path", default="docs/RELEASE_LOG.md", help="Path to RELEASE_LOG.md")
    args = parser.parse_args()

    update_release_log(args.path, args.version, args.date, args.highlights)

if __name__ == "__main__":
    main()
