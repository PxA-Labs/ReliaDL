"""
Unit tests for system package distribution manifests.
Validates syntax, schema, and required fields for Homebrew, WinGet, Scoop, and nFPM manifests.
"""

from __future__ import annotations

import json
import subprocess
import unittest
from pathlib import Path

import yaml


class TestPackagingManifests(unittest.TestCase):
    """Test suite validating packaging manifests integrity."""

    def setUp(self) -> None:
        self.repo_root = Path(__file__).resolve().parent.parent.parent

    def test_homebrew_formula_syntax(self) -> None:
        """Validate Ruby syntax of Homebrew formulas."""
        formula_paths = [
            self.repo_root / "Formula" / "reliadl.rb",
            self.repo_root / "packaging" / "homebrew" / "reliadl.rb",
        ]
        for path in formula_paths:
            self.assertTrue(path.exists(), f"Formula missing at {path}")
            # Check Ruby syntax using ruby -c if ruby is installed
            res = subprocess.run(["ruby", "-c", str(path)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Ruby syntax error in {path}:\n{res.stderr}")

    def test_winget_manifests_validity(self) -> None:
        """Validate YAML syntax and required fields in WinGet manifests."""
        winget_dir = self.repo_root / "packaging" / "winget" / "manifests" / "p" / "PxA-Labs" / "ReliaDL" / "0.3.0"
        self.assertTrue(winget_dir.exists(), f"WinGet manifest dir missing: {winget_dir}")

        version_file = winget_dir / "PxA-Labs.ReliaDL.yaml"
        installer_file = winget_dir / "PxA-Labs.ReliaDL.installer.yaml"
        locale_file = winget_dir / "PxA-Labs.ReliaDL.locale.en-US.yaml"

        for f in [version_file, installer_file, locale_file]:
            self.assertTrue(f.exists(), f"WinGet manifest missing: {f}")
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
                self.assertIsInstance(data, dict)
                self.assertEqual(data.get("PackageIdentifier"), "PxA-Labs.ReliaDL")
                self.assertEqual(data.get("PackageVersion"), "0.3.0")

    def test_scoop_manifest_validity(self) -> None:
        """Validate Scoop JSON manifest."""
        scoop_file = self.repo_root / "packaging" / "scoop" / "reliadl.json"
        self.assertTrue(scoop_file.exists(), f"Scoop manifest missing: {scoop_file}")

        with open(scoop_file, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("version"), "0.3.0")
            self.assertIn("architecture", data)
            self.assertIn("64bit", data["architecture"])
            self.assertIn("url", data["architecture"]["64bit"])

    def test_nfpm_yaml_validity(self) -> None:
        """Validate nFPM configuration YAML file."""
        nfpm_file = self.repo_root / "packaging" / "nfpm" / "nfpm.yaml"
        self.assertTrue(nfpm_file.exists(), f"nFPM config missing: {nfpm_file}")

        with open(nfpm_file, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
            self.assertIsInstance(data, dict)
            self.assertEqual(data.get("name"), "reliadl")
            self.assertIn("contents", data)
            self.assertIn("deb", data)
            self.assertIn("rpm", data)


if __name__ == "__main__":
    unittest.main()
