"""
Unit tests for cooperative in-flight range bisection in
src.algorithms.work_stealer. Verifies the midpoint formula, the disjointness
and completeness invariants, refusal of remainders too small to divide, epoch
planning against a pool of idle workers, and — in a multi-epoch transfer
simulation with one degraded worker — that no byte is ever downloaded twice and
that the tail the scheduler exists to remove actually shrinks.
"""

from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.algorithms.work_stealer import (
    DEFAULT_MIN_SPLIT_BYTES,
    BisectionTrigger,
    RangeBisection,
    StragglerDetector,
    WorkerProgress,
    WorkStealCoordinator,
    WorkStealPlan,
    bisect_range,
    can_bisect,
)
from src.exceptions import ConfigurationError
from src.models import ChunkSpec

MB = 1024 * 1024


def progress(
    worker_id: str,
    start: int,
    end: int,
    next_byte: int,
    throughput_mbps: Optional[float] = 1.0,
) -> WorkerProgress:
    """Build a progress report over an explicit inclusive range."""
    return WorkerProgress(
        worker_id=worker_id,
        range_start=start,
        range_end=end,
        next_byte=next_byte,
        throughput_bps=None if throughput_mbps is None else throughput_mbps * MB,
    )


class TestBisectionFormula(unittest.TestCase):
    """Mid = NextByte + floor(Remaining / 2), measured from the cursor."""

    def test_midpoint_is_measured_from_the_cursor(self) -> None:
        """
        The split point must be relative to delivered progress, not the range
        start — that is the only reason no byte is transferred twice.
        """
        report = progress("v", 0, 100 * MB - 1, 20 * MB)
        bisection = bisect_range(report)
        expected = 20 * MB + (80 * MB) // 2
        self.assertEqual(bisection.boundary, expected)
        self.assertEqual(bisection.boundary, 60 * MB)

    def test_untouched_range_splits_at_its_own_midpoint(self) -> None:
        report = progress("v", 0, 8 * MB - 1, 0)
        self.assertEqual(bisect_range(report).boundary, 4 * MB)

    def test_offset_range_splits_correctly(self) -> None:
        """A range not starting at zero must not leak its origin into the math."""
        report = progress("v", 100 * MB, 200 * MB - 1, 100 * MB)
        bisection = bisect_range(report)
        self.assertEqual(bisection.boundary, 150 * MB)
        self.assertEqual(bisection.victim_range, (100 * MB, 150 * MB - 1))
        self.assertEqual(bisection.thief_range, (150 * MB, 200 * MB - 1))

    def test_victim_truncates_to_one_below_the_boundary(self) -> None:
        bisection = bisect_range(progress("v", 0, 99 * MB, 0))
        self.assertEqual(bisection.victim_end, bisection.boundary - 1)
        self.assertEqual(bisection.thief_start, bisection.boundary)

    def test_thief_keeps_the_original_end(self) -> None:
        report = progress("v", 0, 77 * MB, 3 * MB)
        self.assertEqual(bisect_range(report).thief_end, 77 * MB)

    def test_thief_receives_the_odd_byte(self) -> None:
        """
        floor to the victim, ceil to the thief.

        The thief is the worker believed healthy, so the larger half is the
        allocation more likely to finish sooner.
        """
        report = progress("v", 0, 4 * MB, 0)
        self.assertEqual(report.bytes_remaining, 4 * MB + 1)
        bisection = bisect_range(report)
        self.assertEqual(bisection.victim_bytes_remaining, 2 * MB)
        self.assertEqual(bisection.thief_bytes, 2 * MB + 1)
        self.assertGreater(bisection.thief_bytes, bisection.victim_bytes_remaining)

    def test_even_remainder_splits_evenly(self) -> None:
        bisection = bisect_range(progress("v", 0, 8 * MB - 1, 0))
        self.assertEqual(bisection.victim_bytes_remaining, bisection.thief_bytes)

    def test_thief_id_is_recorded_when_supplied(self) -> None:
        bisection = bisect_range(progress("v", 0, 9 * MB, 0), thief_id="t1")
        self.assertEqual(bisection.victim_id, "v")
        self.assertEqual(bisection.thief_id, "t1")

    def test_thief_id_is_optional(self) -> None:
        self.assertIsNone(bisect_range(progress("v", 0, 9 * MB, 0)).thief_id)


class TestInvariants(unittest.TestCase):
    """
    The two properties the whole protocol rests on.

    A duplicate byte means two connections paying for the same payload and
    racing to write the same file offset; a gap means a range nobody downloads
    and a transfer that never completes.
    """

    def test_halves_are_disjoint_and_abutting(self) -> None:
        bisection = bisect_range(progress("v", 0, 100 * MB - 1, 20 * MB))
        self.assertEqual(bisection.victim_end + 1, bisection.thief_start)
        self.assertLess(bisection.victim_end, bisection.thief_start)

    def test_halves_reconstitute_the_original_range(self) -> None:
        bisection = bisect_range(progress("v", 0, 100 * MB - 1, 20 * MB))
        self.assertTrue(bisection.covers_original_range())
        self.assertEqual(bisection.victim_range[0], bisection.original_start)
        self.assertEqual(bisection.thief_range[1], bisection.original_end)

    def test_undelivered_bytes_are_conserved(self) -> None:
        """Nothing is created or lost: the halves sum to the remainder exactly."""
        bisection = bisect_range(progress("v", 0, 100 * MB - 1, 20 * MB))
        self.assertEqual(
            bisection.victim_bytes_remaining + bisection.thief_bytes,
            bisection.bytes_bisected,
        )
        self.assertEqual(bisection.bytes_bisected, 80 * MB)

    def test_boundary_never_reclaims_delivered_bytes(self) -> None:
        """
        The thief must never be handed a byte the victim already wrote.

        Swept across the full span of cursor positions, since this is the
        property that makes the protocol safe rather than merely correct.
        """
        end = 40 * MB
        for cursor in range(0, end, MB // 4):
            report = progress("v", 0, end, cursor)
            if not can_bisect(report):
                continue
            bisection = bisect_range(report)
            self.assertGreater(
                bisection.boundary,
                cursor,
                "boundary must lie strictly past the delivery cursor",
            )
            self.assertGreaterEqual(bisection.thief_start, report.next_byte)

    def test_invariants_hold_across_an_exhaustive_sweep(self) -> None:
        """Small ranges at a 1-byte floor, where off-by-ones surface."""
        for start in (0, 1, 7, 1000):
            for size in range(2, 40):
                end = start + size - 1
                for cursor in range(start, end + 1):
                    report = progress("v", start, end, cursor)
                    if not can_bisect(report, min_split_bytes=1):
                        continue
                    bisection = bisect_range(report, min_split_bytes=1)
                    self.assertTrue(
                        bisection.covers_original_range(),
                        f"coverage broke at start={start} size={size} cursor={cursor}",
                    )
                    self.assertGreater(bisection.boundary, cursor)
                    self.assertGreaterEqual(bisection.victim_bytes_remaining, 1)
                    self.assertGreaterEqual(bisection.thief_bytes, 1)

    def test_both_halves_clear_the_minimum_split(self) -> None:
        """The 2 * S_min guard exists precisely to guarantee this."""
        for min_split in (MB, 4 * MB, 512):
            report = progress("v", 0, 2 * min_split - 1, 0)
            bisection = bisect_range(report, min_split_bytes=min_split)
            self.assertGreaterEqual(bisection.victim_bytes_remaining, min_split)
            self.assertGreaterEqual(bisection.thief_bytes, min_split)


class TestRepeatedBisection(unittest.TestCase):
    """
    A worker may be split again in a later epoch as its cursor advances.

    Successive splits must keep partitioning the original extent, which is what
    lets the scheduler converge on a persistent straggler instead of splitting
    it once and giving up.
    """

    def test_successive_splits_keep_partitioning_the_original(self) -> None:
        original_start, original_end = 0, 64 * MB - 1
        victim_end = original_end
        cursor = 0
        segments: List[Tuple[int, int]] = []

        for _ in range(6):
            report = progress("v", original_start, victim_end, cursor)
            if not can_bisect(report):
                break
            bisection = bisect_range(report)
            segments.append(bisection.thief_range)
            victim_end = bisection.victim_end
            # The victim makes a little progress before the next epoch.
            cursor = min(cursor + MB, victim_end)

        segments.append((original_start, victim_end))
        segments.sort()

        self.assertEqual(segments[0][0], original_start)
        self.assertEqual(segments[-1][1], original_end)
        for earlier, later in zip(segments, segments[1:]):
            self.assertEqual(
                earlier[1] + 1, later[0], "successive splits left a gap or overlap"
            )
        self.assertEqual(
            sum(end - start + 1 for start, end in segments),
            original_end - original_start + 1,
        )

    def test_splitting_converges_on_the_minimum(self) -> None:
        """Halving must terminate, not shave slivers forever."""
        victim_end = 100 * MB
        splits = 0
        while True:
            report = progress("v", 0, victim_end, 0)
            if not can_bisect(report):
                break
            victim_end = bisect_range(report).victim_end
            splits += 1
            self.assertLess(splits, 100, "bisection failed to converge")
        self.assertGreater(splits, 0)
        self.assertLess(report.bytes_remaining, 2 * DEFAULT_MIN_SPLIT_BYTES)


class TestBisectionRefusal(unittest.TestCase):
    """Ranges too small to divide usefully must be refused, not split."""

    def test_complete_range_cannot_be_bisected(self) -> None:
        report = WorkerProgress("v", 0, 99, 100, 1000.0)
        self.assertFalse(can_bisect(report, min_split_bytes=1))
        with self.assertRaises(ConfigurationError):
            bisect_range(report, min_split_bytes=1)

    def test_remainder_below_twice_the_floor_is_refused(self) -> None:
        report = progress("v", 0, 2 * MB - 2, 0)
        self.assertEqual(report.bytes_remaining, 2 * MB - 1)
        self.assertFalse(can_bisect(report))
        with self.assertRaises(ConfigurationError):
            bisect_range(report)

    def test_remainder_exactly_at_twice_the_floor_is_accepted(self) -> None:
        report = progress("v", 0, 2 * MB - 1, 0)
        self.assertEqual(report.bytes_remaining, 2 * MB)
        self.assertTrue(can_bisect(report))
        self.assertTrue(bisect_range(report).covers_original_range())

    def test_single_remaining_byte_is_refused_even_at_the_lowest_floor(self) -> None:
        report = progress("v", 0, 10, 10)
        self.assertEqual(report.bytes_remaining, 1)
        self.assertFalse(can_bisect(report, min_split_bytes=1))

    def test_two_remaining_bytes_split_one_and_one(self) -> None:
        report = progress("v", 0, 10, 9)
        bisection = bisect_range(report, min_split_bytes=1)
        self.assertEqual(bisection.victim_bytes_remaining, 1)
        self.assertEqual(bisection.thief_bytes, 1)

    def test_invalid_min_split_rejected(self) -> None:
        report = progress("v", 0, 99 * MB, 0)
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                can_bisect(report, min_split_bytes=bad)
            with self.assertRaises(ConfigurationError):
                bisect_range(report, min_split_bytes=bad)


class TestRangeBisectionRecord(unittest.TestCase):
    """The record is the authoritative log of a reassignment."""

    def test_boundary_at_or_below_cursor_rejected(self) -> None:
        """Would hand the thief bytes the victim has already written."""
        for boundary in (50, 49, 0):
            with self.assertRaises(ConfigurationError):
                RangeBisection("v", "t", 0, 99, 50, boundary)

    def test_boundary_past_the_end_rejected(self) -> None:
        """Would leave the thief an empty range."""
        with self.assertRaises(ConfigurationError):
            RangeBisection("v", "t", 0, 99, 50, 100)

    def test_cursor_outside_the_original_range_rejected(self) -> None:
        for cursor in (-1, 100, 200):
            with self.assertRaises(ConfigurationError):
                RangeBisection("v", "t", 0, 99, cursor, 60)

    def test_minimal_valid_record(self) -> None:
        record = RangeBisection("v", "t", 0, 99, 50, 51)
        self.assertEqual(record.victim_range, (0, 50))
        self.assertEqual(record.thief_range, (51, 99))
        self.assertTrue(record.covers_original_range())

    def test_range_header_matches_the_thief_half(self) -> None:
        record = RangeBisection("v", "t", 0, 999, 100, 500)
        self.assertEqual(record.thief_range_header, "bytes=500-999")

    def test_chunk_spec_matches_the_thief_half(self) -> None:
        record = RangeBisection("v", "t", 0, 999, 100, 500)
        spec = record.to_chunk_spec(index=4)
        self.assertIsInstance(spec, ChunkSpec)
        self.assertEqual((spec.start_byte, spec.end_byte), (500, 999))
        self.assertEqual(spec.size, 500)
        self.assertEqual(spec.index, 4)

    def test_record_is_immutable(self) -> None:
        record = RangeBisection("v", "t", 0, 99, 50, 60)
        with self.assertRaises(Exception):
            record.boundary = 70  # type: ignore[misc]

    def test_repr_shows_both_halves(self) -> None:
        text = repr(RangeBisection("v", "t", 0, 99, 50, 60))
        self.assertIn("victim='v'", text)
        self.assertIn("thief='t'", text)
        self.assertIn("boundary=60", text)


class TestCoordinatorPlanning(unittest.TestCase):
    """One epoch: detect, pair with idle workers, bisect."""

    def setUp(self) -> None:
        self.coordinator = WorkStealCoordinator()

    def test_straggler_is_paired_with_an_idle_worker(self) -> None:
        reports = [
            progress("healthy", 0, 50 * MB, 40 * MB, 10.0),
            progress("slow", 100 * MB, 200 * MB, 105 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["idle1"])
        self.assertEqual(len(plan.bisections), 1)
        self.assertTrue(plan.has_work)
        bisection = plan.bisections[0]
        self.assertEqual(bisection.victim_id, "slow")
        self.assertEqual(bisection.thief_id, "idle1")
        self.assertTrue(bisection.covers_original_range())
        self.assertEqual(plan.idle_workers, ())

    def test_healthy_pool_flags_nobody_but_still_uses_idle_capacity(self) -> None:
        """
        No worker is slow here, yet leaving two workers idle would be waste.

        The splits are recorded as IDLE_CAPACITY rather than STRAGGLER, so the
        distinction stays visible in a scheduler log.
        """
        reports = [
            progress("w1", 0, 50 * MB, 10 * MB, 10.0),
            progress("w2", 51 * MB, 100 * MB, 60 * MB, 9.0),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["idle1", "idle2"])
        self.assertEqual(self.coordinator.detector.detect(reports), [])
        self.assertEqual(len(plan.bisections), 2)
        self.assertEqual(
            len(plan.bisections_by_trigger(BisectionTrigger.IDLE_CAPACITY)), 2
        )
        self.assertEqual(plan.bisections_by_trigger(BisectionTrigger.STRAGGLER), ())
        self.assertEqual(plan.idle_workers, ())

    def test_healthy_pool_yields_nothing_when_idle_capacity_is_disabled(self) -> None:
        """With the second trigger off, the coordinator is purely reactive."""
        coordinator = WorkStealCoordinator(use_idle_capacity=False)
        reports = [
            progress("w1", 0, 50 * MB, 10 * MB, 10.0),
            progress("w2", 51 * MB, 100 * MB, 60 * MB, 9.0),
        ]
        plan = coordinator.plan(reports, idle_worker_ids=["idle1", "idle2"])
        self.assertEqual(plan.bisections, ())
        self.assertFalse(plan.has_work)
        self.assertEqual(plan.idle_workers, ("idle1", "idle2"))
        self.assertEqual(plan.bytes_reassigned, 0)

    def test_worst_straggler_is_served_first(self) -> None:
        """With one thief to spend, it goes to the worker setting the tail."""
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("bad", 20 * MB, 60 * MB, 25 * MB, 1.0),
            progress("worst", 70 * MB, 300 * MB, 75 * MB, 1.0),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["idle1"])
        self.assertEqual(len(plan.bisections), 1)
        self.assertEqual(plan.bisections[0].victim_id, "worst")
        self.assertEqual(len(plan.unmatched_stragglers), 1)
        self.assertEqual(plan.unmatched_stragglers[0].worker_id, "bad")

    def test_stragglers_beyond_the_idle_supply_are_reported(self) -> None:
        """
        Shortfall is surfaced, not silently dropped.

        A scheduler that keeps finding stragglers with nobody to give them to is
        running too few workers, which is only visible if this is reported.
        """
        reports = [progress("healthy", 0, 10 * MB, 5 * MB, 10.0)] + [
            progress(f"slow{i}", (20 + 40 * i) * MB, (55 + 40 * i) * MB,
                     (21 + 40 * i) * MB, 0.5)
            for i in range(3)
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["idle1"])
        self.assertEqual(len(plan.bisections), 1)
        self.assertEqual(len(plan.unmatched_stragglers), 2)

    def test_no_idle_workers_leaves_every_straggler_unmatched(self) -> None:
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 200 * MB, 25 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports)
        self.assertEqual(plan.bisections, ())
        self.assertEqual(len(plan.unmatched_stragglers), 1)

    def test_each_victim_is_bisected_at_most_once_per_epoch(self) -> None:
        """
        Splitting one victim twice in an epoch would overlap the halves.

        The second boundary would be computed from an unmoved cursor against a
        range end the first split already gave away.
        """
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 400 * MB, 25 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["i1", "i2", "i3"])
        victims = [b.victim_id for b in plan.bisections]
        self.assertEqual(victims.count("slow"), 1)
        self.assertEqual(len(set(victims)), len(victims), "a victim was split twice")

    def test_multiple_stragglers_are_paired_in_order(self) -> None:
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("bad", 20 * MB, 60 * MB, 25 * MB, 1.0),
            progress("worst", 70 * MB, 300 * MB, 75 * MB, 1.0),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["i1", "i2"])
        self.assertEqual(
            [(b.victim_id, b.thief_id) for b in plan.bisections],
            [("worst", "i1"), ("bad", "i2")],
        )
        self.assertEqual(plan.unmatched_stragglers, ())

    def test_idle_worker_is_not_also_treated_as_a_victim(self) -> None:
        """
        A worker cannot both need relief and provide it.

        If it appears in both, the two observations were taken at different
        instants and the flag is stale.
        """
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 200 * MB, 25 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["slow"])
        self.assertNotIn("slow", [b.victim_id for b in plan.bisections])

    def test_bytes_reassigned_sums_the_thief_halves(self) -> None:
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 220 * MB, 20 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["i1"])
        self.assertEqual(plan.bytes_reassigned, plan.bisections[0].thief_bytes)
        self.assertGreater(plan.bytes_reassigned, 0)

    def test_completed_workers_in_reports_are_ignored_as_victims(self) -> None:
        reports = [
            WorkerProgress("done", 0, 10 * MB, 10 * MB + 1, 10.0 * MB),
            progress("healthy", 20 * MB, 30 * MB, 25 * MB, 10.0),
            progress("slow", 40 * MB, 240 * MB, 45 * MB, 0.5),
        ]
        plan = self.coordinator.plan(reports, idle_worker_ids=["done"])
        self.assertEqual(len(plan.bisections), 1)
        self.assertEqual(plan.bisections[0].victim_id, "slow")
        self.assertEqual(plan.bisections[0].thief_id, "done")

    def test_empty_inputs(self) -> None:
        plan = self.coordinator.plan([], [])
        self.assertEqual(plan.bisections, ())
        self.assertEqual(plan.unmatched_stragglers, ())
        self.assertEqual(plan.idle_workers, ())
        self.assertFalse(plan.has_work)

    def test_plan_is_repeatable(self) -> None:
        """Stateless across epochs: the same observations must plan the same."""
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 200 * MB, 25 * MB, 0.5),
        ]
        first = self.coordinator.plan(reports, ["i1"])
        for _ in range(3):
            self.coordinator.plan([progress("x", 0, 99 * MB, 0, 0.1)], ["i9"])
        self.assertEqual(self.coordinator.plan(reports, ["i1"]), first)

    def test_plan_result_is_immutable(self) -> None:
        plan = self.coordinator.plan([], [])
        with self.assertRaises(Exception):
            plan.bisections = ()  # type: ignore[misc]
        self.assertIsInstance(plan, WorkStealPlan)


class TestCoordinatorValidation(unittest.TestCase):
    """Configuration and input validation."""

    def test_duplicate_idle_identifier_rejected(self) -> None:
        """Would hand one worker two ranges while another sits idle."""
        with self.assertRaises(ConfigurationError):
            WorkStealCoordinator().plan([], ["i1", "i1"])

    def test_invalid_idle_identifier_rejected(self) -> None:
        for bad in ("", None, 5):
            with self.assertRaises(ConfigurationError):
                WorkStealCoordinator().plan([], [bad])  # type: ignore[list-item]

    def test_invalid_min_split_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                WorkStealCoordinator(min_split_bytes=bad)
        for bad_type in (1.5, "1024", True):
            with self.assertRaises(ConfigurationError):
                WorkStealCoordinator(min_split_bytes=bad_type)  # type: ignore[arg-type]

    def test_detector_floor_is_kept_consistent_by_default(self) -> None:
        coordinator = WorkStealCoordinator(min_split_bytes=4 * MB)
        self.assertEqual(coordinator.detector.min_split_bytes, 4 * MB)
        self.assertEqual(coordinator.min_split_bytes, 4 * MB)

    def test_supplied_detector_is_used(self) -> None:
        detector = StragglerDetector(factor=1.5)
        coordinator = WorkStealCoordinator(detector=detector)
        self.assertIs(coordinator.detector, detector)

    def test_slivers_are_refused_when_detector_floor_is_lower(self) -> None:
        """
        A detector with a smaller floor may flag a worker this coordinator will
        not split. It must be reported unmatched rather than cut into slivers.
        """
        coordinator = WorkStealCoordinator(
            detector=StragglerDetector(min_split_bytes=1024),
            min_split_bytes=8 * MB,
        )
        reports = [
            progress("healthy", 0, 10 * MB, 5 * MB, 10.0),
            progress("slow", 20 * MB, 20 * MB + 4096, 20 * MB, 0.001),
        ]
        plan = coordinator.plan(reports, idle_worker_ids=["i1"])
        self.assertEqual(plan.bisections, ())
        self.assertEqual(len(plan.unmatched_stragglers), 1)
        self.assertEqual(plan.idle_workers, ("i1",))

    def test_repr(self) -> None:
        text = repr(WorkStealCoordinator())
        self.assertIn("min_split_bytes=", text)
        self.assertIn("use_idle_capacity=True", text)


class TestIdleCapacityTrigger(unittest.TestCase):
    """
    The second trigger, which is what keeps the protocol alive at the tail.

    Straggler detection needs two active workers to call one of them slow. Once
    the healthy workers finish, the last worker cannot be flagged however far
    behind it is — exactly when the tail is longest and the idle pool largest.
    """

    def test_lone_active_worker_is_split_for_idle_capacity(self) -> None:
        lone = progress("w0", 0, 64 * MB - 1, 4 * MB, 0.5)
        coordinator = WorkStealCoordinator()

        self.assertEqual(
            coordinator.detector.detect([lone]), [], "no baseline exists to flag it"
        )

        plan = coordinator.plan([lone], idle_worker_ids=["w1", "w2", "w3"])
        self.assertEqual(len(plan.bisections), 1)
        bisection = plan.bisections[0]
        self.assertIs(bisection.trigger, BisectionTrigger.IDLE_CAPACITY)
        self.assertEqual(bisection.victim_id, "w0")
        self.assertTrue(bisection.covers_original_range())
        self.assertGreater(bisection.boundary, lone.next_byte)
        self.assertEqual(plan.idle_workers, ("w2", "w3"))

    def test_lone_active_worker_is_left_alone_when_disabled(self) -> None:
        lone = progress("w0", 0, 64 * MB - 1, 4 * MB, 0.5)
        plan = WorkStealCoordinator(use_idle_capacity=False).plan(
            [lone], idle_worker_ids=["w1", "w2", "w3"]
        )
        self.assertEqual(plan.bisections, ())
        self.assertEqual(plan.idle_workers, ("w1", "w2", "w3"))

    def test_stragglers_are_served_before_idle_capacity(self) -> None:
        """The tail comes first; spare capacity gets whatever is left."""
        reports = [
            progress("healthy", 0, 40 * MB, 20 * MB, 10.0),
            progress("slow", 50 * MB, 150 * MB, 55 * MB, 0.5),
        ]
        plan = WorkStealCoordinator().plan(reports, idle_worker_ids=["i1", "i2"])
        self.assertEqual(len(plan.bisections), 2)
        first, second = plan.bisections
        self.assertIs(first.trigger, BisectionTrigger.STRAGGLER)
        self.assertEqual(first.victim_id, "slow")
        self.assertEqual(first.thief_id, "i1")
        self.assertIs(second.trigger, BisectionTrigger.IDLE_CAPACITY)
        self.assertEqual(second.victim_id, "healthy")

    def test_idle_capacity_prefers_the_largest_remainder(self) -> None:
        reports = [
            progress("small", 0, 4 * MB, 0, 10.0),
            progress("large", 10 * MB, 210 * MB, 10 * MB, 10.0),
            progress("medium", 300 * MB, 350 * MB, 300 * MB, 10.0),
        ]
        plan = WorkStealCoordinator().plan(reports, idle_worker_ids=["i1"])
        self.assertEqual(len(plan.bisections), 1)
        self.assertEqual(plan.bisections[0].victim_id, "large")

    def test_idle_capacity_skips_ranges_below_the_floor(self) -> None:
        reports = [progress("tiny", 0, MB, 0, 10.0)]
        plan = WorkStealCoordinator().plan(reports, idle_worker_ids=["i1", "i2"])
        self.assertEqual(plan.bisections, ())
        self.assertEqual(plan.idle_workers, ("i1", "i2"))

    def test_default_trigger_is_straggler(self) -> None:
        self.assertIs(
            bisect_range(progress("v", 0, 9 * MB, 0)).trigger,
            BisectionTrigger.STRAGGLER,
        )
        self.assertIs(
            RangeBisection("v", "t", 0, 99, 50, 60).trigger,
            BisectionTrigger.STRAGGLER,
        )


@dataclass
class _Segment:
    """One contiguous assignment held by a worker during the simulation."""

    worker_id: str
    start: int
    end: int
    cursor: int
    throughput: float

    @property
    def is_complete(self) -> bool:
        return self.cursor > self.end

    @property
    def delivered(self) -> int:
        return self.cursor - self.start

    def advance(self, dt: float) -> None:
        if self.is_complete:
            return
        self.cursor = min(self.cursor + int(self.throughput * dt), self.end + 1)

    def report(self) -> WorkerProgress:
        return WorkerProgress(
            worker_id=self.worker_id,
            range_start=self.start,
            range_end=self.end,
            next_byte=self.cursor,
            throughput_bps=self.throughput,
        )


class TestDegradedWorkerSimulation(unittest.TestCase):
    """
    End-to-end proof of the two acceptance criteria over a whole transfer.

    A 256 MB file is split four ways. Three workers run at 10 MB/s and one at
    0.5 MB/s, which is the shape the scheduler was built for: without
    intervention the degraded worker alone dictates a completion time twenty
    times the others'.

    Every epoch asserts that the live segments still partition the file exactly,
    so a duplicate or a gap would be caught at the moment it was introduced
    rather than inferred from the total afterwards.
    """

    FILE_SIZE = 256 * MB
    WORKERS = 4
    HEALTHY_BPS = 10.0 * MB
    DEGRADED_BPS = 0.5 * MB
    DT = 0.25

    def _initial_segments(self) -> List[_Segment]:
        slice_size = self.FILE_SIZE // self.WORKERS
        segments = []
        for index in range(self.WORKERS):
            start = index * slice_size
            end = (start + slice_size - 1) if index < self.WORKERS - 1 else self.FILE_SIZE - 1
            segments.append(
                _Segment(
                    worker_id=f"w{index}",
                    start=start,
                    end=end,
                    cursor=start,
                    throughput=self.DEGRADED_BPS if index == 0 else self.HEALTHY_BPS,
                )
            )
        return segments

    def _assert_partitions_file(self, segments: List[_Segment]) -> None:
        ordered = sorted(segments, key=lambda s: s.start)
        self.assertEqual(ordered[0].start, 0, "file does not start at byte 0")
        self.assertEqual(
            ordered[-1].end, self.FILE_SIZE - 1, "file does not end at the last byte"
        )
        for earlier, later in zip(ordered, ordered[1:]):
            self.assertEqual(
                earlier.end + 1,
                later.start,
                f"segments [{earlier.start},{earlier.end}] and "
                f"[{later.start},{later.end}] overlap or leave a gap",
            )

    def _run(self, steal: bool) -> Tuple[float, List[_Segment], int]:
        """Run the transfer to completion, returning makespan and segments."""
        segments = self._initial_segments()
        throughput_of = {s.worker_id: s.throughput for s in segments}
        coordinator = WorkStealCoordinator()
        elapsed = 0.0
        reassignments = 0

        for _ in range(20_000):
            if all(segment.is_complete for segment in segments):
                break

            for segment in segments:
                segment.advance(self.DT)
            elapsed += self.DT

            self._assert_partitions_file(segments)

            if not steal:
                continue

            active = [s for s in segments if not s.is_complete]
            busy = {s.worker_id for s in active}
            idle = sorted({s.worker_id for s in segments} - busy)

            plan = coordinator.plan([s.report() for s in active], idle)
            by_id = {s.worker_id: s for s in active}
            for bisection in plan.bisections:
                victim = by_id[bisection.victim_id]
                # The victim stops short; the thief opens a fresh request.
                victim.end = bisection.victim_end
                segments.append(
                    _Segment(
                        worker_id=bisection.thief_id,
                        start=bisection.thief_start,
                        end=bisection.thief_end,
                        cursor=bisection.thief_start,
                        throughput=throughput_of[bisection.thief_id],
                    )
                )
                reassignments += 1

            self._assert_partitions_file(segments)
        else:
            self.fail("transfer did not complete within the step budget")

        return elapsed, segments, reassignments

    def test_no_byte_is_downloaded_twice(self) -> None:
        """
        Acceptance criterion: zero duplicate bytes.

        Summing what every segment actually delivered must equal the file size.
        Any byte fetched twice would push the total above it; any byte missed
        would leave the transfer incomplete.
        """
        _, segments, reassignments = self._run(steal=True)
        delivered = sum(segment.delivered for segment in segments)
        self.assertEqual(
            delivered,
            self.FILE_SIZE,
            "bytes delivered must equal the file size exactly",
        )
        self.assertGreater(reassignments, 0, "the degraded worker was never split")

    def test_completeness_invariant_holds_at_every_epoch(self) -> None:
        """Acceptance criterion: the segments partition the file throughout."""
        _, segments, _ = self._run(steal=True)
        self._assert_partitions_file(segments)
        for segment in segments:
            self.assertTrue(segment.is_complete)
            self.assertEqual(segment.delivered, segment.end - segment.start + 1)

    def test_tail_latency_is_substantially_reduced(self) -> None:
        """
        Epic #24's purpose, measured: the degraded worker no longer sets the
        completion time for the whole transfer.
        """
        without, _, _ = self._run(steal=False)
        with_stealing, _, reassignments = self._run(steal=True)
        self.assertLess(
            with_stealing,
            without / 3.0,
            f"stealing cut makespan only from {without:.1f}s to "
            f"{with_stealing:.1f}s over {reassignments} reassignments",
        )

    def test_stealing_does_not_change_the_bytes_transferred(self) -> None:
        """Work stealing must redistribute the payload, never inflate it."""
        _, baseline_segments, _ = self._run(steal=False)
        _, stolen_segments, _ = self._run(steal=True)
        self.assertEqual(
            sum(s.delivered for s in baseline_segments),
            sum(s.delivered for s in stolen_segments),
        )


if __name__ == "__main__":
    unittest.main()
