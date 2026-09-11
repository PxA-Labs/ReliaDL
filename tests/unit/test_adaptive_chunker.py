"""
Unit tests for the BL-DCS closed-loop chunk sizing controller in
src.algorithms.adaptive_chunker. Verifies monotonic response to loss and
bandwidth, power-of-two alignment, bounds enforcement, segment loss rescaling,
cold-start fallback, and configuration validation.
"""

from __future__ import annotations

import unittest

from src.algorithms.adaptive_chunker import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_MSS_BYTES,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    BLDCSController,
    ChunkSizingDecision,
    align_to_power_of_two,
    is_power_of_two,
)
from src.algorithms.metrics_collector import (
    NetworkMetricsCollector,
    NetworkStateSnapshot,
)
from src.exceptions import ConfigurationError

MB = 1024 * 1024


def snapshot(
    throughput_mbps: float = 12.5,
    rtt: float = 0.05,
    drop: float = 0.0,
    observed_bytes: float = 8 * MB,
) -> NetworkStateSnapshot:
    """Build a primed network state for a given path condition."""
    throughput = throughput_mbps * MB
    return NetworkStateSnapshot(
        rtt_seconds=rtt,
        rtt_deviation_seconds=0.0,
        throughput_bps=throughput,
        drop_probability=drop,
        bdp_bytes=throughput * rtt,
        mean_transfer_bytes=observed_bytes,
        samples=100,
        failures=0,
    )


class TestPowerOfTwoHelpers(unittest.TestCase):
    """Verifies power-of-two detection and log-space quantization."""

    def test_is_power_of_two(self) -> None:
        for good in (1, 2, 4, 1024, MB, 64 * MB):
            self.assertTrue(is_power_of_two(good))
        for bad in (0, -4, 3, 1000, 5 * MB):
            self.assertFalse(is_power_of_two(bad))

    def test_alignment_is_exact_on_powers(self) -> None:
        for value in (MB, 2 * MB, 8 * MB, 64 * MB):
            self.assertEqual(align_to_power_of_two(value), value)

    def test_alignment_rounds_in_log_space(self) -> None:
        # 12 MB is linearly equidistant from 8 and 16 MB but nearer 8 in log2.
        self.assertEqual(align_to_power_of_two(11 * MB), 8 * MB)
        self.assertEqual(align_to_power_of_two(13 * MB), 16 * MB)

    def test_alignment_output_is_always_power_of_two(self) -> None:
        for value in (1.0, 3.7, 1000.0, 5 * MB, 33 * MB, 1e12):
            self.assertTrue(is_power_of_two(align_to_power_of_two(value)))

    def test_alignment_floor(self) -> None:
        self.assertEqual(align_to_power_of_two(0.0), 1)
        self.assertEqual(align_to_power_of_two(-5.0), 1)


class TestLossResponse(unittest.TestCase):
    """Chunk size must shrink as the observed failure ratio rises."""

    def setUp(self) -> None:
        self.controller = BLDCSController(align_power_of_two=False)

    def test_monotonic_non_increasing_in_loss(self) -> None:
        previous = None
        for drop in (0.0, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 0.9, 1.0):
            raw = self.controller.decide(snapshot(drop=drop)).raw_size
            if previous is not None:
                self.assertLessEqual(raw, previous + 1e-9, f"rose at p={drop}")
            previous = raw

    def test_high_loss_shrinks_chunk_materially(self) -> None:
        clean = self.controller.decide(snapshot(drop=0.01)).raw_size
        lossy = self.controller.decide(snapshot(drop=0.30)).raw_size
        self.assertLess(lossy, clean / 2.0)

    def test_aligned_sizes_step_down_with_loss(self) -> None:
        aligned = BLDCSController()
        sizes = [
            aligned.compute_chunk_size(snapshot(drop=p))
            for p in (0.01, 0.05, 0.30, 0.80)
        ]
        self.assertEqual(sizes, sorted(sizes, reverse=True))
        self.assertGreater(sizes[0], sizes[-1])

    def test_lossless_path_reaches_upper_bound(self) -> None:
        decision = BLDCSController().decide(snapshot(drop=0.0))
        self.assertEqual(decision.chunk_size, MAX_CHUNK_SIZE)
        self.assertTrue(decision.clamped_high)

    def test_total_loss_floors_at_minimum(self) -> None:
        decision = BLDCSController().decide(
            snapshot(throughput_mbps=0.5, rtt=0.2, drop=1.0)
        )
        self.assertEqual(decision.chunk_size, MIN_CHUNK_SIZE)


class TestBandwidthResponse(unittest.TestCase):
    """Chunk size must grow with available bandwidth on a clean link."""

    def setUp(self) -> None:
        self.controller = BLDCSController(align_power_of_two=False)

    def test_monotonic_non_decreasing_in_throughput(self) -> None:
        previous = None
        for mbps in (0.5, 2.0, 12.5, 100.0, 1250.0):
            raw = self.controller.decide(
                snapshot(throughput_mbps=mbps, drop=0.02)
            ).raw_size
            if previous is not None:
                self.assertGreaterEqual(raw, previous - 1e-9)
            previous = raw

    def test_faster_link_yields_larger_chunk(self) -> None:
        slow = self.controller.decide(
            snapshot(throughput_mbps=1.0, drop=0.02)
        ).raw_size
        fast = self.controller.decide(
            snapshot(throughput_mbps=500.0, drop=0.02)
        ).raw_size
        self.assertGreater(fast, slow)

    def test_bdp_term_tracks_bandwidth_delay_product(self) -> None:
        controller = BLDCSController(lambda_bdp=2.0, align_power_of_two=False)
        state = snapshot(throughput_mbps=100.0, rtt=0.04, drop=0.02)
        decision = controller.decide(state)
        self.assertAlmostEqual(
            decision.bdp_term, 2.0 * (100.0 * MB) * 0.04, places=3
        )

    def test_high_rtt_increases_chunk_via_bdp(self) -> None:
        low = self.controller.decide(snapshot(rtt=0.01, drop=0.05)).raw_size
        high = self.controller.decide(snapshot(rtt=0.30, drop=0.05)).raw_size
        self.assertGreater(high, low)


class TestSegmentLossRescaling(unittest.TestCase):
    """Chunk-level failure ratios must be converted to per-segment loss."""

    def test_segment_loss_far_below_chunk_loss(self) -> None:
        decision = BLDCSController().decide(snapshot(drop=0.05))
        self.assertLess(decision.segment_drop, 0.05)
        self.assertGreater(decision.segment_drop, 0.0)

    def test_inversion_reproduces_chunk_probability(self) -> None:
        """1 - (1 - p_seg)^n must recover the original chunk failure ratio."""
        observed = 8 * MB
        chunk_drop = 0.05
        decision = BLDCSController().decide(
            snapshot(drop=chunk_drop, observed_bytes=observed)
        )
        segments = observed / DEFAULT_MSS_BYTES
        recovered = 1.0 - (1.0 - decision.segment_drop) ** segments
        self.assertAlmostEqual(recovered, chunk_drop, places=6)

    def test_larger_observed_chunk_implies_lower_segment_loss(self) -> None:
        small = BLDCSController().decide(
            snapshot(drop=0.05, observed_bytes=1 * MB)
        ).segment_drop
        large = BLDCSController().decide(
            snapshot(drop=0.05, observed_bytes=32 * MB)
        ).segment_drop
        self.assertLess(large, small)

    def test_degenerate_bounds(self) -> None:
        self.assertEqual(BLDCSController().decide(snapshot(drop=0.0)).segment_drop, 0.0)
        self.assertEqual(BLDCSController().decide(snapshot(drop=1.0)).segment_drop, 1.0)

    def test_missing_observed_size_uses_default(self) -> None:
        state = snapshot(drop=0.05)
        without = NetworkStateSnapshot(
            rtt_seconds=state.rtt_seconds,
            rtt_deviation_seconds=0.0,
            throughput_bps=state.throughput_bps,
            drop_probability=0.05,
            bdp_bytes=state.bdp_bytes,
            mean_transfer_bytes=None,
            samples=100,
            failures=5,
        )
        reference = snapshot(drop=0.05, observed_bytes=DEFAULT_CHUNK_SIZE)
        self.assertAlmostEqual(
            BLDCSController().decide(without).raw_size,
            BLDCSController().decide(reference).raw_size,
            places=6,
        )


class TestBoundsEnforcement(unittest.TestCase):
    """Outputs must respect the configured hardware bounds in every regime."""

    def test_output_within_bounds_across_wide_sweep(self) -> None:
        controller = BLDCSController()
        for mbps in (0.01, 0.5, 12.5, 100.0, 1250.0, 12500.0):
            for rtt in (0.001, 0.05, 0.5, 2.0):
                for drop in (0.0, 0.001, 0.05, 0.5, 1.0):
                    size = controller.compute_chunk_size(
                        snapshot(throughput_mbps=mbps, rtt=rtt, drop=drop)
                    )
                    with self.subTest(mbps=mbps, rtt=rtt, drop=drop):
                        self.assertGreaterEqual(size, MIN_CHUNK_SIZE)
                        self.assertLessEqual(size, MAX_CHUNK_SIZE)
                        self.assertTrue(is_power_of_two(size))

    def test_custom_bounds_respected(self) -> None:
        controller = BLDCSController(
            min_chunk_size=2 * MB,
            max_chunk_size=16 * MB,
            default_chunk_size=4 * MB,
        )
        self.assertEqual(
            controller.compute_chunk_size(snapshot(drop=0.0)), 16 * MB
        )
        self.assertEqual(
            controller.compute_chunk_size(
                snapshot(throughput_mbps=0.1, rtt=0.01, drop=0.99)
            ),
            2 * MB,
        )

    def test_alignment_never_escapes_upper_bound(self) -> None:
        """Rounding up must not push the result past the configured maximum."""
        controller = BLDCSController(
            min_chunk_size=MB, max_chunk_size=48 * MB, default_chunk_size=8 * MB,
            align_power_of_two=False,
        )
        for drop in (0.0, 1e-6, 1e-4):
            self.assertLessEqual(
                controller.compute_chunk_size(snapshot(drop=drop)), 48 * MB
            )

    def test_clamp_flags_reported(self) -> None:
        controller = BLDCSController()
        self.assertTrue(controller.decide(snapshot(drop=0.0)).clamped_high)
        low = controller.decide(snapshot(throughput_mbps=0.05, rtt=0.01, drop=0.99))
        self.assertTrue(low.clamped_low)
        self.assertTrue(low.clamped)

    def test_equal_bounds_pin_output(self) -> None:
        controller = BLDCSController(
            min_chunk_size=4 * MB, max_chunk_size=4 * MB, default_chunk_size=4 * MB
        )
        for drop in (0.0, 0.5, 1.0):
            self.assertEqual(controller.compute_chunk_size(snapshot(drop=drop)), 4 * MB)


class TestColdStart(unittest.TestCase):
    """Unprimed estimators must yield the configured default, not a guess."""

    def test_unprimed_state_returns_default(self) -> None:
        controller = BLDCSController()
        empty = NetworkMetricsCollector().snapshot()
        decision = controller.decide(empty)
        self.assertEqual(decision.chunk_size, DEFAULT_CHUNK_SIZE)
        self.assertFalse(decision.primed)
        self.assertFalse(decision.clamped)

    def test_partial_state_returns_default(self) -> None:
        """Throughput without RTT is not enough to evaluate the BDP term."""
        collector = NetworkMetricsCollector()
        collector.record_sample(bytes_transferred=1_000_000, duration_seconds=1.0)
        decision = BLDCSController().decide(collector.snapshot())
        self.assertFalse(decision.primed)
        self.assertEqual(decision.chunk_size, DEFAULT_CHUNK_SIZE)

    def test_primed_flag_set_once_estimators_ready(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=8 * MB,
            duration_seconds=1.0,
            header_latency_seconds=0.05,
        )
        self.assertTrue(BLDCSController().decide(collector.snapshot()).primed)


class TestEndToEndWithCollector(unittest.TestCase):
    """Controller must react to live observations fed through the collector."""

    def test_degrading_path_reduces_chunk_size(self) -> None:
        collector = NetworkMetricsCollector(failure_window_size=20)
        controller = BLDCSController()

        for _ in range(20):
            collector.record_sample(
                bytes_transferred=8 * MB,
                duration_seconds=0.64,
                header_latency_seconds=0.05,
            )
        clean = controller.compute_chunk_size(collector.snapshot())

        for _ in range(10):
            collector.record_sample(
                bytes_transferred=8 * MB,
                duration_seconds=0.64,
                header_latency_seconds=0.05,
                success=False,
            )
        degraded = controller.compute_chunk_size(collector.snapshot())

        self.assertLess(degraded, clean)

    def test_recovering_path_restores_chunk_size(self) -> None:
        collector = NetworkMetricsCollector(failure_window_size=10)
        controller = BLDCSController()
        for _ in range(10):
            collector.record_sample(
                bytes_transferred=8 * MB,
                duration_seconds=0.64,
                header_latency_seconds=0.05,
                success=False,
            )
        degraded = controller.compute_chunk_size(collector.snapshot())
        for _ in range(10):
            collector.record_sample(
                bytes_transferred=8 * MB,
                duration_seconds=0.64,
                header_latency_seconds=0.05,
            )
        recovered = controller.compute_chunk_size(collector.snapshot())
        self.assertGreater(recovered, degraded)


class TestConfigurationValidation(unittest.TestCase):
    """Invalid controller configuration must fail loudly at construction."""

    def test_rejects_non_positive_coefficients(self) -> None:
        for kwargs in (
            {"gamma": 0.0},
            {"gamma": -1.0},
            {"lambda_bdp": 0.0},
            {"overhead_seconds": -0.01},
            {"epsilon": 0.0},
            {"mss_bytes": 0},
            {"gamma": float("inf")},
            {"gamma": float("nan")},
            {"lambda_bdp": "fast"},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ConfigurationError):
                    BLDCSController(**kwargs)  # type: ignore[arg-type]

    def test_rejects_inverted_bounds(self) -> None:
        with self.assertRaises(ConfigurationError):
            BLDCSController(min_chunk_size=16 * MB, max_chunk_size=4 * MB)

    def test_rejects_default_outside_bounds(self) -> None:
        with self.assertRaises(ConfigurationError):
            BLDCSController(
                min_chunk_size=2 * MB,
                max_chunk_size=8 * MB,
                default_chunk_size=32 * MB,
            )

    def test_rejects_non_power_of_two_bounds_when_aligning(self) -> None:
        with self.assertRaises(ConfigurationError):
            BLDCSController(
                min_chunk_size=3 * MB,
                max_chunk_size=64 * MB,
                default_chunk_size=8 * MB,
            )

    def test_allows_non_power_of_two_bounds_without_alignment(self) -> None:
        controller = BLDCSController(
            min_chunk_size=3 * MB,
            max_chunk_size=48 * MB,
            default_chunk_size=8 * MB,
            align_power_of_two=False,
        )
        self.assertFalse(controller.aligns_to_power_of_two)

    def test_rejects_invalid_bound_types(self) -> None:
        for kwargs in (
            {"min_chunk_size": 0},
            {"max_chunk_size": -1},
            {"min_chunk_size": 1.5},
            {"max_chunk_size": True},
        ):
            with self.subTest(**kwargs):
                with self.assertRaises(ConfigurationError):
                    BLDCSController(**kwargs)  # type: ignore[arg-type]


class TestDecisionRecord(unittest.TestCase):
    """The decision record must expose intermediate terms for observability."""

    def test_terms_sum_to_raw_size(self) -> None:
        decision = BLDCSController(align_power_of_two=False).decide(
            snapshot(drop=0.05)
        )
        self.assertAlmostEqual(
            decision.loss_term + decision.bdp_term, decision.raw_size, places=6
        )

    def test_decision_is_immutable(self) -> None:
        decision = BLDCSController().decide(snapshot())
        self.assertIsInstance(decision, ChunkSizingDecision)
        with self.assertRaises(Exception):
            decision.chunk_size = 1  # type: ignore[misc]

    def test_controller_is_stateless(self) -> None:
        """Repeated queries with the same state must return the same decision."""
        controller = BLDCSController()
        state = snapshot(drop=0.05)
        first = controller.decide(state)
        for _ in range(5):
            controller.decide(snapshot(drop=0.9))
        self.assertEqual(controller.decide(state), first)


if __name__ == "__main__":
    unittest.main()
