"""
Unit tests for the SR-WSRS straggler detection monitor in
src.algorithms.work_stealer. Verifies TTC projection including the stalled and
unprimed edges, the two-part flagging predicate, the active-only baseline that
keeps finished workers from poisoning the comparison, worst-first ordering, and
the criterion that healthy workers are never falsely flagged.
"""

from __future__ import annotations

import math
import unittest

from src.algorithms.work_stealer import (
    DEFAULT_MIN_SPLIT_BYTES,
    DEFAULT_STRAGGLER_FACTOR,
    StragglerDetector,
    StragglerReason,
    StragglerVerdict,
    WorkerProgress,
)
from src.exceptions import ConfigurationError

MB = 1024 * 1024


def worker(
    worker_id: str,
    remaining: int,
    throughput_mbps: float | None,
    start: int = 0,
    completed: int = 0,
) -> WorkerProgress:
    """
    Build a report holding `remaining` bytes at a given throughput.

    Expressed in terms of the remainder because that, not the absolute range, is
    what the predicate reasons about.
    """
    next_byte = start + completed
    end = next_byte + remaining - 1
    return WorkerProgress(
        worker_id=worker_id,
        range_start=start,
        range_end=end,
        next_byte=next_byte,
        throughput_bps=None if throughput_mbps is None else throughput_mbps * MB,
    )


class TestWorkerProgressValidation(unittest.TestCase):
    """Malformed progress reports must be rejected at construction."""

    def test_valid_report_accepted(self) -> None:
        report = WorkerProgress("w1", 0, 999, 500, 1024.0)
        self.assertEqual(report.worker_id, "w1")
        self.assertEqual(report.throughput_bps, 1024.0)

    def test_empty_worker_id_rejected(self) -> None:
        for bad in ("", None, 5):
            with self.assertRaises(ConfigurationError):
                WorkerProgress(bad, 0, 10, 0)  # type: ignore[arg-type]

    def test_non_integer_offsets_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", 0.0, 10, 0)  # type: ignore[arg-type]
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", 0, 10, True)  # type: ignore[arg-type]

    def test_negative_range_start_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", -1, 10, 0)

    def test_inverted_range_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", 100, 50, 100)

    def test_cursor_outside_range_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", 10, 20, 9)
        with self.assertRaises(ConfigurationError):
            WorkerProgress("w1", 10, 20, 22)

    def test_cursor_one_past_end_is_valid(self) -> None:
        """next_byte == range_end + 1 is how a completed range is expressed."""
        report = WorkerProgress("w1", 10, 20, 21)
        self.assertTrue(report.is_complete)
        self.assertEqual(report.bytes_remaining, 0)

    def test_single_byte_range_is_valid(self) -> None:
        report = WorkerProgress("w1", 5, 5, 5)
        self.assertEqual(report.bytes_total, 1)
        self.assertEqual(report.bytes_remaining, 1)

    def test_negative_or_non_finite_throughput_rejected(self) -> None:
        for bad in (-1.0, math.inf, math.nan, -math.inf):
            with self.assertRaises(ConfigurationError):
                WorkerProgress("w1", 0, 10, 0, bad)

    def test_non_numeric_throughput_rejected(self) -> None:
        for bad in ("fast", True, [1]):
            with self.assertRaises(ConfigurationError):
                WorkerProgress("w1", 0, 10, 0, bad)  # type: ignore[arg-type]

    def test_integer_throughput_normalized_to_float(self) -> None:
        self.assertIsInstance(WorkerProgress("w1", 0, 10, 0, 500).throughput_bps, float)

    def test_report_is_immutable(self) -> None:
        report = WorkerProgress("w1", 0, 10, 0)
        with self.assertRaises(Exception):
            report.next_byte = 5  # type: ignore[misc]


class TestByteArithmetic(unittest.TestCase):
    """Inclusive-range accounting must never miscount by one."""

    def test_totals_and_remainders(self) -> None:
        report = WorkerProgress("w1", 100, 199, 150)
        self.assertEqual(report.bytes_total, 100)
        self.assertEqual(report.bytes_completed, 50)
        self.assertEqual(report.bytes_remaining, 50)
        self.assertFalse(report.is_complete)

    def test_completed_plus_remaining_equals_total(self) -> None:
        for cursor in range(100, 201):
            report = WorkerProgress("w1", 100, 199, cursor)
            self.assertEqual(
                report.bytes_completed + report.bytes_remaining, report.bytes_total
            )

    def test_untouched_range(self) -> None:
        report = WorkerProgress("w1", 0, 99, 0)
        self.assertEqual(report.bytes_completed, 0)
        self.assertEqual(report.bytes_remaining, 100)
        self.assertEqual(report.completion_ratio, 0.0)

    def test_completion_ratio_reaches_one(self) -> None:
        self.assertEqual(WorkerProgress("w1", 0, 99, 100).completion_ratio, 1.0)


class TestTimeToCompletion(unittest.TestCase):
    """TTC projection, including the two edges the predicate depends on."""

    def test_projection_is_remaining_over_throughput(self) -> None:
        report = worker("w1", remaining=50 * MB, throughput_mbps=10.0)
        self.assertAlmostEqual(report.ttc_seconds, 5.0, places=6)

    def test_completed_range_projects_zero(self) -> None:
        report = WorkerProgress("w1", 0, 99, 100, 1024.0)
        self.assertEqual(report.ttc_seconds, 0.0)
        self.assertFalse(report.is_active)

    def test_unprimed_worker_has_no_projection(self) -> None:
        report = worker("w1", remaining=10 * MB, throughput_mbps=None)
        self.assertIsNone(report.ttc_seconds)
        self.assertFalse(report.has_estimate)
        self.assertFalse(report.is_active)

    def test_stalled_worker_projects_infinity(self) -> None:
        """A zero-throughput connection is the case the detector exists for."""
        report = worker("w1", remaining=10 * MB, throughput_mbps=0.0)
        self.assertEqual(report.ttc_seconds, math.inf)
        self.assertTrue(report.is_active)

    def test_active_projection_is_always_positive(self) -> None:
        """No active worker may project zero, or it would zero the baseline."""
        for throughput in (1e-6, 1.0, 1e6, 1e12):
            report = WorkerProgress("w1", 0, 0, 0, throughput)
            self.assertGreater(report.ttc_seconds, 0.0)

    def test_repr_renders_unknown_projection(self) -> None:
        self.assertIn("ttc=unknown", repr(worker("w1", 10, None)))
        self.assertIn("ttc=", repr(worker("w1", 10, 1.0)))


class TestFromObservation(unittest.TestCase):
    """Throughput derived from bytes received over elapsed wall-clock time."""

    def test_throughput_derived_from_progress(self) -> None:
        report = WorkerProgress.from_observation("w1", 0, 999, 500, elapsed_seconds=2.0)
        self.assertAlmostEqual(report.throughput_bps, 250.0, places=6)

    def test_no_bytes_received_reports_stall_not_absence(self) -> None:
        """
        A connected worker that has received nothing is stalled, not unmeasured.

        Reporting None here would excuse exactly the worker that most needs
        flagging, so the distinction is load-bearing.
        """
        report = WorkerProgress.from_observation("w1", 0, 999, 0, elapsed_seconds=30.0)
        self.assertEqual(report.throughput_bps, 0.0)
        self.assertEqual(report.ttc_seconds, math.inf)

    def test_non_positive_elapsed_rejected(self) -> None:
        for bad in (0.0, -1.0, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                WorkerProgress.from_observation("w1", 0, 99, 50, elapsed_seconds=bad)

    def test_non_numeric_elapsed_rejected(self) -> None:
        for bad in ("2", None, True):
            with self.assertRaises(ConfigurationError):
                WorkerProgress.from_observation(
                    "w1", 0, 99, 50, elapsed_seconds=bad  # type: ignore[arg-type]
                )


class TestDetectorConstruction(unittest.TestCase):
    """Detector configuration must reject values that would flag healthy workers."""

    def test_defaults(self) -> None:
        detector = StragglerDetector()
        self.assertEqual(detector.factor, DEFAULT_STRAGGLER_FACTOR)
        self.assertEqual(detector.min_split_bytes, DEFAULT_MIN_SPLIT_BYTES)
        self.assertEqual(detector.min_remaining_bytes, 2 * DEFAULT_MIN_SPLIT_BYTES)

    def test_factor_at_or_below_one_rejected(self) -> None:
        """A factor of 1 would flag any worker marginally behind the fastest."""
        for bad in (1.0, 0.5, 0.0, -2.0):
            with self.assertRaises(ConfigurationError):
                StragglerDetector(factor=bad)

    def test_non_finite_factor_rejected(self) -> None:
        for bad in (math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                StragglerDetector(factor=bad)

    def test_non_numeric_factor_rejected(self) -> None:
        for bad in ("2.5", None, True):
            with self.assertRaises(ConfigurationError):
                StragglerDetector(factor=bad)  # type: ignore[arg-type]

    def test_invalid_min_split_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                StragglerDetector(min_split_bytes=bad)
        for bad_type in (1.5, "1024", True):
            with self.assertRaises(ConfigurationError):
                StragglerDetector(min_split_bytes=bad_type)  # type: ignore[arg-type]

    def test_repr(self) -> None:
        text = repr(StragglerDetector(factor=3.0, min_split_bytes=MB))
        self.assertIn("factor=3.0", text)
        self.assertIn(f"min_split_bytes={MB}", text)


class TestBaseline(unittest.TestCase):
    """
    The baseline must reflect the fastest *active* peer and nothing else.

    Completed and unprimed workers carry no information about how fast the path
    is currently moving bytes, and a completed worker's zero projection would
    drive the threshold to zero and flag the entire pool.
    """

    def setUp(self) -> None:
        self.detector = StragglerDetector()

    def test_baseline_is_minimum_active_projection(self) -> None:
        reports = [
            worker("fast", remaining=10 * MB, throughput_mbps=10.0),   # 1s
            worker("mid", remaining=40 * MB, throughput_mbps=10.0),    # 4s
        ]
        self.assertAlmostEqual(self.detector.baseline_ttc(reports), 1.0, places=6)

    def test_named_worker_excluded_from_its_own_baseline(self) -> None:
        reports = [
            worker("a", remaining=10 * MB, throughput_mbps=10.0),  # 1s
            worker("b", remaining=40 * MB, throughput_mbps=10.0),  # 4s
        ]
        self.assertAlmostEqual(
            self.detector.baseline_ttc(reports, exclude_worker_id="a"), 4.0, places=6
        )

    def test_completed_workers_excluded(self) -> None:
        reports = [
            WorkerProgress("done", 0, 99, 100, 10.0 * MB),          # complete, ttc 0
            worker("active", remaining=20 * MB, throughput_mbps=10.0),  # 2s
        ]
        self.assertAlmostEqual(self.detector.baseline_ttc(reports), 2.0, places=6)

    def test_unprimed_workers_excluded(self) -> None:
        reports = [
            worker("unprimed", remaining=10 * MB, throughput_mbps=None),
            worker("active", remaining=20 * MB, throughput_mbps=10.0),
        ]
        self.assertAlmostEqual(self.detector.baseline_ttc(reports), 2.0, places=6)

    def test_no_active_peers_yields_no_baseline(self) -> None:
        self.assertIsNone(self.detector.baseline_ttc([]))
        self.assertIsNone(
            self.detector.baseline_ttc(
                [WorkerProgress("done", 0, 99, 100, 10.0 * MB)]
            )
        )

    def test_stalled_peer_can_be_the_baseline_when_alone(self) -> None:
        reports = [worker("stalled", remaining=10 * MB, throughput_mbps=0.0)]
        self.assertEqual(self.detector.baseline_ttc(reports), math.inf)


class TestStragglerDetection(unittest.TestCase):
    """The two-part predicate: relative slowness, then whether acting pays."""

    def setUp(self) -> None:
        self.detector = StragglerDetector()

    def _verdict(self, reports, worker_id: str) -> StragglerVerdict:
        return self.detector.verdicts_by_worker(reports)[worker_id]

    def test_degraded_worker_is_flagged(self) -> None:
        reports = [
            worker("healthy", remaining=50 * MB, throughput_mbps=10.0),  # 5s
            worker("slow", remaining=95 * MB, throughput_mbps=1.0),      # 95s
        ]
        verdict = self._verdict(reports, "slow")
        self.assertTrue(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.STRAGGLER)
        self.assertAlmostEqual(verdict.ratio, 19.0, places=6)
        self.assertAlmostEqual(verdict.threshold_ttc_seconds, 12.5, places=6)

    def test_stalled_worker_is_flagged(self) -> None:
        reports = [
            worker("healthy", remaining=50 * MB, throughput_mbps=10.0),
            worker("stalled", remaining=50 * MB, throughput_mbps=0.0),
        ]
        verdict = self._verdict(reports, "stalled")
        self.assertTrue(verdict.is_straggler)
        self.assertEqual(verdict.ttc_seconds, math.inf)

    def test_healthy_workers_are_never_flagged(self) -> None:
        """
        Acceptance criterion: no false positives across a spread of healthy rates.

        Throughputs span a 2.4x spread — well inside the 2.5 factor — over
        varied remainders, which is the realistic jitter between live
        connections to the same origin.
        """
        reports = [
            worker("w1", remaining=40 * MB, throughput_mbps=12.0),
            worker("w2", remaining=50 * MB, throughput_mbps=10.0),
            worker("w3", remaining=30 * MB, throughput_mbps=8.0),
            worker("w4", remaining=45 * MB, throughput_mbps=9.5),
            worker("w5", remaining=38 * MB, throughput_mbps=5.0),
        ]
        self.assertEqual(self.detector.detect(reports), [])
        for verdict in self.detector.evaluate(reports):
            self.assertFalse(verdict.is_straggler, f"{verdict.worker_id} false positive")
            self.assertIs(verdict.reason, StragglerReason.WITHIN_THRESHOLD)

    def test_identical_workers_are_never_flagged(self) -> None:
        reports = [worker(f"w{i}", remaining=50 * MB, throughput_mbps=10.0) for i in range(4)]
        self.assertEqual(self.detector.detect(reports), [])

    def test_uniformly_slow_pool_is_not_a_straggler_problem(self) -> None:
        """A slow network is not a tail-latency problem; splitting helps nobody."""
        reports = [worker(f"w{i}", remaining=50 * MB, throughput_mbps=0.05) for i in range(4)]
        self.assertEqual(self.detector.detect(reports), [])

    def test_all_stalled_pool_is_not_flagged(self) -> None:
        """Every projection is infinite, so none dominates; nothing to steal to."""
        reports = [worker(f"w{i}", remaining=50 * MB, throughput_mbps=0.0) for i in range(3)]
        self.assertEqual(self.detector.detect(reports), [])
        for verdict in self.detector.evaluate(reports):
            self.assertIs(verdict.reason, StragglerReason.WITHIN_THRESHOLD)

    def test_threshold_is_strict(self) -> None:
        """Exactly at 2.5x is not a straggler; the predicate is a strict '>'."""
        reports = [
            worker("baseline", remaining=4 * MB, throughput_mbps=1.0),  # 4s
            worker("edge", remaining=10 * MB, throughput_mbps=1.0),     # 10s == 2.5 * 4
        ]
        verdict = self._verdict(reports, "edge")
        self.assertAlmostEqual(verdict.ttc_seconds, verdict.threshold_ttc_seconds)
        self.assertFalse(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.WITHIN_THRESHOLD)

    def test_just_past_threshold_is_flagged(self) -> None:
        reports = [
            worker("baseline", remaining=4 * MB, throughput_mbps=1.0),
            worker("edge", remaining=10 * MB + 1, throughput_mbps=1.0),
        ]
        verdict = self._verdict(reports, "edge")
        self.assertGreater(verdict.ttc_seconds, verdict.threshold_ttc_seconds)
        self.assertTrue(verdict.is_straggler)

    def test_small_remainder_is_not_worth_splitting(self) -> None:
        """
        A worker nearly done is slow but must be left alone.

        Bisecting leaves each half below the minimum split, so the new
        connection would cost more to establish than the bytes it saves.
        """
        detector = StragglerDetector(min_split_bytes=MB)
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),  # 1s
            worker("nearly_done", remaining=MB, throughput_mbps=0.01),   # 100s
        ]
        verdict = detector.verdicts_by_worker(reports)["nearly_done"]
        self.assertFalse(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.REMAINDER_TOO_SMALL)
        self.assertGreater(verdict.ratio, detector.factor)

    def test_remainder_exactly_at_floor_is_flagged(self) -> None:
        detector = StragglerDetector(min_split_bytes=MB)
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),
            worker("slow", remaining=2 * MB, throughput_mbps=0.01),
        ]
        verdict = detector.verdicts_by_worker(reports)["slow"]
        self.assertEqual(verdict.bytes_remaining, detector.min_remaining_bytes)
        self.assertTrue(verdict.is_straggler)

    def test_completed_worker_reports_complete(self) -> None:
        reports = [
            WorkerProgress("done", 0, 99, 100, 10.0 * MB),
            worker("active", remaining=50 * MB, throughput_mbps=10.0),
        ]
        verdict = self._verdict(reports, "done")
        self.assertFalse(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.COMPLETE)
        self.assertEqual(verdict.bytes_remaining, 0)

    def test_unprimed_worker_reports_unprimed(self) -> None:
        reports = [
            worker("unprimed", remaining=90 * MB, throughput_mbps=None),
            worker("active", remaining=10 * MB, throughput_mbps=10.0),
        ]
        verdict = self._verdict(reports, "unprimed")
        self.assertFalse(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.UNPRIMED)
        self.assertIsNone(verdict.ttc_seconds)

    def test_single_worker_has_no_baseline(self) -> None:
        reports = [worker("only", remaining=90 * MB, throughput_mbps=0.001)]
        verdict = self._verdict(reports, "only")
        self.assertFalse(verdict.is_straggler)
        self.assertIs(verdict.reason, StragglerReason.NO_BASELINE)
        self.assertIsNone(verdict.baseline_ttc_seconds)

    def test_finished_peer_does_not_poison_the_baseline(self) -> None:
        """
        Regression guard on the central design decision.

        Taking the literal min over all peers would use the finished worker's
        zero projection, drive the threshold to zero, and flag every remaining
        worker as a straggler.
        """
        reports = [
            WorkerProgress("finished", 0, 99, 100, 10.0 * MB),
            worker("b", remaining=10 * MB, throughput_mbps=10.0, start=1000),
            worker("c", remaining=20 * MB, throughput_mbps=10.0, start=10_000_000),
        ]
        self.assertEqual(self.detector.detect(reports), [])
        for worker_id in ("b", "c"):
            self.assertGreater(self._verdict(reports, worker_id).baseline_ttc_seconds, 0.0)

    def test_unprimed_peer_does_not_affect_other_verdicts(self) -> None:
        """
        An unmeasured worker must be inert, however much it still holds.

        Asserted by differencing rather than by absolute outcome: adding a
        large unprimed worker to a pool must leave every other worker's verdict
        byte-for-byte unchanged.
        """
        pool = [
            worker("a", remaining=10 * MB, throughput_mbps=10.0),
            worker("b", remaining=60 * MB, throughput_mbps=1.0),
        ]
        without = self.detector.verdicts_by_worker(pool)
        with_unprimed = self.detector.verdicts_by_worker(
            pool + [worker("unprimed", remaining=500 * MB, throughput_mbps=None)]
        )
        for worker_id in ("a", "b"):
            self.assertEqual(with_unprimed[worker_id], without[worker_id])
        self.assertFalse(with_unprimed["unprimed"].is_straggler)

    def test_empty_report_set(self) -> None:
        self.assertEqual(self.detector.evaluate([]), [])
        self.assertEqual(self.detector.detect([]), [])
        self.assertIsNone(self.detector.worst_straggler([]))

    def test_custom_factor_changes_sensitivity(self) -> None:
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),  # 1s
            worker("slow", remaining=20 * MB, throughput_mbps=10.0),     # 2s
        ]
        self.assertEqual(StragglerDetector(factor=2.5).detect(reports), [])
        self.assertEqual(len(StragglerDetector(factor=1.5).detect(reports)), 1)


class TestOrderingAndLookup(unittest.TestCase):
    """Ordering matters when idle workers are scarce."""

    def setUp(self) -> None:
        self.detector = StragglerDetector()

    def test_detect_orders_worst_first(self) -> None:
        """The worker projected to finish last sets the completion time."""
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),  # 1s
            worker("bad", remaining=20 * MB, throughput_mbps=1.0),       # 20s
            worker("worst", remaining=60 * MB, throughput_mbps=1.0),     # 60s
            worker("mild", remaining=10 * MB, throughput_mbps=1.0),      # 10s
        ]
        flagged = [v.worker_id for v in self.detector.detect(reports)]
        self.assertEqual(flagged, ["worst", "bad", "mild"])

    def test_stalled_worker_sorts_ahead_of_merely_slow(self) -> None:
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),
            worker("slow", remaining=60 * MB, throughput_mbps=1.0),
            worker("stalled", remaining=20 * MB, throughput_mbps=0.0),
        ]
        self.assertEqual(self.detector.detect(reports)[0].worker_id, "stalled")

    def test_worst_straggler_matches_first_detected(self) -> None:
        reports = [
            worker("healthy", remaining=10 * MB, throughput_mbps=10.0),
            worker("bad", remaining=20 * MB, throughput_mbps=1.0),
            worker("worst", remaining=60 * MB, throughput_mbps=1.0),
        ]
        self.assertEqual(self.detector.worst_straggler(reports).worker_id, "worst")

    def test_evaluate_preserves_input_order(self) -> None:
        ids = ["z", "a", "m"]
        reports = [worker(i, remaining=10 * MB, throughput_mbps=10.0) for i in ids]
        self.assertEqual([v.worker_id for v in self.detector.evaluate(reports)], ids)

    def test_verdicts_by_worker_covers_every_report(self) -> None:
        reports = [worker(f"w{i}", remaining=10 * MB, throughput_mbps=10.0) for i in range(4)]
        indexed = self.detector.verdicts_by_worker(reports)
        self.assertEqual(set(indexed), {"w0", "w1", "w2", "w3"})


class TestReportSetValidation(unittest.TestCase):
    """Duplicate identities would let a worker anchor its own comparison."""

    def test_duplicate_worker_id_rejected(self) -> None:
        reports = [
            worker("dup", remaining=10 * MB, throughput_mbps=10.0),
            worker("dup", remaining=90 * MB, throughput_mbps=1.0),
        ]
        with self.assertRaises(ConfigurationError):
            StragglerDetector().evaluate(reports)

    def test_non_progress_entry_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            StragglerDetector().evaluate([{"worker_id": "w1"}])  # type: ignore[list-item]

    def test_accepts_any_iterable(self) -> None:
        reports = (worker(f"w{i}", remaining=10 * MB, throughput_mbps=10.0) for i in range(3))
        self.assertEqual(len(StragglerDetector().evaluate(reports)), 3)


class TestStatelessness(unittest.TestCase):
    """The detector keeps no history, so verdicts must be reproducible."""

    def test_repeated_evaluation_is_identical(self) -> None:
        detector = StragglerDetector()
        reports = [
            worker("healthy", remaining=50 * MB, throughput_mbps=10.0),
            worker("slow", remaining=95 * MB, throughput_mbps=1.0),
        ]
        first = detector.evaluate(reports)
        for _ in range(5):
            detector.evaluate([worker("other", remaining=MB, throughput_mbps=0.001)])
        self.assertEqual(detector.evaluate(reports), first)

    def test_verdict_is_immutable(self) -> None:
        verdict = StragglerDetector().evaluate(
            [worker("w1", remaining=10 * MB, throughput_mbps=1.0)]
        )[0]
        with self.assertRaises(Exception):
            verdict.is_straggler = True  # type: ignore[misc]


if __name__ == "__main__":
    unittest.main()
