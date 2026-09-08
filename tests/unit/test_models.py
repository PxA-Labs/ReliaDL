"""
Unit tests for core domain models and configuration in src.models.
Verifies immutability, data validation, serialization, and lifecycle states.
"""

from __future__ import annotations

import json
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

from src.models import (
    ChunkResult,
    ChunkSpec,
    ChunkState,
    ChunkStatus,
    DownloadConfig,
    DownloadResult,
    DownloadState,
    DownloadStatistics,
    DownloadStatus,
    ProgressReport,
    VerificationResult,
)

try:
    from pydantic import ValidationError
    ValidationException = (ValueError, ValidationError)
except ImportError:
    ValidationException = ValueError  # type: ignore[assignment,misc]


class TestChunkStatus(unittest.TestCase):
    """Tests for ChunkStatus enumeration."""

    def test_status_values(self) -> None:
        self.assertEqual(ChunkStatus.PENDING, "PENDING")
        self.assertEqual(ChunkStatus.DOWNLOADING, "DOWNLOADING")
        self.assertEqual(ChunkStatus.IN_PROGRESS, "IN_PROGRESS")
        self.assertEqual(ChunkStatus.VERIFYING, "VERIFYING")
        self.assertEqual(ChunkStatus.COMPLETE, "COMPLETE")
        self.assertEqual(ChunkStatus.COMPLETED, "COMPLETED")
        self.assertEqual(ChunkStatus.FAILED, "FAILED")
        self.assertEqual(ChunkStatus.ABANDONED, "ABANDONED")

    def test_terminal_and_success_predicates(self) -> None:
        self.assertTrue(ChunkStatus.COMPLETE.is_terminal)
        self.assertTrue(ChunkStatus.COMPLETED.is_terminal)
        self.assertTrue(ChunkStatus.ABANDONED.is_terminal)
        self.assertFalse(ChunkStatus.PENDING.is_terminal)
        self.assertFalse(ChunkStatus.IN_PROGRESS.is_terminal)
        self.assertFalse(ChunkStatus.FAILED.is_terminal)

        self.assertTrue(ChunkStatus.COMPLETE.is_successful)
        self.assertTrue(ChunkStatus.COMPLETED.is_successful)
        self.assertFalse(ChunkStatus.ABANDONED.is_successful)
        self.assertFalse(ChunkStatus.PENDING.is_successful)


class TestDownloadStatus(unittest.TestCase):
    """Tests for DownloadStatus enumeration."""

    def test_status_values(self) -> None:
        self.assertEqual(DownloadStatus.PENDING, "PENDING")
        self.assertEqual(DownloadStatus.IN_PROGRESS, "IN_PROGRESS")
        self.assertEqual(DownloadStatus.DOWNLOADING, "DOWNLOADING")
        self.assertEqual(DownloadStatus.ASSEMBLING, "ASSEMBLING")
        self.assertEqual(DownloadStatus.VERIFYING, "VERIFYING")
        self.assertEqual(DownloadStatus.COMPLETE, "COMPLETE")
        self.assertEqual(DownloadStatus.COMPLETED, "COMPLETED")
        self.assertEqual(DownloadStatus.FAILED, "FAILED")
        self.assertEqual(DownloadStatus.CANCELLED, "CANCELLED")

    def test_terminal_and_success_predicates(self) -> None:
        self.assertTrue(DownloadStatus.COMPLETE.is_terminal)
        self.assertTrue(DownloadStatus.COMPLETED.is_terminal)
        self.assertTrue(DownloadStatus.FAILED.is_terminal)
        self.assertTrue(DownloadStatus.CANCELLED.is_terminal)
        self.assertFalse(DownloadStatus.PENDING.is_terminal)
        self.assertFalse(DownloadStatus.IN_PROGRESS.is_terminal)
        self.assertFalse(DownloadStatus.ASSEMBLING.is_terminal)

        self.assertTrue(DownloadStatus.COMPLETE.is_successful)
        self.assertTrue(DownloadStatus.COMPLETED.is_successful)
        self.assertFalse(DownloadStatus.FAILED.is_successful)


class TestChunkSpec(unittest.TestCase):
    """Tests for immutable ChunkSpec dataclass."""

    def test_valid_instantiation_and_auto_size(self) -> None:
        spec = ChunkSpec(index=0, start_byte=0, end_byte=1023)
        self.assertEqual(spec.index, 0)
        self.assertEqual(spec.start_byte, 0)
        self.assertEqual(spec.end_byte, 1023)
        self.assertEqual(spec.size, 1024)
        self.assertIsNone(spec.expected_hash)
        self.assertEqual(spec.range_header_value, "bytes=0-1023")

    def test_valid_with_explicit_matching_size(self) -> None:
        spec = ChunkSpec(
            index=1,
            start_byte=1024,
            end_byte=2047,
            size=1024,
            expected_hash="sha256:A1B2C3D4",
        )
        self.assertEqual(spec.size, 1024)
        self.assertEqual(spec.expected_hash, "a1b2c3d4")
        self.assertEqual(spec.range_header_value, "bytes=1024-2047")

    def test_immutability(self) -> None:
        spec = ChunkSpec(index=0, start_byte=0, end_byte=1023)
        with self.assertRaises(FrozenInstanceError):
            spec.index = 1  # type: ignore[misc]
        with self.assertRaises(FrozenInstanceError):
            spec.size = 2048  # type: ignore[misc]

    def test_negative_index_raises(self) -> None:
        with self.assertRaises(ValueError):
            ChunkSpec(index=-1, start_byte=0, end_byte=1023)

    def test_negative_start_byte_raises(self) -> None:
        with self.assertRaises(ValueError):
            ChunkSpec(index=0, start_byte=-1, end_byte=1023)

    def test_end_byte_less_than_start_byte_raises(self) -> None:
        with self.assertRaises(ValueError):
            ChunkSpec(index=0, start_byte=500, end_byte=400)

    def test_mismatched_explicit_size_raises(self) -> None:
        with self.assertRaises(ValueError):
            ChunkSpec(index=0, start_byte=0, end_byte=1023, size=2048)

    def test_to_dict_serialization(self) -> None:
        spec = ChunkSpec(index=0, start_byte=0, end_byte=1023, expected_hash="abc")
        serialized = spec.to_dict()
        self.assertEqual(serialized["index"], 0)
        self.assertEqual(serialized["start_byte"], 0)
        self.assertEqual(serialized["end_byte"], 1023)
        self.assertEqual(serialized["size"], 1024)
        self.assertEqual(serialized["expected_hash"], "abc")


class TestChunkResult(unittest.TestCase):
    """Tests for ChunkResult dataclass."""

    def test_speed_calculation(self) -> None:
        result = ChunkResult(
            index=0,
            success=True,
            bytes_downloaded=10_000_000,
            duration_sec=2.0,
            computed_hash="hash123",
        )
        self.assertEqual(result.speed_bps, 5_000_000.0)
        self.assertEqual(result.to_dict()["bytes_downloaded"], 10_000_000)

    def test_zero_duration_speed(self) -> None:
        result = ChunkResult(
            index=0,
            success=False,
            bytes_downloaded=0,
            duration_sec=0.0,
            error="Connection timed out",
        )
        self.assertEqual(result.speed_bps, 0.0)
        self.assertEqual(result.error, "Connection timed out")


class TestChunkState(unittest.TestCase):
    """Tests for ChunkState dataclass and lifecycle transitions."""

    def test_lifecycle_transitions(self) -> None:
        state = ChunkState(index=0, start_byte=0, end_byte=8_388_607)
        self.assertEqual(state.status, ChunkStatus.PENDING)
        self.assertEqual(state.size, 8_388_608)
        self.assertEqual(state.retries, 0)
        self.assertFalse(state.hash_verified)

        state.mark_in_progress()
        self.assertEqual(state.status, ChunkStatus.IN_PROGRESS)

        state.mark_verifying()
        self.assertEqual(state.status, ChunkStatus.VERIFYING)

        state.mark_failed(error="Checksum mismatch", increment_retry=True)
        self.assertEqual(state.status, ChunkStatus.FAILED)
        self.assertEqual(state.retries, 1)
        self.assertEqual(state.error, "Checksum mismatch")

        state.mark_complete(computed_hash="a1b2c3d4e5f6")
        self.assertEqual(state.status, ChunkStatus.COMPLETE)
        self.assertTrue(state.hash_verified)
        self.assertEqual(state.computed_hash, "a1b2c3d4e5f6")
        self.assertIsNone(state.error)

        state.mark_abandoned(error="Retries exhausted")
        self.assertEqual(state.status, ChunkStatus.ABANDONED)
        self.assertEqual(state.error, "Retries exhausted")

    def test_spec_property_generation(self) -> None:
        state = ChunkState(
            index=2,
            start_byte=200,
            end_byte=299,
            expected_hash="hex123",
        )
        spec = state.spec
        self.assertEqual(spec.index, 2)
        self.assertEqual(spec.start_byte, 200)
        self.assertEqual(spec.end_byte, 299)
        self.assertEqual(spec.size, 100)
        self.assertEqual(spec.expected_hash, "hex123")

    def test_serialization_roundtrip(self) -> None:
        state = ChunkState(
            index=3,
            start_byte=3000,
            end_byte=3999,
            status=ChunkStatus.FAILED,
            retries=2,
            error="HTTP 503",
        )
        d = state.to_dict()
        self.assertEqual(d["status"], "FAILED")
        self.assertEqual(d["retries"], 2)

        restored = ChunkState.from_dict(d)
        self.assertEqual(restored.index, 3)
        self.assertEqual(restored.status, ChunkStatus.FAILED)
        self.assertEqual(restored.retries, 2)
        self.assertEqual(restored.error, "HTTP 503")


class TestDownloadStatistics(unittest.TestCase):
    """Tests for DownloadStatistics metrics aggregation."""

    def test_progress_calculation(self) -> None:
        stats = DownloadStatistics(
            bytes_completed=500,
            bytes_total=1000,
            speed_bps=250.0,
            eta_seconds=2.0,
            active_workers=4,
        )
        self.assertEqual(stats.progress_percentage, 50.0)
        self.assertFalse(stats.is_complete)
        self.assertEqual(stats.bytes_remaining, 500)

        stats.bytes_completed = 1000
        self.assertEqual(stats.progress_percentage, 100.0)
        self.assertTrue(stats.is_complete)
        self.assertEqual(stats.bytes_remaining, 0)

    def test_zero_total_bytes_protection(self) -> None:
        stats = DownloadStatistics(bytes_completed=0, bytes_total=0)
        self.assertEqual(stats.progress_percentage, 0.0)
        self.assertFalse(stats.is_complete)
        self.assertEqual(stats.bytes_remaining, 0)

    def test_serialization_roundtrip(self) -> None:
        stats = DownloadStatistics(
            bytes_completed=400,
            bytes_total=800,
            speed_bps=1024.0,
            eta_seconds=0.5,
            active_workers=2,
        )
        d = stats.to_dict()
        restored = DownloadStatistics.from_dict(d)
        self.assertEqual(restored.bytes_completed, 400)
        self.assertEqual(restored.bytes_total, 800)
        self.assertEqual(restored.speed_bps, 1024.0)


class TestDownloadState(unittest.TestCase):
    """Tests for persistent DownloadState and JSON serialization."""

    def test_initialization_and_chunk_filtering(self) -> None:
        chunk0 = ChunkState(index=0, start_byte=0, end_byte=99, status=ChunkStatus.COMPLETE)
        chunk1 = ChunkState(index=1, start_byte=100, end_byte=199, status=ChunkStatus.PENDING)
        chunk2 = ChunkState(index=2, start_byte=200, end_byte=299, status=ChunkStatus.FAILED)
        chunk3 = ChunkState(index=3, start_byte=300, end_byte=399, status=ChunkStatus.ABANDONED)

        state = DownloadState(
            download_id="dl-987",
            url="https://example.com/asset.bin",
            target_path="./downloads/asset.bin",
            file_size=400,
            chunks=[chunk0, chunk1, chunk2, chunk3],
        )

        self.assertEqual(state.output_path, "./downloads/asset.bin")
        self.assertEqual(state.total_chunks, 4)
        self.assertEqual(len(state.completed_chunks), 1)
        self.assertEqual(state.completed_chunks[0].index, 0)
        self.assertEqual(len(state.pending_chunks), 1)
        self.assertEqual(state.pending_chunks[0].index, 1)
        self.assertEqual(len(state.failed_chunks), 1)
        self.assertEqual(state.failed_chunks[0].index, 2)
        self.assertEqual(len(state.abandoned_chunks), 1)
        self.assertEqual(state.abandoned_chunks[0].index, 3)
        self.assertFalse(state.is_finished)

    def test_json_roundtrip_serialization(self) -> None:
        now = datetime.now(timezone.utc)
        chunk = ChunkState(
            index=0,
            start_byte=0,
            end_byte=1023,
            status=ChunkStatus.COMPLETE,
            hash_verified=True,
            computed_hash="abcde12345",
        )
        stats = DownloadStatistics(
            bytes_completed=1024,
            bytes_total=1024,
            speed_bps=512.0,
            active_workers=1,
        )
        state = DownloadState(
            download_id="dl-test-json",
            url="https://example.com/file.iso",
            target_path="/tmp/file.iso",
            file_size=1024,
            created_at=now,
            updated_at=now,
            status=DownloadStatus.COMPLETE,
            chunks=[chunk],
            statistics=stats,
        )

        json_output = state.to_json()
        self.assertIsInstance(json_output, str)

        restored = DownloadState.from_json(json_output)
        self.assertEqual(restored.download_id, "dl-test-json")
        self.assertEqual(restored.status, DownloadStatus.COMPLETE)
        self.assertTrue(restored.is_finished)
        self.assertEqual(len(restored.chunks), 1)
        self.assertEqual(restored.chunks[0].computed_hash, "abcde12345")
        self.assertIsNotNone(restored.statistics)
        self.assertEqual(restored.statistics.bytes_completed, 1024)  # type: ignore[union-attr]

    def test_update_timestamp(self) -> None:
        state = DownloadState(
            download_id="dl-ts",
            url="https://example.com/file",
            target_path="file",
            file_size=100,
        )
        old_time = state.updated_at
        state.update_timestamp()
        self.assertGreaterEqual(state.updated_at, old_time)


class TestResultAndReportModels(unittest.TestCase):
    """Tests for DownloadResult, VerificationResult, and ProgressReport."""

    def test_download_result(self) -> None:
        res = DownloadResult(
            output_path=Path("/data/out.bin"),
            file_hash="hash_value_123",
            is_verified=True,
            file_size=50_000_000,
            total_chunks=10,
            chunks_retried=1,
            total_bytes_downloaded=55_000_000,
            elapsed_seconds=11.0,
            average_speed_bps=5_000_000.0,
            download_id="dl-res-01",
        )
        self.assertTrue(res.is_verified)
        self.assertEqual(res.to_dict()["output_path"], "/data/out.bin")
        with self.assertRaises(FrozenInstanceError):
            res.is_verified = False  # type: ignore[misc]

    def test_verification_result(self) -> None:
        v = VerificationResult(
            file_path=Path("/data/target.iso"),
            expected_hash="expected_sha256",
            computed_hash="expected_sha256",
            is_valid=True,
            bytes_verified=1024,
            duration_seconds=0.05,
            algorithm="sha256",
        )
        self.assertTrue(v.is_valid)
        self.assertEqual(v.to_dict()["file_path"], "/data/target.iso")
        with self.assertRaises(FrozenInstanceError):
            v.is_valid = False  # type: ignore[misc]

    def test_progress_report(self) -> None:
        now = datetime.now(timezone.utc)
        p = ProgressReport(
            download_id="dl-report-01",
            timestamp=now,
            total_bytes=10_000_000,
            downloaded_bytes=5_000_000,
            percentage=50.0,
            total_chunks=10,
            chunks_complete=5,
            chunks_in_progress=2,
            chunks_failed=0,
            chunks_pending=3,
            current_speed_bps=1_000_000.0,
            average_speed_bps=950_000.0,
            elapsed_seconds=5.2,
            estimated_remaining_seconds=5.0,
            active_workers=4,
        )
        self.assertEqual(p.percentage, 50.0)
        self.assertEqual(p.to_dict()["download_id"], "dl-report-01")
        with self.assertRaises(FrozenInstanceError):
            p.percentage = 60.0  # type: ignore[misc]


class TestDownloadConfig(unittest.TestCase):
    """Tests for DownloadConfig validation."""

    def test_default_config_validity(self) -> None:
        config = DownloadConfig()
        self.assertEqual(config.chunk_size_bytes, 8 * 1024 * 1024)
        self.assertEqual(config.max_parallel_workers, 4)
        self.assertEqual(config.max_retries_per_chunk, 3)
        self.assertEqual(config.hash_algorithm, "sha256")
        self.assertTrue(config.verify_on_complete)
        self.assertTrue(config.http2)
        self.assertTrue(config.verify_ssl)
        config.validate()

    def test_custom_valid_config(self) -> None:
        config = DownloadConfig(
            chunk_size_bytes=16 * 1024 * 1024,
            max_parallel_workers=8,
            max_retries_per_chunk=5,
            retry_base_delay_seconds=0.5,
            retry_max_delay_seconds=30.0,
            user_agent="ReliaDL-Custom/2.0",
        )
        self.assertEqual(config.chunk_size_bytes, 16 * 1024 * 1024)
        self.assertEqual(config.max_parallel_workers, 8)
        self.assertEqual(config.user_agent, "ReliaDL-Custom/2.0")
        config.validate()

    def test_chunk_size_bounds_validation(self) -> None:
        # Below minimum (< 1 MB)
        with self.assertRaises(ValidationException):
            DownloadConfig(chunk_size_bytes=512 * 1024)

        # Above maximum (> 256 MB)
        with self.assertRaises(ValidationException):
            DownloadConfig(chunk_size_bytes=512 * 1024 * 1024)

    def test_worker_bounds_validation(self) -> None:
        # Less than 1 worker
        with self.assertRaises(ValidationException):
            DownloadConfig(max_parallel_workers=0)

        # More than 32 workers
        with self.assertRaises(ValidationException):
            DownloadConfig(max_parallel_workers=33)

    def test_hash_algorithm_security_policy(self) -> None:
        # Disallow insecure algorithms
        with self.assertRaises(ValidationException):
            DownloadConfig(hash_algorithm="md5")

        with self.assertRaises(ValidationException):
            DownloadConfig(hash_algorithm="sha1")

        # Allow secure algorithms
        cfg256 = DownloadConfig(hash_algorithm="sha256")
        self.assertEqual(cfg256.hash_algorithm, "sha256")

        cfg512 = DownloadConfig(hash_algorithm="sha512")
        self.assertEqual(cfg512.hash_algorithm, "sha512")

    def test_retry_range_validation(self) -> None:
        # retry_max_delay_seconds < retry_base_delay_seconds
        with self.assertRaises(ValidationException):
            DownloadConfig(
                retry_base_delay_seconds=10.0,
                retry_max_delay_seconds=2.0,
            )

    def test_dict_serialization_and_deserialization(self) -> None:
        config = DownloadConfig(chunk_size_bytes=32 * 1024 * 1024, max_parallel_workers=16)
        data = config.to_dict()
        self.assertEqual(data["chunk_size_bytes"], 32 * 1024 * 1024)
        self.assertEqual(data["max_parallel_workers"], 16)

        restored = DownloadConfig.from_dict(data)
        self.assertEqual(restored.chunk_size_bytes, 32 * 1024 * 1024)
        self.assertEqual(restored.max_parallel_workers, 16)


if __name__ == "__main__":
    unittest.main()
