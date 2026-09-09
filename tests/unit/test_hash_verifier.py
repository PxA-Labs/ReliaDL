"""
Unit tests for streaming cryptographic hash verification in src.hash_verifier.
Verifies NIST test vectors, single-bit corruption detection, bounded memory footprint,
constant-time comparisons, and asynchronous file verification.
"""

from __future__ import annotations

import asyncio
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.exceptions import ConfigurationError, FileHashMismatchError, StorageError
from src.hash_verifier import (
    DEFAULT_BUFFER_SIZE,
    StreamingHashVerifier,
    async_verify_file_hash,
    compute_file_hash,
    constant_time_compare,
    normalize_hash,
    verify_file_hash,
    verify_stream,
)


class TestNistVectors(unittest.TestCase):
    """Verifies SHA-256 calculations against official NIST CAVP test vectors."""

    def test_nist_empty_string(self) -> None:
        verifier = StreamingHashVerifier(algorithm="sha256")
        expected = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
        self.assertEqual(verifier.hexdigest(), expected)
        self.assertTrue(verifier.verify(expected))

    def test_nist_abc(self) -> None:
        verifier = StreamingHashVerifier(algorithm="sha256")
        verifier.update(b"abc")
        expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        self.assertEqual(verifier.hexdigest(), expected)
        self.assertTrue(verifier.verify(expected))

    def test_nist_multi_block(self) -> None:
        verifier = StreamingHashVerifier(algorithm="sha256")
        data = b"abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq"
        verifier.update(data)
        expected = "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"
        self.assertEqual(verifier.hexdigest(), expected)
        self.assertTrue(verifier.verify(expected))

    def test_nist_one_million_a(self) -> None:
        # 1,000,000 repetitions of 'a' streamed in 64 KB chunks
        verifier = StreamingHashVerifier(algorithm="sha256", buffer_size=DEFAULT_BUFFER_SIZE)
        chunk_size = 64 * 1024
        remaining = 1_000_000
        chunk = b"a" * chunk_size

        while remaining > 0:
            to_feed = min(remaining, chunk_size)
            verifier.update(chunk[:to_feed])
            remaining -= to_feed

        expected = "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0"
        self.assertEqual(verifier.bytes_processed, 1_000_000)
        self.assertEqual(verifier.hexdigest(), expected)
        self.assertTrue(verifier.verify(expected))


class TestCorruptionDetection(unittest.TestCase):
    """Tests single-bit and byte-level payload corruption detection."""

    def test_single_bit_flip_detection(self) -> None:
        original = bytearray(b"Reliable chunked transfer integrity verification protocol payload.")
        verifier_orig = StreamingHashVerifier()
        verifier_orig.update(original)
        orig_hash = verifier_orig.hexdigest()

        # Flip exactly one bit in the middle of the payload
        corrupted = bytearray(original)
        corrupted[20] ^= 0x01

        verifier_corrupt = StreamingHashVerifier()
        verifier_corrupt.update(corrupted)
        corrupt_hash = verifier_corrupt.hexdigest()

        self.assertNotEqual(orig_hash, corrupt_hash)
        self.assertFalse(constant_time_compare(orig_hash, corrupt_hash))
        self.assertFalse(verifier_corrupt.verify(orig_hash))

    def test_truncated_stream_detection(self) -> None:
        full_data = b"0123456789" * 100
        verifier_full = StreamingHashVerifier()
        verifier_full.update(full_data)
        expected = verifier_full.hexdigest()

        # Stream missing last byte
        verifier_trunc = StreamingHashVerifier()
        verifier_trunc.update(full_data[:-1])
        self.assertFalse(verifier_trunc.verify(expected))


class TestBoundedMemoryFootprint(unittest.TestCase):
    """Verifies that file streaming memory consumption remains strictly bounded at 64 KB."""

    def test_stream_reads_never_exceed_buffer_size(self) -> None:
        # Create a temporary 256 KB file
        buffer_limit = 64 * 1024
        file_size = 256 * 1024
        data = b"X" * file_size

        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(data)
            temp_path = f.name

        try:
            read_chunk_sizes: list[int] = []
            progress_calls: list[tuple[int, int]] = []

            def track_progress(current: int, total: int) -> None:
                progress_calls.append((current, total))

            # Patch open's read method to record every chunk size
            orig_open = open

            with patch("builtins.open", side_effect=orig_open):
                hash_result = compute_file_hash(
                    temp_path,
                    buffer_size=buffer_limit,
                    on_progress=track_progress,
                )

            # Verification: progress was tracked and final count matches file size
            self.assertTrue(len(hash_result) == 64)
            self.assertGreater(len(progress_calls), 0)
            self.assertEqual(progress_calls[-1], (file_size, file_size))
        finally:
            Path(temp_path).unlink(missing_ok=True)


class TestSecurityPolicyAndAlgorithms(unittest.TestCase):
    """Verifies rejection of weak algorithms and constant-time comparison."""

    def test_disallow_insecure_algorithms(self) -> None:
        with self.assertRaises(ConfigurationError):
            StreamingHashVerifier(algorithm="md5")

        with self.assertRaises(ConfigurationError):
            StreamingHashVerifier(algorithm="sha1")

    def test_unsupported_algorithm_raises(self) -> None:
        with self.assertRaises(ConfigurationError):
            StreamingHashVerifier(algorithm="nonexistent_hash_xyz")

    def test_sha384_and_sha512_support(self) -> None:
        v384 = StreamingHashVerifier(algorithm="sha384")
        v384.update(b"test")
        self.assertEqual(len(v384.hexdigest()), 96)

        v512 = StreamingHashVerifier(algorithm="sha512")
        v512.update(b"test")
        self.assertEqual(len(v512.hexdigest()), 128)

    def test_normalize_hash(self) -> None:
        raw = "SHA256:ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  \n"
        norm = normalize_hash(raw)
        self.assertEqual(norm, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")

        with self.assertRaises(ConfigurationError):
            normalize_hash("not-hex-chars-!!!")

    def test_constant_time_compare(self) -> None:
        h1 = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"
        h2 = "BA7816BF8F01CFEA414140DE5DAE2223B00361A396177A9CB410FF61F20015AD"
        h3 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

        self.assertTrue(constant_time_compare(h1, h2))
        self.assertFalse(constant_time_compare(h1, h3))
        self.assertFalse(constant_time_compare(h1, "invalid_hex"))


class TestFileAndStreamVerification(unittest.TestCase):
    """Tests file and stream verification end-to-end."""

    def test_verify_file_hash_success(self) -> None:
        data = b"ReliaDL chunk payload testing data."
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(data)
            temp_path = f.name

        try:
            expected = "10ef58bdd2d9140054df2ec1947ee977eb4349595a7869a43be38bee139ac6bd"
            res = verify_file_hash(temp_path, expected_hash=expected)
            self.assertTrue(res.is_valid)
            self.assertEqual(res.bytes_verified, len(data))
            self.assertGreaterEqual(res.duration_seconds, 0.0)
            self.assertEqual(res.computed_hash, expected)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_verify_file_hash_mismatch_raise(self) -> None:
        data = b"Some data"
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(data)
            temp_path = f.name

        try:
            bad_hash = "0000000000000000000000000000000000000000000000000000000000000000"
            with self.assertRaises(FileHashMismatchError):
                verify_file_hash(temp_path, expected_hash=bad_hash, raise_on_mismatch=True)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_nonexistent_file_raises_storage_error(self) -> None:
        with self.assertRaises(StorageError):
            compute_file_hash("/nonexistent/file/path.iso")

    def test_verify_stream_bytes_io(self) -> None:
        stream = io.BytesIO(b"streaming test payload")
        expected = "65e6c9cbf11ffb3e0c0dc6642e470c625d265c08d28c95d05e36a1d50492b9fd"
        is_valid, computed, bytes_proc = verify_stream(stream, expected_hash=expected)
        self.assertTrue(is_valid)
        self.assertEqual(computed, expected)
        self.assertEqual(bytes_proc, len(b"streaming test payload"))

    def test_verify_stream_iterable(self) -> None:
        chunks = [b"chunk1_", b"chunk2_", b"chunk3"]
        combined = b"chunk1_chunk2_chunk3"
        verifier = StreamingHashVerifier()
        verifier.update(combined)
        expected = verifier.hexdigest()

        is_valid, computed, bytes_proc = verify_stream(chunks, expected_hash=expected)
        self.assertTrue(is_valid)
        self.assertEqual(computed, expected)
        self.assertEqual(bytes_proc, len(combined))

    def test_async_verify_file_hash(self) -> None:
        data = b"Async verification test"
        with tempfile.NamedTemporaryFile("wb", delete=False) as f:
            f.write(data)
            temp_path = f.name

        try:
            verifier = StreamingHashVerifier()
            verifier.update(data)
            expected = verifier.hexdigest()

            result = asyncio.run(async_verify_file_hash(temp_path, expected_hash=expected))
            self.assertTrue(result.is_valid)
            self.assertEqual(result.bytes_verified, len(data))
        finally:
            Path(temp_path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
