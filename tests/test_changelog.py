import os
import tempfile
import unittest
from pathlib import Path
from scripts.update_changelog import parse_commit_message, update_changelog_content

class TestChangelogUpdater(unittest.TestCase):
    def test_parse_feat_commit(self):
        cat, msg = parse_commit_message("feat(core): support dynamic chunk adjustments")
        self.assertEqual(cat, "Added")
        self.assertEqual(msg, "**core**: Support dynamic chunk adjustments")

    def test_parse_fix_commit(self):
        cat, msg = parse_commit_message("fix(verify): resolve empty hash validation crash")
        self.assertEqual(cat, "Fixed")
        self.assertEqual(msg, "**verify**: Resolve empty hash validation crash")

    def test_parse_chore_commit(self):
        cat, msg = parse_commit_message("chore: upgrade dependencies")
        self.assertEqual(cat, "Changed")
        self.assertEqual(msg, "Upgrade dependencies")

    def test_parse_ignored_commits(self):
        # Merge commits should be ignored
        cat, msg = parse_commit_message("Merge pull request #12 from branch")
        self.assertIsNone(cat)
        self.assertIsNone(msg)

        # Changelog updates should be ignored
        cat, msg = parse_commit_message("docs: update CHANGELOG.md with release notes")
        self.assertIsNone(cat)
        self.assertIsNone(msg)

    def test_update_changelog_content(self):
        # Create a mock CHANGELOG.md in a temporary directory
        temp_dir = tempfile.TemporaryDirectory()
        changelog_path = Path(temp_dir.name) / "CHANGELOG.md"
        
        initial_content = """# Changelog
All notable changes to this project will be documented in this file.

---

## [Unreleased]

### Planned
- Custom headers preset

---

## [1.0.0] - 2026-08-14
- Initial release
"""
        with open(changelog_path, "w", encoding="utf-8") as f:
            f.write(initial_content)

        parsed_commits = [
            ("Added", "**core**: Support dynamic chunk adjustments"),
            ("Fixed", "**verify**: Resolve empty hash validation crash")
        ]

        result = update_changelog_content(str(changelog_path), parsed_commits)
        self.assertTrue(result)

        # Read updated file and verify
        with open(changelog_path, "r", encoding="utf-8") as f:
            updated_content = f.read()

        self.assertIn("### Added\n- **core**: Support dynamic chunk adjustments", updated_content)
        self.assertIn("### Fixed\n- **verify**: Resolve empty hash validation crash", updated_content)
        self.assertIn("### Planned\n- Custom headers preset", updated_content)
        self.assertIn("## [1.0.0]", updated_content)

        # Clean up
        temp_dir.cleanup()

if __name__ == "__main__":
    unittest.main()
