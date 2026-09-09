"""
Unit tests for structured logging and credential redaction in src.logger.
Verifies JSON formatting, log levels, file emission, and sensitive credential scrubbing.
"""

from __future__ import annotations

import io
import json
import logging
import tempfile
import unittest
from pathlib import Path

from src.logger import (
    configure_logger,
    get_logger,
    redact_credentials,
)


class TestCredentialRedaction(unittest.TestCase):
    """Tests for recursive credential scrubbing and header redaction."""

    def test_redact_sensitive_keys_in_dict(self) -> None:
        payload = {
            "download_id": "dl-12345",
            "authorization": "Bearer eyJhbGciOi...",
            "proxy-authorization": "Basic dXNlcjpwYXNz",
            "x-api-key": "secret-key-abcdef",
            "nested": {
                "password": "super-secret-password",
                "api_token": "token-9999",
                "safe_field": "public_data",
            },
        }

        redacted = redact_credentials(payload)

        self.assertEqual(redacted["download_id"], "dl-12345")
        self.assertEqual(redacted["authorization"], "[REDACTED]")
        self.assertEqual(redacted["proxy-authorization"], "[REDACTED]")
        self.assertEqual(redacted["x-api-key"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["password"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["api_token"], "[REDACTED]")
        self.assertEqual(redacted["nested"]["safe_field"], "public_data")

    def test_redact_in_lists_and_tuples(self) -> None:
        data = [
            {"token": "token-1"},
            {"safe": "data"},
            ("tuple_key", {"password": "pass"}),
        ]
        redacted = redact_credentials(data)

        self.assertEqual(redacted[0]["token"], "[REDACTED]")
        self.assertEqual(redacted[1]["safe"], "data")
        self.assertEqual(redacted[2][1]["password"], "[REDACTED]")

    def test_redact_embedded_url_credentials(self) -> None:
        url_with_auth = "Connecting to https://proxy_user:proxy_pass_123@proxy.corp.internal:8080/tunnel"
        redacted = redact_credentials(url_with_auth)
        self.assertNotIn("proxy_user", redacted)
        self.assertNotIn("proxy_pass_123", redacted)
        self.assertIn("https://[REDACTED]:[REDACTED]@proxy.corp.internal:8080/tunnel", redacted)

    def test_redact_bearer_token_in_string(self) -> None:
        msg = "Request failed with Authorization: Bearer secret_token_xyz_890"
        redacted = redact_credentials(msg)
        self.assertNotIn("secret_token_xyz_890", redacted)
        self.assertIn("Bearer [REDACTED]", redacted)


class TestStructuredLogger(unittest.TestCase):
    """Tests for structlog configuration and output rendering."""

    def test_json_logging_and_redaction(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            log_file = f.name

        try:
            configure_logger(level="INFO", format_type="json", log_file=log_file)
            log = get_logger("test_json")

            log.info(
                "chunk_downloaded",
                chunk_index=42,
                bytes_transferred=8388608,
                authorization="Bearer confidential_auth_key",
                proxy_url="http://myuser:mypassword@proxy:3128",
            )

            # Read log file and parse JSON lines
            with open(log_file, "r", encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]

            self.assertGreaterEqual(len(lines), 1)
            last_record = json.loads(lines[-1])

            self.assertEqual(last_record.get("event"), "chunk_downloaded")
            self.assertEqual(last_record.get("chunk_index"), 42)
            self.assertEqual(last_record.get("bytes_transferred"), 8388608)
            self.assertEqual(last_record.get("level"), "info")
            self.assertIn("timestamp", last_record)

            # Assert credentials were fully scrubbed
            self.assertEqual(last_record.get("authorization"), "[REDACTED]")
            self.assertNotIn("confidential_auth_key", json.dumps(last_record))
            self.assertNotIn("mypassword", json.dumps(last_record))
            self.assertIn("[REDACTED]:[REDACTED]@", last_record.get("proxy_url", ""))
        finally:
            Path(log_file).unlink(missing_ok=True)

    def test_text_formatting(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            log_file = f.name

        try:
            configure_logger(level="INFO", format_type="text", log_file=log_file)
            log = get_logger("test_text")
            log.info("cli_status_update", progress_pct=75.5)

            with open(log_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertIn("cli_status_update", content)
            self.assertIn("progress_pct", content)
        finally:
            Path(log_file).unlink(missing_ok=True)

    def test_log_level_filtering(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
            log_file = f.name

        try:
            configure_logger(level="WARNING", format_type="json", log_file=log_file)
            log = get_logger("test_level")

            log.debug("debug_event_should_be_filtered")
            log.info("info_event_should_be_filtered")
            log.warning("warning_event_emitted", reason="slow_mirror")

            with open(log_file, "r", encoding="utf-8") as f:
                content = f.read()

            self.assertNotIn("debug_event_should_be_filtered", content)
            self.assertNotIn("info_event_should_be_filtered", content)
            self.assertIn("warning_event_emitted", content)
        finally:
            Path(log_file).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
