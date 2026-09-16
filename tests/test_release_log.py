import unittest
import tempfile
from pathlib import Path
from scripts.update_release_log import format_release_entry, update_release_log

class TestReleaseLogUpdater(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "RELEASE_LOG.md"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_format_release_entry(self):
        artifacts = [
            {"filename": "reliadl-0.4.0-py3-none-any.whl", "type": "Python Wheel", "sha256": "abc123sha256"}
        ]
        entry = format_release_entry(
            version="0.4.0",
            release_date="2026-10-01",
            highlights="### Release Summary\nNew feature release.",
            artifacts=artifacts
        )
        self.assertIn("## [0.4.0] — 2026-10-01", entry)
        self.assertIn("New feature release.", entry)
        self.assertIn("`reliadl-0.4.0-py3-none-any.whl`", entry)
        self.assertIn("`abc123sha256`", entry)
        self.assertIn("pip install reliadl==0.4.0", entry)

    def test_update_release_log_new_file(self):
        result = update_release_log(
            file_path=str(self.log_path),
            version="v0.4.0",
            release_date="2026-10-01",
            highlights="### Summary\nFirst test release."
        )
        self.assertTrue(result)
        self.assertTrue(self.log_path.exists())
        content = self.log_path.read_text(encoding="utf-8")
        self.assertIn("# Release Log — ReliaDL", content)
        self.assertIn("## [0.4.0] — 2026-10-01", content)

    def test_update_release_log_duplicate_version(self):
        update_release_log(
            file_path=str(self.log_path),
            version="0.4.0",
            release_date="2026-10-01",
            highlights="Initial entry."
        )
        # Attempting duplicate update
        duplicate_result = update_release_log(
            file_path=str(self.log_path),
            version="0.4.0",
            release_date="2026-10-01",
            highlights="Duplicate entry."
        )
        self.assertFalse(duplicate_result)

if __name__ == "__main__":
    unittest.main()
