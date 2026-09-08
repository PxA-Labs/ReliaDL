"""
Unit tests for structured custom exception taxonomy in src.exceptions.
Verifies class inheritance, retryability classification, and contextual payloads.
"""

from __future__ import annotations

import builtins
import unittest
from datetime import datetime

from src.exceptions import (
    AllocationError,
    AssemblyError,
    AssemblyFailedError,
    ChunkGuardError,
    ChunkHashMismatchError,
    ClientError,
    ConfigurationError,
    ConnectionError,
    DiskFullError,
    FileHashMismatchError,
    HTTPError,
    IntegrityError,
    ManifestError,
    ManifestFormatError,
    ManifestSignatureMismatchError,
    NetworkError,
    PermissionError,
    PreconditionFailedError,
    ProxyAuthenticationError,
    ProxyConnectionError,
    ProxyError,
    ReliaDLError,
    ServerError,
    StateCorruptedError,
    StateError,
    StateNotFoundError,
    StorageError,
    StoragePermissionError,
    TimeoutError,
)


class TestRootException(unittest.TestCase):
    """Tests for ReliaDLError root base exception."""

    def test_root_properties_and_serialization(self) -> None:
        err = ReliaDLError("Something went wrong", context={"env": "prod"}, is_retryable=False)
        self.assertIsInstance(err, Exception)
        self.assertEqual(err.message, "Something went wrong")
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.context["env"], "prod")
        self.assertIsInstance(err.timestamp, datetime)

        d = err.to_dict()
        self.assertEqual(d["error_type"], "ReliaDLError")
        self.assertEqual(d["message"], "Something went wrong")
        self.assertFalse(d["is_retryable"])
        self.assertEqual(d["context"], {"env": "prod"})
        self.assertIn("timestamp", d)

    def test_chunkguard_alias(self) -> None:
        self.assertIs(ChunkGuardError, ReliaDLError)
        err = ChunkGuardError("Legacy alias message")
        self.assertIsInstance(err, ReliaDLError)

    def test_custom_retryable_override(self) -> None:
        # Default is_retryable for ConfigurationError is False
        err_default = ConfigurationError("Bad config")
        self.assertFalse(err_default.is_retryable)

        # Explicit override
        err_forced = ConfigurationError("Bad config", is_retryable=True)
        self.assertTrue(err_forced.is_retryable)


class TestConfigurationHierarchy(unittest.TestCase):
    """Tests for ConfigurationError taxonomy."""

    def test_configuration_error(self) -> None:
        err = ConfigurationError(
            "Invalid worker count",
            parameter="max_parallel_workers",
            value=64,
        )
        self.assertIsInstance(err, ReliaDLError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.parameter, "max_parallel_workers")
        self.assertEqual(err.value, 64)
        self.assertEqual(err.context["parameter"], "max_parallel_workers")
        self.assertEqual(err.context["value"], 64)


class TestNetworkHierarchy(unittest.TestCase):
    """Tests for NetworkError and network-specific exception taxonomy."""

    def test_network_error(self) -> None:
        err = NetworkError("Failed to reach endpoint", url="https://example.com")
        self.assertIsInstance(err, ReliaDLError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.url, "https://example.com")
        self.assertEqual(err.context["url"], "https://example.com")

    def test_connection_error(self) -> None:
        err = ConnectionError("DNS resolution failed", host="cdn.example.com", port=443)
        self.assertIsInstance(err, NetworkError)
        self.assertIsInstance(err, builtins.ConnectionError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.host, "cdn.example.com")
        self.assertEqual(err.port, 443)
        self.assertEqual(err.context["host"], "cdn.example.com")
        self.assertEqual(err.context["port"], 443)

    def test_timeout_error(self) -> None:
        err = TimeoutError("Chunk read timed out", timeout_seconds=30.0, phase="read")
        self.assertIsInstance(err, NetworkError)
        self.assertIsInstance(err, builtins.TimeoutError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.timeout_seconds, 30.0)
        self.assertEqual(err.phase, "read")
        self.assertEqual(err.context["timeout_seconds"], 30.0)
        self.assertEqual(err.context["phase"], "read")

    def test_http_error_automatic_retryability(self) -> None:
        # 404 Client error -> not retryable
        err_404 = HTTPError("Not found", status_code=404)
        self.assertFalse(err_404.is_retryable)

        # 429 Too Many Requests -> retryable
        err_429 = HTTPError("Rate limited", status_code=429)
        self.assertTrue(err_429.is_retryable)

        # 408 Request Timeout -> retryable
        err_408 = HTTPError("Request timeout", status_code=408)
        self.assertTrue(err_408.is_retryable)

        # 503 Service Unavailable -> retryable
        err_503 = HTTPError("Service unavailable", status_code=503)
        self.assertTrue(err_503.is_retryable)

    def test_client_error(self) -> None:
        err = ClientError("Forbidden", retry_after=60.0, status_code=403)
        self.assertIsInstance(err, HTTPError)
        self.assertEqual(err.retry_after, 60.0)
        self.assertEqual(err.context["retry_after"], 60.0)
        self.assertFalse(err.is_retryable)

    def test_server_error(self) -> None:
        err = ServerError("Internal server error", status_code=500)
        self.assertIsInstance(err, HTTPError)
        self.assertTrue(err.is_retryable)

    def test_precondition_failed_error(self) -> None:
        err = PreconditionFailedError("ETag changed", etag="w/1234567")
        self.assertIsInstance(err, ClientError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.status_code, 412)
        self.assertEqual(err.etag, "w/1234567")
        self.assertEqual(err.context["etag"], "w/1234567")


class TestProxyHierarchy(unittest.TestCase):
    """Tests for ProxyError taxonomy."""

    def test_proxy_connection_error(self) -> None:
        err = ProxyConnectionError("Proxy unreachable", proxy_url="http://proxy.corp:8080")
        self.assertIsInstance(err, ProxyError)
        self.assertIsInstance(err, ReliaDLError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.proxy_url, "http://proxy.corp:8080")

    def test_proxy_authentication_error(self) -> None:
        err = ProxyAuthenticationError("407 Proxy Auth Required", proxy_url="http://proxy.corp:8080")
        self.assertIsInstance(err, ProxyError)
        self.assertFalse(err.is_retryable)


class TestManifestHierarchy(unittest.TestCase):
    """Tests for ManifestError taxonomy."""

    def test_manifest_signature_mismatch(self) -> None:
        err = ManifestSignatureMismatchError(
            "Signature verification failed",
            key_id="key-sec-01",
            algorithm="ed25519",
            manifest_path="/tmp/test.cgmanifest",
        )
        self.assertIsInstance(err, ManifestError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.key_id, "key-sec-01")
        self.assertEqual(err.algorithm, "ed25519")
        self.assertEqual(err.manifest_path, "/tmp/test.cgmanifest")

    def test_manifest_format_error(self) -> None:
        errors = ["Missing field 'chunks'", "Invalid 'version' type"]
        err = ManifestFormatError("Invalid schema", schema_errors=errors)
        self.assertIsInstance(err, ManifestError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.schema_errors, errors)
        self.assertEqual(err.context["schema_errors"], errors)


class TestIntegrityHierarchy(unittest.TestCase):
    """Tests for IntegrityError taxonomy."""

    def test_chunk_hash_mismatch(self) -> None:
        err = ChunkHashMismatchError(
            "Hash verification failed for chunk 5",
            chunk_index=5,
            expected_hash="a1b2c3d4",
            computed_hash="e5f60718",
            start_byte=5242880,
            end_byte=6291455,
        )
        self.assertIsInstance(err, IntegrityError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.chunk_index, 5)
        self.assertEqual(err.expected_hash, "a1b2c3d4")
        self.assertEqual(err.computed_hash, "e5f60718")
        self.assertEqual(err.start_byte, 5242880)
        self.assertEqual(err.end_byte, 6291455)

    def test_file_hash_mismatch(self) -> None:
        err = FileHashMismatchError(
            "Full file integrity check failed",
            file_path="/data/file.iso",
            expected_hash="exp_hash",
            computed_hash="bad_hash",
        )
        self.assertIsInstance(err, IntegrityError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.file_path, "/data/file.iso")


class TestStorageHierarchy(unittest.TestCase):
    """Tests for StorageError taxonomy."""

    def test_disk_full_error(self) -> None:
        err = DiskFullError(
            "No space left on device",
            path="/mnt/downloads",
            available_bytes=1024,
            required_bytes=1048576,
        )
        self.assertIsInstance(err, StorageError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.available_bytes, 1024)
        self.assertEqual(err.required_bytes, 1048576)

    def test_permission_error(self) -> None:
        err = PermissionError("Permission denied", path="/etc/shadow")
        self.assertIsInstance(err, StorageError)
        self.assertIsInstance(err, builtins.PermissionError)
        self.assertFalse(err.is_retryable)
        self.assertIs(StoragePermissionError, PermissionError)

    def test_allocation_error(self) -> None:
        err = AllocationError("posix_fallocate failed", path="/data/large.bin")
        self.assertIsInstance(err, StorageError)
        self.assertFalse(err.is_retryable)


class TestStateHierarchy(unittest.TestCase):
    """Tests for StateError taxonomy."""

    def test_state_not_found(self) -> None:
        err = StateNotFoundError("State file not found", state_file="/tmp/resume.state")
        self.assertIsInstance(err, StateError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.state_file, "/tmp/resume.state")

    def test_state_corrupted(self) -> None:
        err = StateCorruptedError(
            "State corrupted",
            state_file="/tmp/resume.state",
            corrupted_field="chunks[2]",
            details="Invalid JSON syntax",
        )
        self.assertIsInstance(err, StateError)
        self.assertFalse(err.is_retryable)
        self.assertEqual(err.corrupted_field, "chunks[2]")
        self.assertEqual(err.details, "Invalid JSON syntax")


class TestAssemblyHierarchy(unittest.TestCase):
    """Tests for AssemblyError taxonomy."""

    def test_assembly_failed_error(self) -> None:
        err = AssemblyFailedError(
            "Missing chunk files during concatenation",
            reason="Missing chunks on disk",
            missing_chunks=[2, 4, 7],
        )
        self.assertIsInstance(err, AssemblyError)
        self.assertIsInstance(err, ReliaDLError)
        self.assertTrue(err.is_retryable)
        self.assertEqual(err.missing_chunks, [2, 4, 7])
        self.assertEqual(err.reason, "Missing chunks on disk")


if __name__ == "__main__":
    unittest.main()
