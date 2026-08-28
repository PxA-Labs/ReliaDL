#!/usr/bin/env python3
import re
import sys
import subprocess
from pathlib import Path

def get_git_commits(before_sha, after_sha):
    """
    Retrieves commit messages in the given range.
    If before_sha is empty or all zeros, default to the last commit.
    """
    if not before_sha or before_sha == "0000000000000000000000000000000000000000":
        # Just get the last commit
        commit_range = "-1"
    else:
        commit_range = f"{before_sha}..{after_sha}"

    try:
        # Fetch the logs in format: abbreviated_hash|subject
        result = subprocess.run(
            ["git", "log", "--pretty=format:%h|%s", commit_range],
            capture_output=True,
            text=True,
            check=True
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
        print(f"Error running git log: {e}", file=sys.stderr)
        return []

def parse_commit_message(msg):
    """
    Parses conventional commits.
    Returns: (category, formatted_message) or (None, None) if ignored.
    """
    # Ignore merge commits
    if msg.startswith("Merge pull request") or msg.startswith("Merge branch") or msg.startswith("Merge remote-tracking"):
        return None, None

    # Ignore changelog auto-updates
    if "update CHANGELOG.md" in msg or "update changelog" in msg.lower():
        return None, None

    # Conventional commits regex: type(scope): message
    # e.g., feat(core): add retry logic
    # or fix: bug description
    pattern = r"^(\w+)(?:\(([^)]+)\))?\s*:\s*(.*)$"
    match = re.match(pattern, msg)

    if match:
        commit_type = match.group(1).lower()
        scope = match.group(2)
        desc = match.group(3).strip()

        # Capitalize first letter of description
        if desc:
            desc = desc[0].upper() + desc[1:]

        # Format with scope if present
        if scope:
            formatted_msg = f"**{scope}**: {desc}"
        else:
            formatted_msg = desc

        # Map to Keep a Changelog categories
        if commit_type == "feat":
            return "Added", formatted_msg
        elif commit_type == "fix":
            return "Fixed", formatted_msg
        elif commit_type in ["refactor", "style", "perf", "chore", "docs", "test", "ci"]:
            return "Changed", formatted_msg
        elif commit_type in ["security", "sec"]:
            return "Security", formatted_msg
        else:
            # Fallback for other conventional types
            return "Changed", formatted_msg
    else:
        # Non-conventional commit: capitalize and put under Changed
        clean_msg = msg.strip()
        if clean_msg:
            clean_msg = clean_msg[0].upper() + clean_msg[1:]
        return "Changed", clean_msg

def update_changelog_content(changelog_path, parsed_commits):
    """
    Updates the [Unreleased] section of CHANGELOG.md with parsed commits.
    """
    if not Path(changelog_path).exists():
        print(f"Changelog file not found at {changelog_path}", file=sys.stderr)
        return False

    with open(changelog_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Find the [Unreleased] section boundaries
    lines = content.splitlines()
    unreleased_idx = -1
    for i, line in enumerate(lines):
        if "## [Unreleased]" in line:
            unreleased_idx = i
            break

    if unreleased_idx == -1:
        print("Could not find '## [Unreleased]' section in changelog.", file=sys.stderr)
        return False

    # Find the next separator/header after ## [Unreleased]
    end_idx = -1
    for i in range(unreleased_idx + 1, len(lines)):
        if lines[i].startswith("## ") or lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        end_idx = len(lines)

    # Extract the [Unreleased] section lines
    unreleased_lines = lines[unreleased_idx + 1:end_idx]

    # Parse current subsections within Unreleased
    subsections = {}
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
        elif striped:
            # Handle other lines if necessary, or skip
            pass

    # Merge new parsed commits
    changes_made = False
    for cat, msg in parsed_commits:
        if cat not in subsections:
            subsections[cat] = []
        # Check for duplicates
        if msg not in subsections[cat]:
            subsections[cat].append(msg)
            changes_made = True

    if not changes_made:
        print("No new changes to add to the changelog.")
        return False

    # Reconstruct the Unreleased section
    # Keep the Keep a Changelog standard order
    order = ["Planned", "Added", "Changed", "Deprecated", "Removed", "Fixed", "Security"]
    # Add any extra categories that might exist
    for cat in subsections:
        if cat not in order:
            order.append(cat)

    new_unreleased_lines = [""] # start with empty line after ## [Unreleased]
    for cat in order:
        if cat in subsections and subsections[cat]:
            new_unreleased_lines.append(f"### {cat}")
            for item in subsections[cat]:
                new_unreleased_lines.append(f"- {item}")
            new_unreleased_lines.append("") # blank line after section

    # If the list only contains [""] or is empty, clear it
    if len(new_unreleased_lines) == 1 and new_unreleased_lines[0] == "":
        new_unreleased_lines.pop()

    # Reconstruct the whole file
    new_lines = lines[:unreleased_idx + 1] + new_unreleased_lines + lines[end_idx:]
    new_content = "\n".join(new_lines) + "\n"

    with open(changelog_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    print(f"Successfully updated {changelog_path}")
    return True

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Update CHANGELOG.md based on git commits.")
    parser.add_argument("--before", help="Before commit SHA")
    parser.add_argument("--after", help="After commit SHA")
    parser.add_argument("--path", default="docs/CHANGELOG.md", help="Path to CHANGELOG.md")
    args = parser.parse_args()

    print(f"Running changelog updater for commits: {args.before} -> {args.after}")
    commits = get_git_commits(args.before, args.after)
    if not commits:
        print("No commits found in range.")
        return

    parsed_commits = []
    for sha, msg in commits:
        cat, clean_msg = parse_commit_message(msg)
        if cat and clean_msg:
            print(f"Found change: [{cat}] {clean_msg} ({sha})")
            parsed_commits.append((cat, clean_msg))

    # Commits are returned newest first by git log.
    # We should reverse them so they are appended in chronological order.
    parsed_commits.reverse()

    if parsed_commits:
        update_changelog_content(args.path, parsed_commits)
    else:
        print("No user-facing changes to update.")

if __name__ == "__main__":
    main()
