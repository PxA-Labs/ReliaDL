"""
Unit tests for worker request dispatch integration in
src.algorithms.mirror_bandit. Verifies URL resolution, restricted selection over
available mirrors and the conditional probability it reports, the in-flight
request handle under out-of-order completion, failure and abandonment paths,
exhausted-pool handling, and end-to-end multi-mirror dispatch over a simulated
transfer with a slow mirror and a failing one.
"""

from __future__ import annotations

import math
import threading
import unittest
from typing import Dict, List, Optional

from src.algorithms.mirror_bandit import (
    ArmSelection,
    CircuitState,
    DispatchedRequest,
    DispatchOutcome,
    EXP3Bandit,
    MirrorDispatcher,
    MirrorEndpoint,
)
from src.exceptions import ConfigurationError
from src.models import ChunkSpec

MB = 1024 * 1024


class FakeClock:
    """A clock the test advances by hand, so timing needs no real waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


ENDPOINTS = [
    MirrorEndpoint("cdn-a", "https://a.example/file.iso"),
    MirrorEndpoint("cdn-b", "https://b.example/file.iso"),
    MirrorEndpoint("cdn-c", "https://c.example/file.iso"),
]


def dispatcher(clock: FakeClock, **kwargs) -> MirrorDispatcher:
    """Build a dispatcher over the standard endpoint set."""
    kwargs.setdefault("seed", 7)
    return MirrorDispatcher(ENDPOINTS, clock=clock, **kwargs)


def chunk(index: int = 0, start: int = 0, size: int = 8 * MB) -> ChunkSpec:
    """Build a chunk spec of a given size."""
    return ChunkSpec(index=index, start_byte=start, end_byte=start + size - 1)


class TestEndpoints(unittest.TestCase):
    """A mirror is an identifier plus a URL, deliberately kept separate."""

    def test_identifier_and_url_are_distinct(self) -> None:
        """
        A redirected edge or rotated hostname is still the same mirror.

        Restarting its learning because its address moved would discard exactly
        the evidence that makes routing work.
        """
        endpoint = MirrorEndpoint("cdn-a", "https://a.example/file.iso")
        self.assertEqual(endpoint.mirror_id, "cdn-a")
        self.assertEqual(endpoint.url, "https://a.example/file.iso")

    def test_from_url_uses_the_url_as_identifier(self) -> None:
        endpoint = MirrorEndpoint.from_url("https://a.example/file.iso")
        self.assertEqual(endpoint.mirror_id, endpoint.url)

    def test_invalid_endpoint_rejected(self) -> None:
        for mirror_id, url in (("", "https://a"), ("a", ""), (None, "https://a")):
            with self.assertRaises(ConfigurationError):
                MirrorEndpoint(mirror_id, url)  # type: ignore[arg-type]

    def test_endpoint_is_immutable(self) -> None:
        endpoint = MirrorEndpoint("cdn-a", "https://a.example/file.iso")
        with self.assertRaises(Exception):
            endpoint.url = "https://evil.example"  # type: ignore[misc]

    def test_two_mirrors_may_share_a_url(self) -> None:
        """Distinct identifiers over one host are legitimate and stay distinct."""
        pool = MirrorDispatcher(
            [
                MirrorEndpoint("primary", "https://a.example/f"),
                MirrorEndpoint("secondary", "https://a.example/f"),
            ],
            clock=FakeClock(),
        )
        self.assertEqual(pool.mirrors, ("primary", "secondary"))


class TestDispatcherConstruction(unittest.TestCase):
    """Pool validation, shared with the bandit's own arm checks."""

    def test_empty_pool_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MirrorDispatcher([])

    def test_duplicate_mirror_identifier_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MirrorDispatcher(
                [
                    MirrorEndpoint("dup", "https://a.example/f"),
                    MirrorEndpoint("dup", "https://b.example/f"),
                ]
            )

    def test_non_endpoint_entry_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            MirrorDispatcher(["https://a.example/f"])  # type: ignore[list-item]

    def test_single_mirror_pool_is_valid(self) -> None:
        pool = MirrorDispatcher([MirrorEndpoint("only", "https://a.example/f")],
                                clock=FakeClock())
        request = pool.dispatch(chunk())
        self.assertEqual(request.mirror, "only")
        self.assertAlmostEqual(request.selection.probability, 1.0, places=9)

    def test_components_are_exposed(self) -> None:
        pool = dispatcher(FakeClock())
        self.assertIsInstance(pool.bandit, EXP3Bandit)
        self.assertEqual(pool.bandit.arms, ("cdn-a", "cdn-b", "cdn-c"))
        self.assertEqual(pool.monitor.mirrors, ("cdn-a", "cdn-b", "cdn-c"))

    def test_repr(self) -> None:
        self.assertIn("mirrors=3", repr(dispatcher(FakeClock())))


class TestUrlResolution(unittest.TestCase):
    """Resolving the chosen mirror to the URL the worker will fetch."""

    def setUp(self) -> None:
        self.pool = dispatcher(FakeClock())

    def test_url_for_known_mirror(self) -> None:
        self.assertEqual(self.pool.url_for("cdn-b"), "https://b.example/file.iso")

    def test_url_for_unknown_mirror_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.url_for("ghost")

    def test_dispatched_request_carries_the_resolved_url(self) -> None:
        request = self.pool.dispatch(chunk())
        self.assertEqual(request.url, self.pool.url_for(request.mirror))

    def test_request_carries_the_range_header(self) -> None:
        request = self.pool.dispatch(chunk(index=3, start=16 * MB, size=4 * MB))
        self.assertEqual(
            request.range_header, f"bytes={16 * MB}-{20 * MB - 1}"
        )
        self.assertEqual(request.expected_bytes, 4 * MB)

    def test_chunk_is_optional(self) -> None:
        """A whole-file fetch has no range to express."""
        request = self.pool.dispatch()
        self.assertIsNone(request.chunk)
        self.assertIsNone(request.range_header)
        self.assertIsNone(request.expected_bytes)


class TestRestrictedSelection(unittest.TestCase):
    """
    A blacklisted mirror must never be drawn, and the recorded probability must
    be the conditional one the draw actually used.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.pool = dispatcher(self.clock, failure_threshold=2, cooldown_seconds=30.0)

    def _kill(self, mirror: str) -> None:
        for _ in range(2):
            request = self.pool.dispatch()
            while request.mirror != mirror:
                self.pool.abandon(request)
                request = self.pool.dispatch()
            self.pool.fail(request)

    def test_blacklisted_mirror_is_never_dispatched(self) -> None:
        self._kill("cdn-c")
        self.assertNotIn("cdn-c", self.pool.available_mirrors())
        for _ in range(500):
            request = self.pool.dispatch(chunk())
            self.assertNotEqual(request.mirror, "cdn-c")
            self.pool.abandon(request)

    def test_probability_is_conditional_on_availability(self) -> None:
        """
        r/p is unbiased only against the distribution that produced the action.

        With one of three mirrors blacklisted the draw is over two, so the
        reported probabilities must sum to 1 across the survivors, not across
        the whole pool.
        """
        self._kill("cdn-c")
        seen: Dict[str, float] = {}
        for _ in range(200):
            request = self.pool.dispatch()
            seen[request.mirror] = request.selection.probability
            self.pool.abandon(request)
        self.assertEqual(set(seen), {"cdn-a", "cdn-b"})
        self.assertAlmostEqual(sum(seen.values()), 1.0, places=9)

    def test_exclusion_steers_a_retry_elsewhere(self) -> None:
        """A chunk whose request just failed should be retried somewhere else."""
        for _ in range(200):
            request = self.pool.dispatch(chunk(), exclude=["cdn-a"])
            self.assertNotEqual(request.mirror, "cdn-a")
            self.pool.abandon(request)

    def test_exclusion_combines_with_blacklisting(self) -> None:
        self._kill("cdn-c")
        request = self.pool.dispatch(chunk(), exclude=["cdn-a"])
        self.assertEqual(request.mirror, "cdn-b")
        self.assertAlmostEqual(request.selection.probability, 1.0, places=9)

    def test_excluding_an_unknown_mirror_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.pool.dispatch(chunk(), exclude=["ghost"])

    def test_available_mirrors_reflects_exclusions(self) -> None:
        self.assertEqual(
            self.pool.available_mirrors(exclude=["cdn-b"]), ("cdn-a", "cdn-c")
        )


class TestExhaustedPool(unittest.TestCase):
    """
    Every mirror blacklisted is a wait condition, not a failed transfer.

    The breakers reopen on their own, so the dispatcher reports how long to
    wait instead of abandoning a transfer that will be servable shortly.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.pool = dispatcher(self.clock, failure_threshold=1, cooldown_seconds=30.0)

    def _kill_everything(self) -> None:
        for mirror in self.pool.mirrors:
            self.pool.monitor.record_failure(mirror)

    def test_dispatch_returns_none_when_no_mirror_is_available(self) -> None:
        self._kill_everything()
        self.assertEqual(self.pool.available_mirrors(), ())
        self.assertIsNone(self.pool.dispatch(chunk()))

    def test_retry_delay_is_reported(self) -> None:
        self._kill_everything()
        self.assertAlmostEqual(self.pool.seconds_until_retry, 30.0, places=9)
        self.clock.advance(10.0)
        self.assertAlmostEqual(self.pool.seconds_until_retry, 20.0, places=9)

    def test_dispatch_resumes_after_the_cooldown(self) -> None:
        self._kill_everything()
        self.clock.advance(30.0)
        self.assertEqual(self.pool.seconds_until_retry, 0.0)
        request = self.pool.dispatch(chunk())
        self.assertIsNotNone(request)

    def test_no_retry_delay_while_a_mirror_is_usable(self) -> None:
        self.assertEqual(self.pool.seconds_until_retry, 0.0)

    def test_exclusions_can_empty_the_pool_too(self) -> None:
        self.assertIsNone(
            self.pool.dispatch(chunk(), exclude=list(self.pool.mirrors))
        )


class TestRequestLifecycle(unittest.TestCase):
    """
    The handle is what makes concurrent dispatch correct.

    Many requests are outstanding at once and finish out of order, so the
    selection, mirror and start time travel with the request rather than living
    in the dispatcher.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.pool = dispatcher(self.clock, peak_decay=0.0)

    def test_request_ids_are_unique_and_monotonic(self) -> None:
        requests = [self.pool.dispatch(chunk(i)) for i in range(20)]
        ids = [request.request_id for request in requests]
        self.assertEqual(ids, list(range(20)))
        self.assertEqual(len(set(ids)), 20)

    def test_in_flight_count_tracks_outstanding_requests(self) -> None:
        first = self.pool.dispatch(chunk(0))
        second = self.pool.dispatch(chunk(1))
        self.assertEqual(self.pool.in_flight, 2)
        self.clock.advance(1.0)
        self.pool.complete(first, 8 * MB)
        self.assertEqual(self.pool.in_flight, 1)
        self.pool.fail(second)
        self.assertEqual(self.pool.in_flight, 0)

    def test_completion_uses_the_clock_when_no_duration_is_given(self) -> None:
        request = self.pool.dispatch(chunk())
        self.clock.advance(2.0)
        outcome = self.pool.complete(request, 8 * MB)
        self.assertAlmostEqual(outcome.duration_seconds, 2.0, places=9)
        self.assertAlmostEqual(outcome.throughput_bps, 4 * MB, places=3)

    def test_explicit_duration_overrides_the_clock(self) -> None:
        """
        A worker that timed the transfer itself can exclude its own queueing.

        Time the request spent waiting for a worker slot is not the mirror's
        doing and should not be charged to it.
        """
        request = self.pool.dispatch(chunk())
        self.clock.advance(10.0)
        outcome = self.pool.complete(request, 8 * MB, duration_seconds=1.0)
        self.assertAlmostEqual(outcome.duration_seconds, 1.0, places=9)
        self.assertAlmostEqual(outcome.throughput_bps, 8 * MB, places=3)

    def test_out_of_order_completion_scores_each_request_correctly(self) -> None:
        """The case the handle exists for: rewards arriving in a jumbled order."""
        slow = self.pool.dispatch(chunk(0))
        self.clock.advance(4.0)
        fast = self.pool.dispatch(chunk(1))
        self.clock.advance(1.0)

        fast_outcome = self.pool.complete(fast, 8 * MB)      # took 1s
        slow_outcome = self.pool.complete(slow, 8 * MB)      # took 5s

        self.assertAlmostEqual(fast_outcome.duration_seconds, 1.0, places=9)
        self.assertAlmostEqual(slow_outcome.duration_seconds, 5.0, places=9)
        self.assertGreater(
            fast_outcome.throughput_bps, slow_outcome.throughput_bps
        )

    def test_stale_selection_probability_is_preserved(self) -> None:
        """
        The completion must score against the draw's own probability.

        Many other requests land in between and move the distribution.
        """
        in_flight = self.pool.dispatch(chunk())
        original = in_flight.selection.probability

        for index in range(50):
            other = self.pool.dispatch(chunk(index + 1))
            self.clock.advance(0.1)
            self.pool.complete(other, 8 * MB)

        self.clock.advance(1.0)
        outcome = self.pool.complete(in_flight, 8 * MB)
        self.assertAlmostEqual(outcome.update.probability, original, places=12)

    def test_double_completion_rejected(self) -> None:
        """
        Applying one observation's reward twice is exactly the sort of bias that
        produces a plausible distribution converging on the wrong mirror.
        """
        request = self.pool.dispatch(chunk())
        self.clock.advance(1.0)
        self.pool.complete(request, 8 * MB)
        with self.assertRaises(ConfigurationError):
            self.pool.complete(request, 8 * MB)
        with self.assertRaises(ConfigurationError):
            self.pool.fail(request)

    def test_foreign_request_rejected(self) -> None:
        other_pool = dispatcher(self.clock)
        foreign = other_pool.dispatch(chunk())
        with self.assertRaises(ConfigurationError):
            self.pool.complete(foreign, 8 * MB)

    def test_zero_elapsed_time_does_not_divide_by_zero(self) -> None:
        """A coarse clock can report the same reading twice for a fast transfer."""
        request = self.pool.dispatch(chunk())
        outcome = self.pool.complete(request, 8 * MB)
        self.assertGreater(outcome.duration_seconds, 0.0)
        self.assertTrue(math.isfinite(outcome.throughput_bps))
        self.assertLessEqual(outcome.reward.reward, 1.0)

    def test_invalid_completion_arguments_rejected(self) -> None:
        request = self.pool.dispatch(chunk())
        with self.assertRaises(ConfigurationError):
            self.pool.complete(request, -1)
        self.assertEqual(self.pool.in_flight, 0, "a rejected report still retires")

    def test_outcome_is_immutable(self) -> None:
        request = self.pool.dispatch(chunk())
        self.clock.advance(1.0)
        outcome = self.pool.complete(request, 8 * MB)
        self.assertIsInstance(outcome, DispatchOutcome)
        with self.assertRaises(Exception):
            outcome.success = False  # type: ignore[misc]


class TestFailureAndAbandonment(unittest.TestCase):
    """Failures teach the router; cancellations must not."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.pool = dispatcher(self.clock, failure_threshold=2, peak_decay=0.0)

    def test_failure_scores_zero_and_updates_the_bandit(self) -> None:
        request = self.pool.dispatch(chunk())
        outcome = self.pool.fail(request)
        self.assertFalse(outcome.success)
        self.assertEqual(outcome.reward.reward, 0.0)
        self.assertEqual(outcome.bytes_transferred, 0)
        self.assertEqual(outcome.update.reward, 0.0)
        self.assertEqual(self.pool.bandit.rounds, 1)

    def test_failure_records_the_probability_the_draw_used(self) -> None:
        """
        The audit record must reflect the draw, not a later recomputation.

        For a failure this cannot change the weights — the reward is zero, so
        the delta is zero whatever the probability — which is exactly why it
        needs asserting directly. The record is the only place the difference
        is observable, and a transfer post-mortem reading it would otherwise be
        told a probability that never applied.
        """
        request = self.pool.dispatch(chunk())
        original = request.selection.probability

        for index in range(30):
            other = self.pool.dispatch(chunk(index + 1))
            self.clock.advance(0.5)
            self.pool.complete(other, 8 * MB)

        self.assertNotAlmostEqual(
            self.pool.bandit.probability(request.mirror), original, places=6
        )
        outcome = self.pool.fail(request)
        self.assertAlmostEqual(outcome.update.probability, original, places=12)
        self.assertEqual(outcome.update.log_weight_delta, 0.0)

    def test_repeated_failures_blacklist_the_mirror(self) -> None:
        for _ in range(20):
            request = self.pool.dispatch(chunk(), exclude=["cdn-a", "cdn-b"])
            if request is None:
                break
            self.pool.fail(request)
        self.assertNotIn("cdn-c", self.pool.available_mirrors())

    def test_abandonment_penalizes_nobody(self) -> None:
        """
        A range cancelled by the scheduler is not the mirror's fault.

        Work-stealing truncates in-flight ranges, and charging the mirror for a
        decision taken above it would blacklist healthy edges.
        """
        request = self.pool.dispatch(chunk())
        before_rounds = self.pool.bandit.rounds
        before_stats = self.pool.monitor.stats(request.mirror)

        self.pool.abandon(request)

        self.assertEqual(self.pool.bandit.rounds, before_rounds)
        after = self.pool.monitor.stats(request.mirror)
        self.assertEqual(after.successes, before_stats.successes)
        self.assertEqual(after.failures, before_stats.failures)
        self.assertEqual(self.pool.in_flight, 0)

    def test_abandoning_twice_rejected(self) -> None:
        request = self.pool.dispatch(chunk())
        self.pool.abandon(request)
        with self.assertRaises(ConfigurationError):
            self.pool.abandon(request)

    def test_outcome_reports_the_circuit_state(self) -> None:
        request = self.pool.dispatch(chunk(), exclude=["cdn-b", "cdn-c"])
        self.pool.fail(request)
        request = self.pool.dispatch(chunk(), exclude=["cdn-b", "cdn-c"])
        outcome = self.pool.fail(request)
        self.assertIs(outcome.circuit_state, CircuitState.OPEN)

    def test_stats_counters(self) -> None:
        first = self.pool.dispatch(chunk(0))
        second = self.pool.dispatch(chunk(1))
        third = self.pool.dispatch(chunk(2))
        self.clock.advance(1.0)
        self.pool.complete(first, 8 * MB)
        self.pool.fail(second)
        self.pool.abandon(third)

        stats = self.pool.stats()
        self.assertEqual(stats["dispatched"], 3)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["in_flight"], 0)
        self.assertAlmostEqual(sum(stats["probabilities"].values()), 1.0, places=9)


class TestEndToEndDispatch(unittest.TestCase):
    """
    Acceptance criterion: end-to-end multi-mirror dispatch.

    Runs the loop a download worker actually runs — dispatch, transfer, report —
    over a pool containing a fast mirror, a slow one, and one that is down, and
    checks that the routing that emerges is the one the transfer wanted.
    """

    RATES = {"cdn-a": 40 * MB, "cdn-b": 4 * MB}

    def _run_transfer(
        self,
        pool: MirrorDispatcher,
        clock: FakeClock,
        chunks: int,
        dead: Optional[str] = None,
    ) -> Dict[str, int]:
        """Drive chunks through the pool, returning per-mirror served counts."""
        served: Dict[str, int] = {m: 0 for m in pool.mirrors}
        index = 0
        while index < chunks:
            request = pool.dispatch(chunk(index, start=index * 8 * MB))
            if request is None:
                clock.advance(pool.seconds_until_retry)
                continue

            if dead is not None and request.mirror == dead:
                clock.advance(5.0)  # a connection timeout costs real time
                pool.fail(request)
                continue

            size = request.expected_bytes
            clock.advance(size / self.RATES[request.mirror])
            pool.complete(request, size)
            served[request.mirror] += 1
            index += 1
        return served

    def test_transfer_completes_and_prefers_the_faster_mirror(self) -> None:
        clock = FakeClock()
        pool = MirrorDispatcher(
            [
                MirrorEndpoint("cdn-a", "https://a.example/f"),
                MirrorEndpoint("cdn-b", "https://b.example/f"),
            ],
            weight_decay=0.0,
            clock=clock,
            seed=11,
        )
        served = self._run_transfer(pool, clock, chunks=600)

        self.assertEqual(sum(served.values()), 600)
        self.assertGreater(
            served["cdn-a"],
            served["cdn-b"] * 2,
            f"router did not favour the faster mirror: {served}",
        )
        self.assertEqual(pool.in_flight, 0)
        self.assertEqual(pool.stats()["completed"], 600)

    def test_every_chunk_is_served_exactly_once(self) -> None:
        """The dispatcher must route the transfer, not alter its contents."""
        clock = FakeClock()
        pool = dispatcher(clock, seed=12)
        delivered: List[int] = []
        index = 0
        while index < 200:
            request = pool.dispatch(chunk(index, start=index * 8 * MB))
            clock.advance(0.2)
            pool.complete(request, request.expected_bytes)
            delivered.append(request.chunk.index)
            index += 1
        self.assertEqual(delivered, list(range(200)))

    def test_a_dead_mirror_is_dropped_and_the_transfer_still_finishes(self) -> None:
        """
        The whole point of pairing the two mechanisms.

        The bandit alone would keep sending the dead mirror its eta/M share
        forever, each request costing a connection timeout; the breaker removes
        it, and the transfer completes on the survivors.
        """
        clock = FakeClock()
        pool = MirrorDispatcher(
            [
                MirrorEndpoint("cdn-a", "https://a.example/f"),
                MirrorEndpoint("cdn-b", "https://b.example/f"),
                MirrorEndpoint("cdn-dead", "https://dead.example/f"),
            ],
            failure_threshold=3,
            cooldown_seconds=600.0,
            clock=clock,
            seed=13,
        )
        served = self._run_transfer(pool, clock, chunks=400, dead="cdn-dead")

        self.assertEqual(sum(served.values()), 400)
        self.assertEqual(served["cdn-dead"], 0)
        self.assertNotIn("cdn-dead", pool.available_mirrors())
        self.assertLessEqual(
            pool.stats()["failed"],
            6,
            "a blacklisted mirror should stop absorbing requests quickly",
        )

    def test_a_recovered_mirror_is_used_again(self) -> None:
        """A cooldown that expires must actually bring the mirror back."""
        clock = FakeClock()
        pool = MirrorDispatcher(
            [
                MirrorEndpoint("cdn-a", "https://a.example/f"),
                MirrorEndpoint("cdn-b", "https://b.example/f"),
            ],
            failure_threshold=2,
            cooldown_seconds=30.0,
            clock=clock,
            seed=14,
        )
        for _ in range(2):
            pool.monitor.record_failure("cdn-b")
        self.assertEqual(pool.available_mirrors(), ("cdn-a",))

        clock.advance(30.0)
        used = set()
        for index in range(200):
            request = pool.dispatch(chunk(index))
            clock.advance(0.5)
            pool.complete(request, request.expected_bytes)
            used.add(request.mirror)
        self.assertIn("cdn-b", used)
        self.assertEqual(pool.available_mirrors(), ("cdn-a", "cdn-b"))

    def test_retry_on_another_mirror_after_a_failure(self) -> None:
        """The failover path a worker takes when a range request fails."""
        clock = FakeClock()
        pool = dispatcher(clock, seed=15)
        target = chunk(0)

        first = pool.dispatch(target)
        clock.advance(3.0)
        pool.fail(first)

        retry = pool.dispatch(target, exclude=[first.mirror])
        self.assertNotEqual(retry.mirror, first.mirror)
        self.assertEqual(retry.chunk, target)
        clock.advance(1.0)
        outcome = pool.complete(retry, target.size)
        self.assertTrue(outcome.success)

    def test_routing_survives_a_mirror_degrading_mid_transfer(self) -> None:
        """
        The non-stationary case, end to end.

        The fast mirror collapses halfway through and the router must move its
        traffic onto the one that is now quicker.
        """
        clock = FakeClock()
        pool = MirrorDispatcher(
            [
                MirrorEndpoint("cdn-a", "https://a.example/f"),
                MirrorEndpoint("cdn-b", "https://b.example/f"),
            ],
            weight_decay=0.01,
            clock=clock,
            seed=16,
        )
        rates = {"cdn-a": 40 * MB, "cdn-b": 4 * MB}
        second_half: Dict[str, int] = {"cdn-a": 0, "cdn-b": 0}

        for index in range(1200):
            if index == 600:
                rates = {"cdn-a": 2 * MB, "cdn-b": 40 * MB}
            request = pool.dispatch(chunk(index))
            clock.advance(request.expected_bytes / rates[request.mirror])
            pool.complete(request, request.expected_bytes)
            if index >= 900:
                second_half[request.mirror] += 1

        self.assertGreater(
            second_half["cdn-b"],
            second_half["cdn-a"],
            f"router stayed on the degraded mirror: {second_half}",
        )


class TestThreadSafety(unittest.TestCase):
    """Workers dispatch and report concurrently, and finish out of order."""

    def test_concurrent_dispatch_and_completion(self) -> None:
        pool = dispatcher(FakeClock(), peak_decay=0.0)
        errors: List[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                for step in range(300):
                    request = pool.dispatch(chunk(step))
                    if request is None:
                        continue
                    if step % 7 == 0:
                        pool.fail(request)
                    else:
                        pool.complete(
                            request, 8 * MB, duration_seconds=1.0 + index
                        )
            except BaseException as error:  # noqa: BLE001 - recorded and re-raised
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "a worker did not terminate")

        self.assertEqual(errors, [])
        stats = pool.stats()
        self.assertEqual(stats["in_flight"], 0)
        self.assertEqual(
            stats["completed"] + stats["failed"], stats["dispatched"]
        )
        self.assertAlmostEqual(sum(stats["probabilities"].values()), 1.0, places=9)

    def test_request_ids_stay_unique_under_contention(self) -> None:
        """A duplicated id would let one completion retire another's request."""
        pool = dispatcher(FakeClock())
        seen: List[int] = []
        guard = threading.Lock()

        def worker() -> None:
            local = []
            for _ in range(400):
                request = pool.dispatch(chunk())
                local.append(request.request_id)
                pool.abandon(request)
            with guard:
                seen.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(len(seen), len(set(seen)))


if __name__ == "__main__":
    unittest.main()
