#!/usr/bin/env python3
"""
Enterprise Conventional Commit Changelog & Release Log Generator.
Parses git commits/PRs according to Conventional Commits specifications,
categorizes entries into Keep a Changelog standard sections, formats PR hyperlinking,
and supports automated version release promotion.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

# Repository URL for PR hyperlinking
REPO_URL = "https://github.com/PxA-Labs/ReliaDL"

# Conventional commit categories mapping
CATEGORY_MAP = {
    "feat": "Features",
    "fix": "Bug Fixes",
    "perf": "Performance Improvements",
    "security": "Security",
    "sec": "Security",
    "docs": "Documentation",
    "refactor": "Maintenance & Dependencies",
    "style": "Maintenance & Dependencies",
    "chore": "Maintenance & Dependencies",
    "ci": "Maintenance & Dependencies",
    "test": "Maintenance & Dependencies",
    "deps": "Maintenance & Dependencies",
}

# Ordered categories for Keep a Changelog representation
CATEGORY_ORDER = [
    "Planned",
    "Features",
    "Bug Fixes",
    "Performance Improvements",
    "Security",
    "Documentation",
    "Maintenance & Dependencies",
    "Added",
    "Changed",
    "Deprecated",
    "Removed",
    "Fixed",
]


def get_git_commits(before_sha: Optional[str], after_sha: Optional[str]) -> list[tuple[str, str]]:
    """
    Retrieves commit messages in the given SHA range.
    Defaults to the last commit if range is not provided.
    """
    if not before_sha or before_sha == "0000000000000000000000000000000000000000":
        commit_range = "-1"
    else:
        commit_range = f"{before_sha}..{after_sha or 'HEAD'}"

    try:
        result = subprocess.run(
            ["git", "log", "--pretty=format:%h|%s", commit_range],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = result.stdout.strip().split("\n")
        commits = []
        for line in lines:
            if not line:
                continue
            parts = line.split("|", 1)
            if len(parts) == 2:
                commits.append((parts[0], parts[1]))
        return commits
    except subprocess.CalledProcessError as e:
        print(f"[WARN] Error running git log: {e}", file=sys.stderr)
        return []


def parse_commit_message(msg: str) -> tuple[Optional[str], Optional[str]]:
    """
    Parses a conventional commit message.
    Returns: (category, formatted_markdown_message) or (None, None) if ignored.
    """
    clean_msg = msg.strip()

    # Ignore merge commits & automated bot commits
    if any(clean_msg.startswith(prefix) for prefix in ("Merge pull request", "Merge branch", "Merge remote-tracking")):
        return None, None
    if "update CHANGELOG.md" in clean_msg.lower() or "update changelog" in clean_msg.lower():
        return None, None

    # Extract PR number if present (e.g., (#97) or (#102))
    pr_match = re.search(r"\(#(\d+)\)$", clean_msg)
    pr_num = pr_match.group(1) if pr_match else None
    if pr_match:
        clean_msg = clean_msg[: pr_match.start()].strip()

    # Match conventional commit syntax: type(scope): description
    pattern = r"^(\w+)(?:\(([^)]+)\))?\s*:\s*(.*)$"
    match = re.match(pattern, clean_msg)

    if match:
        commit_type = match.group(1).lower()
        scope = match.group(2)
        desc = match.group(3).strip()

        if desc:
            desc = desc[0].upper() + desc[1:]

        # Format scope
        if scope:
            formatted_msg = f"**{scope}**: {desc}"
        else:
            formatted_msg = desc

        # Append PR hyperlink
        if pr_num:
            formatted_msg += f" ([#{pr_num}]({REPO_URL}/pull/{pr_num}))"

        category = CATEGORY_MAP.get(commit_type, "Maintenance & Dependencies")
        return category, formatted_msg

    # Non-conventional commit fallback
    if clean_msg:
        clean_msg = clean_msg[0].upper() + clean_msg[1:]
        if pr_num:
            clean_msg += f" ([#{pr_num}]({REPO_URL}/pull/{pr_num}))"
        return "Maintenance & Dependencies", clean_msg

    return None, None


def update_changelog_content(
    changelog_path: str,
    parsed_commits: list[tuple[str, str]],
    release_version: Optional[str] = None,
) -> bool:
    """
    Updates CHANGELOG.md with parsed commits under [Unreleased] or promotes
    [Unreleased] to a release header if release_version is provided.
    """
    file_path = Path(changelog_path)
    if not file_path.exists():
        print(f"[ERROR] Changelog file not found at {changelog_path}", file=sys.stderr)
        return False

    content = file_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    unreleased_idx = -1
    for i, line in enumerate(lines):
        if "## [Unreleased]" in line:
            unreleased_idx = i
            break

    if unreleased_idx == -1:
        print("[ERROR] Could not find '## [Unreleased]' section in changelog.", file=sys.stderr)
        return False

    # Find the boundary of [Unreleased] section
    end_idx = -1
    for i in range(unreleased_idx + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx == -1:
        end_idx = len(lines)

    unreleased_lines = lines[unreleased_idx + 1 : end_idx]

    # Parse existing subsections within [Unreleased]
    subsections: dict[str, list[str]] = {}
    current_section = None
    for line in unreleased_lines:
        striped = line.strip()
        if striped.startswith("### "):
            current_section = striped[4:].strip()
            if current_section not in subsections:
                subsections[current_section] = []
        elif striped.startswith("- ") or striped.startswith("* "):
            if current_section:
                subsections[current_section].append(striped[2:].strip())

    # Add new parsed commits
    changes_made = False
    for cat, msg in parsed_commits:
        if cat not in subsections:
            subsections[cat] = []
        if msg not in subsections[cat]:
            subsections[cat].append(msg)
            changes_made = True

    # If promoting to a new release
    if release_version:
        today_str = datetime.date.today().strftime("%Y-%m-%d")
        release_header = f"## [{release_version}] — {today_str}"

        # Build released entries block
        released_lines = ["", release_header, ""]
        for cat in CATEGORY_ORDER:
            if cat in subsections and subsections[cat]:
                released_lines.append(f"### {cat}")
                for item in subsections[cat]:
                    released_lines.append(f"- {item}")
                released_lines.append("")

        new_lines = lines[:unreleased_idx + 1] + ["", "---"] + released_lines + lines[end_idx:]
        new_content = "\n".join(new_lines) + "\n"
        file_path.write_text(new_content, encoding="utf-8")
        print(f"[SUCCESS] Promoted [Unreleased] to release {release_header} in {changelog_path}")
        return True

    if not changes_made:
        print("[INFO] No new changes to add to changelog.")
        return False

    # Reconstruct [Unreleased] block
    new_unreleased_lines = [""]
    for cat in CATEGORY_ORDER:
        if cat in subsections and subsections[cat]:
            new_unreleased_lines.append(f"### {cat}")
            for item in subsections[cat]:
                new_unreleased_lines.append(f"- {item}")
            new_unreleased_lines.append("")

    new_lines = lines[: unreleased_idx + 1] + new_unreleased_lines + lines[end_idx:]
    new_content = "\n".join(new_lines) + "\n"
    file_path.write_text(new_content, encoding="utf-8")
    print(f"[SUCCESS] Updated {changelog_path}")
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Enterprise Conventional Commit Changelog & Release Generator")
    parser.add_argument("--before", help="Before commit SHA")
    parser.add_argument("--after", help="After commit SHA")
    parser.add_argument("--path", default="docs/CHANGELOG.md", help="Path to CHANGELOG.md")
    parser.add_argument("--release", help="Promote [Unreleased] to specified release version tag (e.g., 0.4.0)")
    args = parser.parse_args()

    print(f"Running changelog updater for commits: {args.before or 'HEAD~1'} -> {args.after or 'HEAD'}")
    commits = get_git_commits(args.before, args.after)

    parsed_commits: list[tuple[str, str]] = []
    for sha, msg in commits:
        cat, clean_msg = parse_commit_message(msg)
        if cat and clean_msg:
            print(f"Found change: [{cat}] {clean_msg} ({sha})")
            parsed_commits.append((cat, clean_msg))

    parsed_commits.reverse()
    update_changelog_content(args.path, parsed_commits, release_version=args.release)


if __name__ == "__main__":
    main()
