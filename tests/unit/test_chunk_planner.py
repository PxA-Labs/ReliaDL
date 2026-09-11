"""
Unit tests for the dynamic chunk boundary planner in src.algorithms.adaptive_chunker.
Verifies gap-free and overlap-free coverage, exact terminal byte alignment,
variable sizing under changing network state, resume serialization, and
concurrent issuance.
"""

from __future__ import annotations

import threading
import unittest

from src.algorithms.adaptive_chunker import (
    DEFAULT_CHUNK_SIZE,
    BLDCSController,
    DynamicChunkPlanner,
)
from src.algorithms.metrics_collector import NetworkStateSnapshot
from src.exceptions import ConfigurationError
from src.models import ChunkSpec

MB = 1024 * 1024


def snapshot(
    throughput_mbps: float = 12.5, rtt: float = 0.05, drop: float = 0.0
) -> NetworkStateSnapshot:
    """Build a primed network state for a given path condition."""
    throughput = throughput_mbps * MB
    return NetworkStateSnapshot(
        rtt_seconds=rtt,
        rtt_deviation_seconds=0.0,
        throughput_bps=throughput,
        drop_probability=drop,
        bdp_bytes=throughput * rtt,
        mean_transfer_bytes=8 * MB,
        samples=100,
        failures=0,
    )


def assert_partition(test: unittest.TestCase, specs: list[ChunkSpec], size: int) -> None:
    """Assert a chunk list is a gap-free, overlap-free cover of [0, size)."""
    if size == 0:
        test.assertEqual(specs, [])
        return
    test.assertEqual(specs[0].start_byte, 0)
    test.assertEqual(specs[-1].end_byte, size - 1)
    test.assertEqual(sum(s.size for s in specs), size)
    for previous, current in zip(specs, specs[1:]):
        test.assertEqual(current.start_byte, previous.end_byte + 1)
    test.assertEqual([s.index for s in specs], list(range(len(specs))))
    for spec in specs:
        test.assertGreater(spec.size, 0)


class TestCoverage(unittest.TestCase):
    """Every byte must be covered exactly once."""

    def test_exact_multiple_of_chunk_size(self) -> None:
        size = 64 * MB
        planner = DynamicChunkPlanner(file_size=size)
        specs = planner.plan_all()
        assert_partition(self, specs, size)
        self.assertEqual(len(specs), 8)

    def test_ragged_tail(self) -> None:
        size = 104 * MB + 12345  # 104 MB is an exact multiple of the 8 MB default
        planner = DynamicChunkPlanner(file_size=size)
        specs = planner.plan_all()
        assert_partition(self, specs, size)
        self.assertEqual(specs[-1].size, 12345)

    def test_file_smaller_than_one_chunk(self) -> None:
        planner = DynamicChunkPlanner(file_size=1024)
        specs = planner.plan_all()
        assert_partition(self, specs, 1024)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].range_header_value, "bytes=0-1023")

    def test_single_byte_file(self) -> None:
        specs = DynamicChunkPlanner(file_size=1).plan_all()
        assert_partition(self, specs, 1)
        self.assertEqual(specs[0].range_header_value, "bytes=0-0")

    def test_empty_file_yields_no_chunks(self) -> None:
        planner = DynamicChunkPlanner(file_size=0)
        self.assertEqual(planner.plan_all(), [])
        self.assertIsNone(planner.next_chunk())
        self.assertTrue(planner.is_exhausted)

    def test_coverage_across_many_sizes(self) -> None:
        for size in (
            1, 2, 1023, MB - 1, MB, MB + 1, 7 * MB + 3,
            8 * MB, 8 * MB + 1, 63 * MB, 512 * MB + 999,
        ):
            with self.subTest(size=size):
                assert_partition(
                    self, DynamicChunkPlanner(file_size=size).plan_all(), size
                )

    def test_no_overlap_under_varying_sizes(self) -> None:
        """Boundaries must stay contiguous even as the controller changes size."""
        size = 400 * MB
        planner = DynamicChunkPlanner(file_size=size)
        specs: list[ChunkSpec] = []
        drops = [0.0, 0.5, 0.01, 0.9, 0.0, 0.3]
        i = 0
        while not planner.is_exhausted:
            spec = planner.next_chunk(snapshot(drop=drops[i % len(drops)]))
            assert spec is not None
            specs.append(spec)
            i += 1
        assert_partition(self, specs, size)


class TestTerminalByte(unittest.TestCase):
    """The final chunk must end at exactly file_size - 1."""

    def test_last_chunk_ends_at_final_byte(self) -> None:
        for size in (1, 4095, 8 * MB, 8 * MB + 1, 123_456_789):
            with self.subTest(size=size):
                specs = DynamicChunkPlanner(file_size=size).plan_all()
                self.assertEqual(specs[-1].end_byte, size - 1)

    def test_no_chunk_extends_past_end(self) -> None:
        size = 9 * MB
        for spec in DynamicChunkPlanner(file_size=size).plan_all():
            self.assertLess(spec.end_byte, size)

    def test_tail_truncated_not_padded(self) -> None:
        planner = DynamicChunkPlanner(file_size=8 * MB + 7)
        specs = planner.plan_all()
        self.assertEqual(specs[-1].size, 7)


class TestVariableSizing(unittest.TestCase):
    """Chunk sizes must track the controller's response to network state."""

    def test_clean_link_yields_larger_chunks_than_lossy(self) -> None:
        clean = DynamicChunkPlanner(file_size=512 * MB).next_chunk(snapshot(drop=0.0))
        lossy = DynamicChunkPlanner(file_size=512 * MB).next_chunk(snapshot(drop=0.8))
        assert clean is not None and lossy is not None
        self.assertGreater(clean.size, lossy.size)

    def test_degradation_mid_transfer_narrows_chunks(self) -> None:
        planner = DynamicChunkPlanner(file_size=512 * MB)
        first = planner.next_chunk(snapshot(drop=0.0))
        second = planner.next_chunk(snapshot(drop=0.9))
        assert first is not None and second is not None
        self.assertLess(second.size, first.size)
        self.assertEqual(second.start_byte, first.end_byte + 1)

    def test_omitted_state_uses_controller_default(self) -> None:
        spec = DynamicChunkPlanner(file_size=512 * MB).next_chunk()
        assert spec is not None
        self.assertEqual(spec.size, DEFAULT_CHUNK_SIZE)

    def test_custom_controller_bounds_honoured(self) -> None:
        controller = BLDCSController(
            min_chunk_size=2 * MB, max_chunk_size=4 * MB, default_chunk_size=2 * MB
        )
        planner = DynamicChunkPlanner(file_size=100 * MB, controller=controller)
        for spec in planner.plan_all(snapshot(drop=0.0))[:-1]:
            self.assertLessEqual(spec.size, 4 * MB)
            self.assertGreaterEqual(spec.size, 2 * MB)


class TestPlannerState(unittest.TestCase):
    """Cursor tracking, exhaustion, and reset semantics."""

    def test_cursor_advances(self) -> None:
        planner = DynamicChunkPlanner(file_size=100 * MB)
        self.assertEqual(planner.next_offset, 0)
        self.assertEqual(planner.next_index, 0)
        spec = planner.next_chunk()
        assert spec is not None
        self.assertEqual(planner.next_offset, spec.end_byte + 1)
        self.assertEqual(planner.next_index, 1)

    def test_remaining_bytes_decreases(self) -> None:
        size = 100 * MB
        planner = DynamicChunkPlanner(file_size=size)
        self.assertEqual(planner.remaining_bytes, size)
        spec = planner.next_chunk()
        assert spec is not None
        self.assertEqual(planner.remaining_bytes, size - spec.size)

    def test_exhaustion_returns_none_repeatedly(self) -> None:
        planner = DynamicChunkPlanner(file_size=1024)
        planner.next_chunk()
        self.assertTrue(planner.is_exhausted)
        self.assertIsNone(planner.next_chunk())
        self.assertIsNone(planner.next_chunk())
        self.assertEqual(planner.remaining_bytes, 0)

    def test_reset_restarts_partition(self) -> None:
        planner = DynamicChunkPlanner(file_size=32 * MB)
        planner.plan_all()
        planner.reset()
        self.assertEqual(planner.next_offset, 0)
        self.assertEqual(planner.next_index, 0)
        assert_partition(self, planner.plan_all(), 32 * MB)


class TestSerialization(unittest.TestCase):
    """Planner position must survive a serialize/restore cycle."""

    def test_round_trip_preserves_cursor(self) -> None:
        planner = DynamicChunkPlanner(file_size=200 * MB)
        planner.next_chunk(snapshot())
        planner.next_chunk(snapshot())
        restored = DynamicChunkPlanner.from_dict(planner.to_dict())
        self.assertEqual(restored.next_offset, planner.next_offset)
        self.assertEqual(restored.next_index, planner.next_index)
        self.assertEqual(restored.file_size, planner.file_size)

    def test_resume_completes_partition_without_gaps(self) -> None:
        """A resumed planner must cover exactly the bytes the original left."""
        size = 300 * MB + 4321
        original = DynamicChunkPlanner(file_size=size)
        issued = [original.next_chunk(snapshot(drop=0.1)) for _ in range(3)]
        resumed = DynamicChunkPlanner.from_dict(original.to_dict())
        remainder = resumed.plan_all(snapshot(drop=0.6))
        combined = [s for s in issued if s is not None] + remainder
        assert_partition(self, combined, size)

    def test_resume_preserves_index_continuity(self) -> None:
        original = DynamicChunkPlanner(file_size=100 * MB)
        original.next_chunk()
        original.next_chunk()
        resumed = DynamicChunkPlanner.from_dict(original.to_dict())
        spec = resumed.next_chunk()
        assert spec is not None
        self.assertEqual(spec.index, 2)

    def test_serialized_form_is_json_safe(self) -> None:
        import json

        planner = DynamicChunkPlanner(file_size=50 * MB)
        planner.next_chunk()
        self.assertEqual(
            json.loads(json.dumps(planner.to_dict())), planner.to_dict()
        )

    def test_rejects_incomplete_payload(self) -> None:
        for payload in (
            {},
            {"file_size": 100},
            {"file_size": 100, "next_offset": 0},
            {"next_offset": 0, "next_index": 0},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(ConfigurationError):
                    DynamicChunkPlanner.from_dict(payload)


class TestValidation(unittest.TestCase):
    """Invalid planner construction must fail loudly."""

    def test_rejects_negative_file_size(self) -> None:
        with self.assertRaises(ConfigurationError):
            DynamicChunkPlanner(file_size=-1)

    def test_rejects_non_integer_file_size(self) -> None:
        for bad in (1.5, "100", True, None):
            with self.subTest(file_size=bad):
                with self.assertRaises(ConfigurationError):
                    DynamicChunkPlanner(file_size=bad)  # type: ignore[arg-type]

    def test_rejects_offset_past_end(self) -> None:
        with self.assertRaises(ConfigurationError):
            DynamicChunkPlanner(file_size=100, start_offset=101)

    def test_rejects_negative_offset_or_index(self) -> None:
        with self.assertRaises(ConfigurationError):
            DynamicChunkPlanner(file_size=100, start_offset=-1)
        with self.assertRaises(ConfigurationError):
            DynamicChunkPlanner(file_size=100, start_index=-1)

    def test_offset_at_end_is_exhausted(self) -> None:
        planner = DynamicChunkPlanner(file_size=100, start_offset=100)
        self.assertTrue(planner.is_exhausted)
        self.assertIsNone(planner.next_chunk())


class TestConcurrency(unittest.TestCase):
    """Concurrent workers must never receive overlapping ranges."""

    def test_parallel_issuance_yields_valid_partition(self) -> None:
        size = 256 * MB
        planner = DynamicChunkPlanner(file_size=size)
        collected: list[ChunkSpec] = []
        guard = threading.Lock()

        def worker() -> None:
            while True:
                spec = planner.next_chunk(snapshot(drop=0.05))
                if spec is None:
                    return
                with guard:
                    collected.append(spec)

        pool = [threading.Thread(target=worker) for _ in range(8)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()

        collected.sort(key=lambda s: s.start_byte)
        assert_partition(self, collected, size)


if __name__ == "__main__":
    unittest.main()
