#!/usr/bin/env python3
import os
import re
import sys
import json
from pathlib import Path

def check_pr_title(title):
    """
    Checks if PR title follows conventional commits and is descriptive.
    """
    if not title:
        return False, "PR Title is empty."
    
    # Conventional commit pattern
    pattern = r"^(feat|fix|docs|style|refactor|perf|test|build|ci|chore|revert)(?:\([^)]+\))?:\s+.+$"
    if not re.match(pattern, title):
        return False, f"PR Title '{title}' does not follow Conventional Commits format (e.g., 'feat: add support for HTTP proxies')."
    
    if len(title.strip()) < 10:
        return False, "PR Title is too short (must be at least 10 characters)."
        
    return True, "PR Title is valid."

def check_pr_description(body):
    """
    Checks if PR description is detailed.
    """
    if not body or len(body.strip()) < 30:
        return False, "PR Description must be at least 30 characters long and detail the changes."
        
    # Check for empty headers/placeholders
    placeholders = ["TODO", "FIXME", "describe your changes", "enter description here"]
    for p in placeholders:
        if p.lower() in body.lower():
            return False, f"PR Description contains unresolved placeholder/instructions: '{p}'"
            
    return True, "PR Description is valid."

def check_novelty(repo_root):
    """
    Scans files for debug placeholders or boilerplate indicators.
    """
    md_files = list(Path(repo_root).rglob("*.py"))
    placeholders = ["# TODO", "# FIXME", "placeholder_value_here", "dummy_value"]
    
    found_placeholders = []
    for file_path in md_files:
        # Skip checking the verification script itself and tests
        if "verify_novelty.py" in str(file_path) or "test_" in str(file_path):
            continue
            
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            for idx, line in enumerate(f, 1):
                for p in placeholders:
                    if p in line:
                        found_placeholders.append(f"{file_path.name}:{idx} - Found placeholder '{p}'")
                        
    if found_placeholders:
        return False, "Code contains temporary debug placeholders:\n" + "\n".join(found_placeholders)
        
    return True, "Code satisfies novelty verification guidelines."

def main():
    event_path = os.environ.get("GITHUB_EVENT_PATH")
    repo_root = Path(os.environ.get("GITHUB_WORKSPACE", "."))
    
    title = ""
    body = ""
    
    if event_path and Path(event_path).exists():
        with open(event_path, "r") as f:
            event_data = json.load(f)
        
        pr_data = event_data.get("pull_request", {})
        title = pr_data.get("title", "")
        body = pr_data.get("body", "")
        print(f"Validating Pull Request #{pr_data.get('number', 'unknown')}...")
    else:
        # Fallback to manual tests/mock inputs when run locally
        title = os.environ.get("PR_TITLE", "feat: add chunk validator")
        body = os.environ.get("PR_BODY", "This adds robust verification logic to chunk download engines. All unit tests have been successfully passed.")
        print("Running in local mock mode...")

    print(f"PR Title: '{title}'")
    print(f"PR Description Length: {len(body) if body else 0} chars")

    # 1. Check PR Title
    title_ok, title_msg = check_pr_title(title)
    print(f"-> Title Check: {'✅' if title_ok else '❌'} {title_msg}")
    
    # 2. Check PR Description
    desc_ok, desc_msg = check_pr_description(body)
    print(f"-> Description Check: {'✅' if desc_ok else '❌'} {desc_msg}")
    
    # 3. Check Novelty in code
    novel_ok, novel_msg = check_novelty(repo_root)
    print(f"-> Novelty Check: {'✅' if novel_ok else '❌'} {novel_msg}")

    # Aggregating results
    if not (title_ok and desc_ok and novel_ok):
        print("\n❌ PR Checks Failed! Please fix the errors listed above before merging.")
        sys.exit(1)
        
    print("\n✅ All PR standard checks (Title, Description, and Novelty) passed successfully!")
    sys.exit(0)

if __name__ == "__main__":
    main()
