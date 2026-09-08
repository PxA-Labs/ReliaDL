"""
Unit tests for configuration loading, parsing, and unit conversion in src.config.
Verifies YAML loading, hierarchical deep merges, environment variable overrides,
byte unit conversions, and DownloadConfig model validation.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import (
    apply_env_overrides,
    deep_merge,
    format_size,
    get_download_config,
    load_config,
    load_yaml_file,
    parse_size,
)
from src.exceptions import ConfigurationError
from src.models import DownloadConfig


class TestSizeParserAndFormatter(unittest.TestCase):
    """Tests for parse_size and format_size utilities."""

    def test_parse_size_bytes(self) -> None:
        self.assertEqual(parse_size("1024"), 1024)
        self.assertEqual(parse_size("1024B"), 1024)
        self.assertEqual(parse_size("1024 bytes"), 1024)
        self.assertEqual(parse_size(1024), 1024)
        self.assertEqual(parse_size(1024.0), 1024)

    def test_parse_size_kb(self) -> None:
        self.assertEqual(parse_size("1KB"), 1024)
        self.assertEqual(parse_size("512 KB"), 512 * 1024)
        self.assertEqual(parse_size("512k"), 512 * 1024)
        self.assertEqual(parse_size("512kib"), 512 * 1024)

    def test_parse_size_mb(self) -> None:
        self.assertEqual(parse_size("8MB"), 8 * 1024 * 1024)
        self.assertEqual(parse_size("16 mb"), 16 * 1024 * 1024)
        self.assertEqual(parse_size("32M"), 32 * 1024 * 1024)
        self.assertEqual(parse_size("64MiB"), 64 * 1024 * 1024)

    def test_parse_size_gb(self) -> None:
        self.assertEqual(parse_size("1GB"), 1024 * 1024 * 1024)
        self.assertEqual(parse_size("1.5 GB"), int(1.5 * 1024 * 1024 * 1024))
        self.assertEqual(parse_size("2G"), 2 * 1024 * 1024 * 1024)

    def test_parse_size_tb(self) -> None:
        self.assertEqual(parse_size("1TB"), 1024 * 1024 * 1024 * 1024)

    def test_parse_size_invalid_inputs(self) -> None:
        invalid_cases = [
            "",
            "   ",
            "invalid",
            "MB",
            "-8MB",
            -10,
            "100PB",  # unsupported unit
            "12.34.56 MB",
        ]
        for val in invalid_cases:
            with self.subTest(val=val):
                with self.assertRaises(ConfigurationError):
                    parse_size(val)  # type: ignore[arg-type]

    def test_format_size(self) -> None:
        self.assertEqual(format_size(500), "500 B")
        self.assertEqual(format_size(1024), "1.00 KB")
        self.assertEqual(format_size(8 * 1024 * 1024), "8.00 MB")
        self.assertEqual(format_size(int(1.5 * 1024 * 1024 * 1024)), "1.50 GB")
        with self.assertRaises(ConfigurationError):
            format_size(-1)


class TestYamlLoadingAndMerging(unittest.TestCase):
    """Tests for YAML reading and dictionary merging."""

    def test_load_valid_yaml(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("download:\n  chunk_size: '16MB'\n  workers: 8\n")
            f_path = f.name

        try:
            data = load_yaml_file(f_path)
            self.assertEqual(data["download"]["chunk_size"], "16MB")
            self.assertEqual(data["download"]["workers"], 8)
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_load_empty_yaml(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("")
            f_path = f.name

        try:
            data = load_yaml_file(f_path)
            self.assertEqual(data, {})
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_load_nonexistent_yaml_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_yaml_file("/nonexistent/path/config.yaml")

    def test_load_invalid_syntax_yaml_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("download: [unbalanced brackets")
            f_path = f.name

        try:
            with self.assertRaises(ConfigurationError):
                load_yaml_file(f_path)
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_load_non_dict_yaml_raises(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
            f.write("- list item 1\n- list item 2\n")
            f_path = f.name

        try:
            with self.assertRaises(ConfigurationError):
                load_yaml_file(f_path)
        finally:
            Path(f_path).unlink(missing_ok=True)

    def test_deep_merge(self) -> None:
        base = {
            "download": {"chunk_size": "8MB", "workers": 4},
            "network": {"timeout": 30},
        }
        override = {
            "download": {"workers": 8, "verify": True},
            "logging": {"level": "DEBUG"},
        }
        merged = deep_merge(base, override)

        self.assertEqual(merged["download"]["chunk_size"], "8MB")
        self.assertEqual(merged["download"]["workers"], 8)
        self.assertTrue(merged["download"]["verify"])
        self.assertEqual(merged["network"]["timeout"], 30)
        self.assertEqual(merged["logging"]["level"], "DEBUG")
        # Ensure base was not mutated
        self.assertEqual(base["download"]["workers"], 4)


class TestEnvironmentOverrides(unittest.TestCase):
    """Tests for environment variable overrides."""

    def test_hierarchical_env_overrides(self) -> None:
        base = {
            "download": {"chunk_size": "8MB", "max_parallel_workers": 4},
            "network": {"max_redirects": 5, "http2": True},
        }
        env = {
            "CHUNKGUARD_DOWNLOAD__CHUNK_SIZE": "16MB",
            "RELIADL_DOWNLOAD__MAX_PARALLEL_WORKERS": "12",
            "RELIADL_NETWORK__HTTP2": "false",
            "RELIADL_NETWORK__MAX_REDIRECTS": "10",
        }
        with patch.dict(os.environ, env, clear=False):
            overridden = apply_env_overrides(base)

        self.assertEqual(overridden["download"]["chunk_size"], "16MB")
        self.assertEqual(overridden["download"]["max_parallel_workers"], 12)
        self.assertFalse(overridden["network"]["http2"])
        self.assertEqual(overridden["network"]["max_redirects"], 10)

    def test_shorthand_env_overrides(self) -> None:
        base = {
            "download": {"chunk_size": "8MB", "max_parallel_workers": 4, "verify_on_complete": True},
            "retry": {"max_attempts": 3},
            "logging": {"level": "INFO"},
        }
        env = {
            "RELIADL_CHUNK_SIZE": "32MB",
            "RELIADL_WORKERS": "16",
            "RELIADL_RETRIES": "7",
            "RELIADL_LOG_LEVEL": "DEBUG",
            "RELIADL_NO_VERIFY": "true",
        }
        with patch.dict(os.environ, env, clear=False):
            overridden = apply_env_overrides(base)

        self.assertEqual(overridden["download"]["chunk_size"], "32MB")
        self.assertEqual(overridden["download"]["max_parallel_workers"], 16)
        self.assertEqual(overridden["retry"]["max_attempts"], 7)
        self.assertEqual(overridden["logging"]["level"], "DEBUG")
        self.assertFalse(overridden["download"]["verify_on_complete"])


class TestGetDownloadConfig(unittest.TestCase):
    """Tests for end-to-end configuration loading into DownloadConfig."""

    def test_default_config_loading(self) -> None:
        config = get_download_config(env_overrides=False)
        self.assertIsInstance(config, DownloadConfig)
        self.assertEqual(config.chunk_size_bytes, 8 * 1024 * 1024)
        self.assertEqual(config.max_parallel_workers, 4)
        self.assertEqual(config.max_retries_per_chunk, 3)
        self.assertTrue(config.verify_on_complete)
        self.assertTrue(config.http2)

    def test_get_download_config_with_explicit_overrides(self) -> None:
        overrides = {
            "download": {"chunk_size": "16MB", "max_parallel_workers": 8},
            "retry": {"max_attempts": 5},
        }
        config = get_download_config(overrides=overrides, env_overrides=False)
        self.assertEqual(config.chunk_size_bytes, 16 * 1024 * 1024)
        self.assertEqual(config.max_parallel_workers, 8)
        self.assertEqual(config.max_retries_per_chunk, 5)

    def test_get_download_config_with_env_override(self) -> None:
        env = {
            "RELIADL_DOWNLOAD__CHUNK_SIZE": "64MB",
            "RELIADL_DOWNLOAD__MAX_PARALLEL_WORKERS": "24",
        }
        with patch.dict(os.environ, env, clear=False):
            config = get_download_config(env_overrides=True)

        self.assertEqual(config.chunk_size_bytes, 64 * 1024 * 1024)
        self.assertEqual(config.max_parallel_workers, 24)


if __name__ == "__main__":
    unittest.main()
