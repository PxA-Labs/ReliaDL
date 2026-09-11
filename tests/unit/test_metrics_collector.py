"""
Unit tests for real-time network metrics collection in src.algorithms.metrics_collector.
Verifies EWMA convergence and smoothing under noisy input, RFC 6298 deviation
tracking, sliding-window drop probability accuracy, BDP derivation, input
validation, and thread-safe concurrent ingestion.
"""

from __future__ import annotations

import statistics
import threading
import unittest

from src.algorithms.metrics_collector import (
    DEFAULT_FAILURE_WINDOW_SIZE,
    DEFAULT_RTT_ALPHA,
    DEFAULT_THROUGHPUT_BETA,
    EWMAEstimator,
    FailureWindow,
    NetworkMetricsCollector,
    NetworkStateSnapshot,
    TransferSample,
)
from src.exceptions import ConfigurationError


class TestEWMAEstimator(unittest.TestCase):
    """Verifies the exponentially weighted moving average filter."""

    def test_uninitialized_state(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        self.assertIsNone(est.value)
        self.assertFalse(est.is_initialized)
        self.assertEqual(est.count, 0)
        self.assertEqual(est.deviation, 0.0)

    def test_first_sample_seeds_directly(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        self.assertEqual(est.update(0.5), 0.5)
        self.assertTrue(est.is_initialized)
        self.assertEqual(est.count, 1)

    def test_recurrence_matches_closed_form(self) -> None:
        alpha = 0.2
        est = EWMAEstimator(alpha=alpha)
        est.update(100.0)
        est.update(200.0)
        # (1 - 0.2) * 100 + 0.2 * 200 = 120.0
        self.assertAlmostEqual(est.value, 120.0, places=9)
        est.update(200.0)
        self.assertAlmostEqual(est.value, 136.0, places=9)

    def test_smoothing_suppresses_noise(self) -> None:
        """A noisy signal around a fixed mean must smooth to near that mean."""
        est = EWMAEstimator(alpha=DEFAULT_RTT_ALPHA)
        noisy = [0.10, 0.30, 0.05, 0.35, 0.12, 0.28, 0.08, 0.32, 0.15, 0.25] * 8
        for sample in noisy:
            est.update(sample)
        self.assertAlmostEqual(est.value, statistics.fmean(noisy), delta=0.05)
        # The filtered output must vary far less than the raw input.
        self.assertLess(est.deviation, statistics.pstdev(noisy) * 1.5)

    def test_converges_to_step_change(self) -> None:
        """After a step change the estimate must track the new level."""
        est = EWMAEstimator(alpha=0.3)
        for _ in range(50):
            est.update(1.0)
        self.assertAlmostEqual(est.value, 1.0, places=6)
        for _ in range(50):
            est.update(5.0)
        self.assertAlmostEqual(est.value, 5.0, places=4)

    def test_higher_alpha_adapts_faster(self) -> None:
        slow, fast = EWMAEstimator(alpha=0.1), EWMAEstimator(alpha=0.9)
        for est in (slow, fast):
            est.update(0.0)
            est.update(10.0)
        self.assertLess(slow.value, fast.value)

    def test_alpha_one_tracks_raw_signal(self) -> None:
        est = EWMAEstimator(alpha=1.0)
        est.update(3.0)
        self.assertEqual(est.update(7.0), 7.0)

    def test_deviation_zero_for_constant_signal(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        for _ in range(20):
            est.update(0.25)
        self.assertAlmostEqual(est.deviation, 0.0, places=9)

    def test_deviation_grows_with_jitter(self) -> None:
        steady, jittery = EWMAEstimator(alpha=0.2), EWMAEstimator(alpha=0.2)
        for i in range(40):
            steady.update(1.0)
            jittery.update(1.0 if i % 2 == 0 else 3.0)
        self.assertGreater(jittery.deviation, steady.deviation)

    def test_reset_clears_state(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        est.update(1.0)
        est.update(2.0)
        est.reset()
        self.assertIsNone(est.value)
        self.assertEqual(est.count, 0)
        self.assertEqual(est.deviation, 0.0)

    def test_rejects_invalid_alpha(self) -> None:
        for bad in (0.0, -0.1, 1.5, "0.2", None, True):
            with self.subTest(alpha=bad):
                with self.assertRaises(ConfigurationError):
                    EWMAEstimator(alpha=bad)

    def test_rejects_non_numeric_sample(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        for bad in ("fast", None, True, [1.0]):
            with self.subTest(sample=bad):
                with self.assertRaises(ConfigurationError):
                    est.update(bad)

    def test_rejects_non_finite_sample(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        for bad in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(sample=bad):
                with self.assertRaises(ConfigurationError):
                    est.update(bad)

    def test_rejected_sample_leaves_state_untouched(self) -> None:
        est = EWMAEstimator(alpha=0.2)
        est.update(1.0)
        with self.assertRaises(ConfigurationError):
            est.update(float("nan"))
        self.assertEqual(est.value, 1.0)
        self.assertEqual(est.count, 1)


class TestFailureWindow(unittest.TestCase):
    """Verifies the bounded sliding window of transfer outcomes."""

    def test_empty_window_reports_zero(self) -> None:
        window = FailureWindow(size=10)
        self.assertEqual(window.failure_ratio, 0.0)
        self.assertEqual(window.observations, 0)
        self.assertEqual(len(window), 0)

    def test_all_failures(self) -> None:
        window = FailureWindow(size=5)
        for _ in range(5):
            window.record_failure()
        self.assertEqual(window.failure_ratio, 1.0)
        self.assertEqual(window.failures, 5)

    def test_mixed_ratio(self) -> None:
        window = FailureWindow(size=10)
        for i in range(10):
            window.record(i % 4 != 0)
        # Failures at i = 0, 4, 8 -> 3 of 10.
        self.assertAlmostEqual(window.failure_ratio, 0.3, places=9)

    def test_ratio_uses_partial_window(self) -> None:
        """Before the window fills, the ratio divides by observations seen."""
        window = FailureWindow(size=100)
        window.record_failure()
        window.record_success()
        self.assertAlmostEqual(window.failure_ratio, 0.5, places=9)
        self.assertEqual(window.observations, 2)

    def test_eviction_drops_stale_failures(self) -> None:
        """Recovered paths must return to a zero drop probability."""
        window = FailureWindow(size=10)
        for _ in range(10):
            window.record_failure()
        self.assertEqual(window.failure_ratio, 1.0)
        for _ in range(10):
            window.record_success()
        self.assertEqual(window.failure_ratio, 0.0)
        self.assertEqual(window.failures, 0)
        self.assertEqual(window.observations, 10)

    def test_window_never_exceeds_capacity(self) -> None:
        window = FailureWindow(size=8)
        for _ in range(500):
            window.record_success()
        self.assertEqual(window.observations, 8)
        self.assertEqual(window.total_recorded, 500)

    def test_failure_count_tracks_window_contents(self) -> None:
        """Internal failure tally must stay consistent across many evictions."""
        window = FailureWindow(size=16)
        for i in range(300):
            window.record(i % 3 != 0)
        self.assertEqual(window.failures, sum(1 for f in window._window if not f))

    def test_reset_clears_window(self) -> None:
        window = FailureWindow(size=5)
        window.record_failure()
        window.reset()
        self.assertEqual(window.failure_ratio, 0.0)
        self.assertEqual(window.total_recorded, 0)

    def test_rejects_invalid_size(self) -> None:
        for bad in (0, -1, 2.5, "10", True):
            with self.subTest(size=bad):
                with self.assertRaises(ConfigurationError):
                    FailureWindow(size=bad)


class TestTransferSample(unittest.TestCase):
    """Verifies transfer observation validation and derived throughput."""

    def test_throughput_derivation(self) -> None:
        sample = TransferSample(bytes_transferred=8_000_000, duration_seconds=2.0)
        self.assertAlmostEqual(sample.throughput_bps, 4_000_000.0, places=6)

    def test_rejects_non_positive_duration(self) -> None:
        for bad in (0.0, -1.0):
            with self.subTest(duration=bad):
                with self.assertRaises(ConfigurationError):
                    TransferSample(bytes_transferred=100, duration_seconds=bad)

    def test_rejects_negative_bytes(self) -> None:
        with self.assertRaises(ConfigurationError):
            TransferSample(bytes_transferred=-1, duration_seconds=1.0)

    def test_rejects_negative_header_latency(self) -> None:
        with self.assertRaises(ConfigurationError):
            TransferSample(
                bytes_transferred=100,
                duration_seconds=1.0,
                header_latency_seconds=-0.01,
            )

    def test_is_immutable(self) -> None:
        sample = TransferSample(bytes_transferred=100, duration_seconds=1.0)
        with self.assertRaises(Exception):
            sample.bytes_transferred = 200  # type: ignore[misc]


class TestNetworkMetricsCollector(unittest.TestCase):
    """Verifies aggregate network state estimation across worker reports."""

    def test_unprimed_snapshot(self) -> None:
        collector = NetworkMetricsCollector()
        state = collector.snapshot()
        self.assertIsInstance(state, NetworkStateSnapshot)
        self.assertIsNone(state.rtt_seconds)
        self.assertIsNone(state.throughput_bps)
        self.assertIsNone(state.bdp_bytes)
        self.assertEqual(state.drop_probability, 0.0)
        self.assertFalse(state.is_primed)

    def test_primed_after_successful_transfer(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=1_000_000,
            duration_seconds=1.0,
            header_latency_seconds=0.05,
        )
        state = collector.snapshot()
        self.assertTrue(state.is_primed)
        self.assertAlmostEqual(state.rtt_seconds, 0.05, places=9)
        self.assertAlmostEqual(state.throughput_bps, 1_000_000.0, places=6)

    def test_bdp_is_throughput_times_rtt(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=10_000_000,
            duration_seconds=1.0,
            header_latency_seconds=0.04,
        )
        state = collector.snapshot()
        self.assertAlmostEqual(state.bdp_bytes, 10_000_000.0 * 0.04, places=3)

    def test_failed_transfer_excluded_from_throughput(self) -> None:
        """A partial failed transfer must not drag the throughput estimate."""
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=10_000_000, duration_seconds=1.0
        )
        baseline = collector.snapshot().throughput_bps
        collector.record_sample(
            bytes_transferred=1, duration_seconds=1.0, success=False
        )
        self.assertEqual(collector.snapshot().throughput_bps, baseline)

    def test_failed_transfer_counted_in_drop_probability(self) -> None:
        collector = NetworkMetricsCollector()
        for _ in range(3):
            collector.record_sample(bytes_transferred=1000, duration_seconds=1.0)
        collector.record_sample(
            bytes_transferred=0, duration_seconds=1.0, success=False
        )
        state = collector.snapshot()
        self.assertAlmostEqual(state.drop_probability, 0.25, places=9)
        self.assertEqual(state.samples, 4)
        self.assertEqual(state.failures, 1)

    def test_record_failure_without_timing(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_failure()
        state = collector.snapshot()
        self.assertEqual(state.drop_probability, 1.0)
        self.assertIsNone(state.throughput_bps)

    def test_header_latency_optional(self) -> None:
        """Transfers reporting no header latency leave the RTT filter unprimed."""
        collector = NetworkMetricsCollector()
        collector.record_sample(bytes_transferred=1000, duration_seconds=1.0)
        state = collector.snapshot()
        self.assertIsNone(state.rtt_seconds)
        self.assertIsNotNone(state.throughput_bps)
        self.assertIsNone(state.bdp_bytes)

    def test_standalone_rtt_probe(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_rtt(0.12)
        self.assertAlmostEqual(collector.snapshot().rtt_seconds, 0.12, places=9)

    def test_rejects_negative_rtt_probe(self) -> None:
        collector = NetworkMetricsCollector()
        with self.assertRaises(ConfigurationError):
            collector.record_rtt(-0.01)

    def test_throughput_smoothed_across_workers(self) -> None:
        """Heterogeneous worker rates converge toward the population mean."""
        collector = NetworkMetricsCollector(throughput_beta=DEFAULT_THROUGHPUT_BETA)
        rates = [4_000_000.0, 6_000_000.0] * 40
        for rate in rates:
            collector.record_sample(
                bytes_transferred=int(rate), duration_seconds=1.0
            )
        self.assertAlmostEqual(
            collector.snapshot().throughput_bps, 5_000_000.0, delta=600_000.0
        )

    def test_mean_transfer_bytes_tracked(self) -> None:
        collector = NetworkMetricsCollector()
        for _ in range(30):
            collector.record_sample(
                bytes_transferred=8 * 1024 * 1024, duration_seconds=1.0
            )
        self.assertAlmostEqual(
            collector.snapshot().mean_transfer_bytes,
            float(8 * 1024 * 1024),
            delta=1.0,
        )

    def test_mean_transfer_bytes_excludes_failures(self) -> None:
        """A failed partial transfer must not shrink the observed chunk size."""
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=8 * 1024 * 1024, duration_seconds=1.0
        )
        baseline = collector.snapshot().mean_transfer_bytes
        collector.record_sample(
            bytes_transferred=512, duration_seconds=1.0, success=False
        )
        self.assertEqual(collector.snapshot().mean_transfer_bytes, baseline)

    def test_mean_transfer_bytes_none_before_priming(self) -> None:
        self.assertIsNone(NetworkMetricsCollector().snapshot().mean_transfer_bytes)

    def test_snapshot_is_immutable(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(bytes_transferred=1000, duration_seconds=1.0)
        state = collector.snapshot()
        with self.assertRaises(Exception):
            state.drop_probability = 0.9  # type: ignore[misc]

    def test_snapshot_does_not_track_later_writes(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(bytes_transferred=1000, duration_seconds=1.0)
        state = collector.snapshot()
        collector.record_failure()
        self.assertEqual(state.drop_probability, 0.0)
        self.assertAlmostEqual(collector.snapshot().drop_probability, 0.5, places=9)

    def test_reset_clears_all_estimators(self) -> None:
        collector = NetworkMetricsCollector()
        collector.record_sample(
            bytes_transferred=1000,
            duration_seconds=1.0,
            header_latency_seconds=0.05,
        )
        collector.reset()
        state = collector.snapshot()
        self.assertIsNone(state.rtt_seconds)
        self.assertIsNone(state.throughput_bps)
        self.assertEqual(state.drop_probability, 0.0)

    def test_default_window_size_honoured(self) -> None:
        collector = NetworkMetricsCollector()
        self.assertEqual(collector.failure_window.size, DEFAULT_FAILURE_WINDOW_SIZE)

    def test_concurrent_ingestion_loses_no_samples(self) -> None:
        """Concurrent worker reports must not race the shared counters."""
        collector = NetworkMetricsCollector(failure_window_size=10_000)
        per_thread, threads = 500, 8

        def worker() -> None:
            for _ in range(per_thread):
                collector.record_sample(
                    bytes_transferred=1000,
                    duration_seconds=1.0,
                    header_latency_seconds=0.05,
                )

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for thread in pool:
            thread.start()
        for thread in pool:
            thread.join()

        expected = per_thread * threads
        self.assertEqual(collector.failure_window.total_recorded, expected)
        self.assertEqual(collector.rtt_estimator.count, expected)
        self.assertEqual(collector.throughput_estimator.count, expected)


if __name__ == "__main__":
    unittest.main()
