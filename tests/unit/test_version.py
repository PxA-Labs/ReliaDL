"""Tests for the package version exposed by the public API and CLI."""

from __future__ import annotations

import unittest

from reliadl import __version__
from reliadl.cli import __version__ as cli_version


class TestPackageVersion(unittest.TestCase):
    def test_package_and_cli_share_the_release_version(self) -> None:
        self.assertEqual(__version__, "0.4.0")
        self.assertEqual(cli_version, __version__)


if __name__ == "__main__":
    unittest.main()
