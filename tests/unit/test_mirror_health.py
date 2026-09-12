"""
Unit tests for mirror health monitoring and reward calculation in
src.algorithms.mirror_bandit. Verifies the pool-wide normalized throughput
reward and its penalization of slow and errored mirrors, the three-state
circuit breaker including automatic recovery after the cooldown and its
backoff, peak decay against spurious outliers, and the end-to-end path from an
observed transfer to a bandit weight update.
"""

from __future__ import annotations

import math
import threading
import unittest
from typing import List

from src.algorithms.mirror_bandit import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_MIN_PEAK_SAMPLE_BYTES,
    CircuitBreaker,
    CircuitState,
    EXP3Bandit,
    MirrorHealthMonitor,
    MirrorReward,
    MirrorStats,
)
from src.exceptions import ConfigurationError

MB = 1024 * 1024
MIRRORS = ["fast", "slow", "dead"]


class FakeClock:
    """A clock the test advances by hand, so cooldowns need no real waiting."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def monitor(clock: FakeClock, **kwargs) -> MirrorHealthMonitor:
    """Build a monitor over the standard mirror set with an injected clock."""
    return MirrorHealthMonitor(MIRRORS, clock=clock, **kwargs)


class TestCircuitBreakerTripping(unittest.TestCase):
    """Consecutive failures remove a mirror; isolated ones do not."""

    def setUp(self) -> None:
        self.clock = FakeClock()

    def test_starts_closed_and_available(self) -> None:
        breaker = CircuitBreaker(clock=self.clock)
        self.assertIs(breaker.state, CircuitState.CLOSED)
        self.assertTrue(breaker.is_available)
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertEqual(breaker.trips, 0)
        self.assertEqual(breaker.seconds_until_retry, 0.0)

    def test_failures_below_threshold_keep_it_closed(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, clock=self.clock)
        for _ in range(2):
            breaker.record_failure()
        self.assertIs(breaker.state, CircuitState.CLOSED)
        self.assertTrue(breaker.is_available)

    def test_threshold_consecutive_failures_open_it(self) -> None:
        breaker = CircuitBreaker(failure_threshold=3, clock=self.clock)
        for _ in range(3):
            breaker.record_failure()
        self.assertIs(breaker.state, CircuitState.OPEN)
        self.assertFalse(breaker.is_available)
        self.assertEqual(breaker.trips, 1)

    def test_success_resets_the_failure_run(self) -> None:
        """
        Consecutive, not cumulative: a mirror failing one request in fifty is
        degraded but usable, while three in a row is a mirror that is down.
        """
        breaker = CircuitBreaker(failure_threshold=3, clock=self.clock)
        for _ in range(10):
            breaker.record_failure()
            breaker.record_failure()
            breaker.record_success()
        self.assertIs(breaker.state, CircuitState.CLOSED)
        self.assertEqual(breaker.consecutive_failures, 0)
        self.assertEqual(breaker.trips, 0)

    def test_threshold_of_one_opens_immediately(self) -> None:
        breaker = CircuitBreaker(failure_threshold=1, clock=self.clock)
        breaker.record_failure()
        self.assertIs(breaker.state, CircuitState.OPEN)

    def test_further_failures_while_open_do_not_extend_the_cooldown(self) -> None:
        """Only a failed *trial* backs the cooldown off, not queued failures."""
        breaker = CircuitBreaker(
            failure_threshold=2, cooldown_seconds=10.0, clock=self.clock
        )
        breaker.record_failure()
        breaker.record_failure()
        original = breaker.cooldown_seconds
        breaker.record_failure()
        self.assertEqual(breaker.cooldown_seconds, original)
        self.assertEqual(breaker.trips, 1)

    def test_seconds_until_retry_counts_down(self) -> None:
        breaker = CircuitBreaker(
            failure_threshold=1, cooldown_seconds=30.0, clock=self.clock
        )
        breaker.record_failure()
        self.assertAlmostEqual(breaker.seconds_until_retry, 30.0, places=9)
        self.clock.advance(20.0)
        self.assertAlmostEqual(breaker.seconds_until_retry, 10.0, places=9)
        self.clock.advance(10.0)
        self.assertEqual(breaker.seconds_until_retry, 0.0)


class TestCircuitBreakerRecovery(unittest.TestCase):
    """
    Automatic recovery after the cooldown, an acceptance criterion.

    A blacklisted mirror cannot be restored by evidence, because while it is
    blacklisted no requests reach it and no evidence can arrive. The half-open
    state breaks that deadlock.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = CircuitBreaker(
            failure_threshold=2, cooldown_seconds=30.0, clock=self.clock
        )
        self.breaker.record_failure()
        self.breaker.record_failure()

    def test_cooldown_promotes_to_half_open(self) -> None:
        self.assertIs(self.breaker.state, CircuitState.OPEN)
        self.clock.advance(29.9)
        self.assertIs(self.breaker.state, CircuitState.OPEN)
        self.clock.advance(0.1)
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)
        self.assertTrue(self.breaker.is_available)

    def test_promotion_needs_no_timer_to_fire(self) -> None:
        """State is derived from the clock, not stored by a scheduled event."""
        self.clock.advance(10_000.0)
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)

    def test_successful_trial_closes_the_breaker(self) -> None:
        self.clock.advance(30.0)
        self.assertIs(self.breaker.record_success(), CircuitState.CLOSED)
        self.assertIs(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.consecutive_failures, 0)

    def test_successful_trial_resets_the_backoff(self) -> None:
        """
        A recovered mirror must not keep serving a long cooldown.

        Holding the backed-off interval against it would punish it for an
        outage it has demonstrably come back from.
        """
        self.clock.advance(30.0)
        self.breaker.record_failure()  # failed trial: cooldown doubles to 60
        self.assertAlmostEqual(self.breaker.cooldown_seconds, 60.0, places=9)
        self.clock.advance(60.0)
        self.breaker.record_success()
        self.assertAlmostEqual(self.breaker.cooldown_seconds, 30.0, places=9)

    def test_failed_trial_reopens_immediately(self) -> None:
        """
        One failure is enough while half-open, regardless of the threshold.

        The trial existed to answer whether the mirror had recovered, and it
        answered no.
        """
        self.clock.advance(30.0)
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)
        self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.OPEN)

    def test_repeated_failed_trials_back_off_geometrically(self) -> None:
        expected = [60.0, 120.0, 240.0]
        for anticipated in expected:
            self.clock.advance(self.breaker.cooldown_seconds)
            self.breaker.record_failure()
            self.assertAlmostEqual(
                self.breaker.cooldown_seconds, anticipated, places=9
            )

    def test_backoff_is_capped(self) -> None:
        """
        Uncapped backoff would exceed any plausible transfer length.

        A mirror that recovered after a long outage would then never be
        reconsidered.
        """
        breaker = CircuitBreaker(
            failure_threshold=1,
            cooldown_seconds=10.0,
            backoff_multiplier=3.0,
            max_cooldown_seconds=100.0,
            clock=self.clock,
        )
        breaker.record_failure()
        for _ in range(10):
            self.clock.advance(breaker.cooldown_seconds)
            breaker.record_failure()
        self.assertAlmostEqual(breaker.cooldown_seconds, 100.0, places=9)

    def test_full_outage_and_recovery_cycle(self) -> None:
        """The whole lifecycle a transient mirror outage produces."""
        self.assertIs(self.breaker.state, CircuitState.OPEN)
        self.clock.advance(30.0)
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)
        self.breaker.record_failure()
        self.assertIs(self.breaker.state, CircuitState.OPEN)
        self.clock.advance(60.0)
        self.assertIs(self.breaker.state, CircuitState.HALF_OPEN)
        self.breaker.record_success()
        self.assertIs(self.breaker.state, CircuitState.CLOSED)
        self.assertTrue(self.breaker.is_available)

    def test_reset_returns_to_initial_state(self) -> None:
        self.breaker.reset()
        self.assertIs(self.breaker.state, CircuitState.CLOSED)
        self.assertEqual(self.breaker.trips, 0)
        self.assertAlmostEqual(self.breaker.cooldown_seconds, 30.0, places=9)


class TestCircuitBreakerValidation(unittest.TestCase):
    """Configuration validation."""

    def test_invalid_threshold_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                CircuitBreaker(failure_threshold=bad)
        for bad_type in (1.5, "3", True):
            with self.assertRaises(ConfigurationError):
                CircuitBreaker(failure_threshold=bad_type)  # type: ignore[arg-type]

    def test_invalid_cooldown_rejected(self) -> None:
        for bad in (0.0, -1.0, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                CircuitBreaker(cooldown_seconds=bad)
        with self.assertRaises(ConfigurationError):
            CircuitBreaker(cooldown_seconds="30")  # type: ignore[arg-type]

    def test_invalid_backoff_rejected(self) -> None:
        """A multiplier below 1 would shorten the cooldown on repeated failure."""
        for bad in (0.9, 0.0, -1.0, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                CircuitBreaker(backoff_multiplier=bad)

    def test_backoff_of_exactly_one_is_valid(self) -> None:
        breaker = CircuitBreaker(backoff_multiplier=1.0, clock=FakeClock())
        self.assertEqual(breaker.cooldown_seconds, DEFAULT_COOLDOWN_SECONDS)

    def test_max_cooldown_below_base_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            CircuitBreaker(cooldown_seconds=60.0, max_cooldown_seconds=30.0)

    def test_a_long_cooldown_alone_raises_its_own_cap(self) -> None:
        """
        A caller who sets only a long cooldown means it.

        Requiring them to discover a separate ceiling before their own setting
        becomes legal is friction with no benefit, so the cap defaults relative
        to the base rather than to a fixed constant. An explicitly conflicting
        pair is still rejected by the test above.
        """
        breaker = CircuitBreaker(
            failure_threshold=1, cooldown_seconds=600.0, clock=FakeClock()
        )
        self.assertAlmostEqual(breaker.cooldown_seconds, 600.0, places=9)
        MirrorHealthMonitor(MIRRORS, cooldown_seconds=600.0)

    def test_repr(self) -> None:
        self.assertIn("state=CLOSED", repr(CircuitBreaker()))

    def test_defaults_are_exposed(self) -> None:
        breaker = CircuitBreaker()
        self.assertEqual(breaker.failure_threshold, DEFAULT_FAILURE_THRESHOLD)
        self.assertEqual(breaker.cooldown_seconds, DEFAULT_COOLDOWN_SECONDS)


class TestRewardCalculation(unittest.TestCase):
    """
    r = (Throughput / PeakThroughput) * (1 - error_flag).

    Acceptance criterion: the reward must penalize slow and errored mirrors.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor(self.clock, peak_decay=0.0)

    def test_first_observation_sets_the_peak_and_scores_one(self) -> None:
        reward = self.monitor.record_transfer("fast", 8 * MB, 1.0)
        self.assertAlmostEqual(reward.reward, 1.0, places=9)
        self.assertAlmostEqual(reward.throughput_bps, 8 * MB, places=3)
        self.assertFalse(reward.errored)

    def test_reward_is_proportional_to_throughput(self) -> None:
        self.monitor.record_transfer("fast", 8 * MB, 1.0)
        half = self.monitor.record_transfer("slow", 8 * MB, 2.0)
        quarter = self.monitor.record_transfer("slow", 8 * MB, 4.0)
        self.assertAlmostEqual(half.reward, 0.5, places=6)
        self.assertAlmostEqual(quarter.reward, 0.25, places=6)

    def test_slower_mirror_always_scores_lower(self) -> None:
        self.monitor.record_transfer("fast", 64 * MB, 1.0)
        previous = 1.1
        for duration in (1.0, 2.0, 4.0, 8.0, 16.0):
            reward = self.monitor.record_transfer("slow", 64 * MB, duration)
            self.assertLess(reward.reward, previous)
            previous = reward.reward

    def test_errored_request_scores_zero(self) -> None:
        self.monitor.record_transfer("fast", 8 * MB, 1.0)
        reward = self.monitor.record_failure("dead")
        self.assertEqual(reward.reward, 0.0)
        self.assertTrue(reward.errored)
        self.assertEqual(reward.throughput_bps, 0.0)

    def test_record_transfer_with_success_false_is_a_failure(self) -> None:
        reward = self.monitor.record_transfer("dead", 8 * MB, 1.0, success=False)
        self.assertTrue(reward.errored)
        self.assertEqual(reward.reward, 0.0)
        self.assertEqual(self.monitor.stats("dead").failures, 1)

    def test_reward_always_lies_in_the_unit_interval(self) -> None:
        """EXP3's weight bound rests entirely on the reward being at most 1."""
        for bytes_moved, duration in (
            (1, 1.0),
            (64 * MB, 0.001),
            (1, 100.0),
            (512 * MB, 0.5),
            (0, 1.0),
        ):
            reward = self.monitor.record_transfer("fast", bytes_moved, duration)
            self.assertGreaterEqual(reward.reward, 0.0)
            self.assertLessEqual(reward.reward, 1.0)

    def test_zero_bytes_is_not_an_error_but_scores_zero(self) -> None:
        """A successful response that delivered nothing is worth nothing."""
        self.monitor.record_transfer("fast", 8 * MB, 1.0)
        reward = self.monitor.record_transfer("slow", 0, 1.0)
        self.assertEqual(reward.reward, 0.0)
        self.assertFalse(reward.errored)
        self.assertEqual(self.monitor.stats("slow").successes, 1)

    def test_peak_is_shared_across_the_pool(self) -> None:
        """
        The central design decision, asserted directly.

        Normalizing each mirror against its own historical best would score
        every mirror near 1 whenever it performed typically for itself, making a
        uniformly mediocre edge indistinguishable from a fast one. Rewards must
        be comparable between arms or the bandit is comparing nothing.
        """
        self.monitor.record_transfer("fast", 100 * MB, 1.0)
        reward = self.monitor.record_transfer("slow", 10 * MB, 1.0)
        self.assertAlmostEqual(reward.reward, 0.1, places=6)
        self.assertAlmostEqual(reward.peak_bps, 100 * MB, places=3)

        # A slow mirror repeating its own best still scores low.
        repeat = self.monitor.record_transfer("slow", 10 * MB, 1.0)
        self.assertAlmostEqual(repeat.reward, 0.1, places=6)

    def test_a_faster_mirror_raises_the_bar_for_everyone(self) -> None:
        self.monitor.record_transfer("slow", 10 * MB, 1.0)
        before = self.monitor.record_transfer("slow", 10 * MB, 1.0).reward
        self.monitor.record_transfer("fast", 100 * MB, 1.0)
        after = self.monitor.record_transfer("slow", 10 * MB, 1.0).reward
        self.assertAlmostEqual(before, 1.0, places=6)
        self.assertLess(after, 0.2)

    def test_unknown_mirror_rejected(self) -> None:
        for call in (
            lambda: self.monitor.record_transfer("ghost", 1, 1.0),
            lambda: self.monitor.record_failure("ghost"),
            lambda: self.monitor.stats("ghost"),
            lambda: self.monitor.is_available("ghost"),
        ):
            with self.assertRaises(ConfigurationError):
                call()

    def test_invalid_byte_count_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            self.monitor.record_transfer("fast", -1, 1.0)
        for bad in (1.5, "100", True):
            with self.assertRaises(ConfigurationError):
                self.monitor.record_transfer("fast", bad, 1.0)  # type: ignore[arg-type]

    def test_invalid_duration_rejected(self) -> None:
        """Zero duration would divide by zero; a negative one is nonsense."""
        for bad in (0.0, -1.0, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                self.monitor.record_transfer("fast", 8 * MB, bad)


class TestPeakDecay(unittest.TestCase):
    """
    A spurious peak must fade rather than poison every later reward.

    One range served from cache, or one whose duration rounds toward the
    clock's resolution, can register an apparent rate orders of magnitude above
    anything achievable.
    """

    def setUp(self) -> None:
        self.clock = FakeClock()

    def test_outlier_peak_decays_and_rewards_recover(self) -> None:
        pool = monitor(self.clock, peak_decay=0.01)
        pool.record_transfer("fast", MB, 0.001)  # ~1 GB/s, not reproducible
        depressed = pool.record_transfer("slow", MB, 1.0).reward
        self.assertLess(depressed, 0.01)

        for _ in range(800):
            recovered = pool.record_transfer("slow", MB, 1.0).reward
        self.assertGreater(
            recovered, 0.9, "a stale outlier kept suppressing every reward"
        )

    def test_a_sustained_peak_is_continuously_re_established(self) -> None:
        """Decay costs a genuinely fast mirror nothing while it keeps performing."""
        pool = monitor(self.clock, peak_decay=0.01)
        for _ in range(200):
            reward = pool.record_transfer("fast", 8 * MB, 1.0)
        self.assertAlmostEqual(reward.reward, 1.0, places=6)
        self.assertAlmostEqual(pool.peak_throughput_bps, 8 * MB, delta=1000)

    def test_short_samples_cannot_set_the_peak(self) -> None:
        """
        Short transfers are dominated by setup cost and timer granularity.

        Their apparent rate is unreliable in both directions, so they are scored
        but never allowed to define the scale.
        """
        pool = monitor(self.clock, peak_decay=0.0, min_peak_sample_bytes=64 * 1024)
        pool.record_transfer("slow", MB, 1.0)
        peak_before = pool.peak_throughput_bps
        pool.record_transfer("fast", 1024, 0.0000001)  # absurd apparent rate
        self.assertEqual(pool.peak_throughput_bps, peak_before)

    def test_large_samples_may_set_the_peak(self) -> None:
        pool = monitor(self.clock, peak_decay=0.0)
        pool.record_transfer("slow", MB, 1.0)
        pool.record_transfer("fast", 10 * MB, 1.0)
        self.assertAlmostEqual(pool.peak_throughput_bps, 10 * MB, places=3)

    def test_zero_decay_keeps_the_peak_forever(self) -> None:
        pool = monitor(self.clock, peak_decay=0.0)
        pool.record_transfer("fast", 100 * MB, 1.0)
        for _ in range(500):
            reward = pool.record_transfer("slow", MB, 1.0)
        self.assertAlmostEqual(pool.peak_throughput_bps, 100 * MB, places=3)
        self.assertLess(reward.reward, 0.02)

    def test_reward_is_clamped_when_throughput_outruns_the_peak(self) -> None:
        """
        Two paths let an observation exceed the peak it is scored against, and
        both must clamp rather than hand the bandit a reward above 1.

        The decay lowers the peak just before the comparison, so a mirror
        simply repeating its own best rate divides by a slightly smaller
        number. And a sample too short to set the peak is still scored, so an
        absurd apparent rate would otherwise sail straight through. EXP3's
        bound on a single weight update rests on the reward being at most 1.
        """
        decayed = monitor(self.clock, peak_decay=0.05)
        decayed.record_transfer("fast", 8 * MB, 1.0)
        repeat = decayed.record_transfer("fast", 8 * MB, 1.0)
        self.assertGreater(repeat.throughput_bps, repeat.peak_bps * 0.99)
        self.assertEqual(repeat.reward, 1.0)

        guarded = monitor(self.clock, peak_decay=0.0, min_peak_sample_bytes=64 * 1024)
        guarded.record_transfer("slow", MB, 1.0)
        spike = guarded.record_transfer("fast", 1024, 0.0000001)
        self.assertGreater(spike.throughput_bps, spike.peak_bps)
        self.assertEqual(spike.reward, 1.0)

    def test_invalid_decay_and_sample_floor_rejected(self) -> None:
        for bad in (-0.1, 1.1, math.nan):
            with self.assertRaises(ConfigurationError):
                MirrorHealthMonitor(MIRRORS, peak_decay=bad)
        with self.assertRaises(ConfigurationError):
            MirrorHealthMonitor(MIRRORS, min_peak_sample_bytes=-1)
        with self.assertRaises(ConfigurationError):
            MirrorHealthMonitor(
                MIRRORS, min_peak_sample_bytes=1.5  # type: ignore[arg-type]
            )

    def test_default_sample_floor_is_exposed(self) -> None:
        self.assertEqual(DEFAULT_MIN_PEAK_SAMPLE_BYTES, 64 * 1024)


class TestPoolAvailability(unittest.TestCase):
    """Which mirrors the dispatcher may currently use."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor(self.clock, failure_threshold=2, cooldown_seconds=30.0)

    def _kill(self, mirror: str) -> None:
        for _ in range(2):
            self.monitor.record_failure(mirror)

    def test_all_mirrors_available_initially(self) -> None:
        self.assertEqual(self.monitor.available_mirrors(), tuple(MIRRORS))
        self.assertEqual(self.monitor.seconds_until_any_available(), 0.0)

    def test_blacklisted_mirror_is_excluded(self) -> None:
        self._kill("dead")
        self.assertEqual(self.monitor.available_mirrors(), ("fast", "slow"))
        self.assertFalse(self.monitor.is_available("dead"))

    def test_mirror_returns_after_the_cooldown(self) -> None:
        self._kill("dead")
        self.clock.advance(30.0)
        self.assertIn("dead", self.monitor.available_mirrors())

    def test_fully_blacklisted_pool_reports_empty(self) -> None:
        """
        The caller must handle this by waiting, not by failing the transfer.

        The breakers reopen on their own, so an empty pool is temporary.
        """
        for mirror in MIRRORS:
            self._kill(mirror)
        self.assertEqual(self.monitor.available_mirrors(), ())

    def test_time_until_any_mirror_returns(self) -> None:
        """Lets a stalled dispatcher sleep exactly as long as needed."""
        for mirror in MIRRORS:
            self._kill(mirror)
        self.clock.advance(10.0)
        self.assertAlmostEqual(
            self.monitor.seconds_until_any_available(), 20.0, places=9
        )
        self.clock.advance(20.0)
        self.assertEqual(self.monitor.seconds_until_any_available(), 0.0)
        self.assertNotEqual(self.monitor.available_mirrors(), ())

    def test_soonest_cooldown_is_reported(self) -> None:
        self._kill("fast")
        self.clock.advance(10.0)
        self._kill("slow")
        self._kill("dead")
        self.assertAlmostEqual(
            self.monitor.seconds_until_any_available(), 20.0, places=9
        )

    def test_ordering_follows_the_configured_pool(self) -> None:
        self._kill("slow")
        self.assertEqual(self.monitor.available_mirrors(), ("fast", "dead"))


class TestStats(unittest.TestCase):
    """Health summaries drive operator dashboards and dispatcher decisions."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.monitor = monitor(self.clock, peak_decay=0.0)

    def test_counts_accumulate(self) -> None:
        for _ in range(3):
            self.monitor.record_transfer("fast", 8 * MB, 1.0)
        self.monitor.record_failure("fast")
        stats = self.monitor.stats("fast")
        self.assertIsInstance(stats, MirrorStats)
        self.assertEqual(stats.successes, 3)
        self.assertEqual(stats.failures, 1)
        self.assertEqual(stats.total_requests, 4)
        self.assertAlmostEqual(stats.success_ratio, 0.75, places=9)

    def test_fresh_mirror_reports_a_perfect_ratio(self) -> None:
        """No evidence of failure is not evidence of failure."""
        stats = self.monitor.stats("fast")
        self.assertEqual(stats.total_requests, 0)
        self.assertEqual(stats.success_ratio, 1.0)
        self.assertTrue(stats.is_available)

    def test_last_observation_is_retained(self) -> None:
        self.monitor.record_transfer("fast", 8 * MB, 1.0)
        self.monitor.record_transfer("slow", 2 * MB, 1.0)
        stats = self.monitor.stats("slow")
        self.assertAlmostEqual(stats.last_throughput_bps, 2 * MB, places=3)
        self.assertAlmostEqual(stats.last_reward, 0.25, places=6)

    def test_failure_clears_the_last_reward(self) -> None:
        self.monitor.record_transfer("fast", 8 * MB, 1.0)
        self.monitor.record_failure("fast")
        self.assertEqual(self.monitor.stats("fast").last_reward, 0.0)

    def test_all_stats_covers_every_mirror(self) -> None:
        self.assertEqual(set(self.monitor.all_stats()), set(MIRRORS))

    def test_stats_reflect_the_breaker(self) -> None:
        for _ in range(DEFAULT_FAILURE_THRESHOLD):
            self.monitor.record_failure("dead")
        stats = self.monitor.stats("dead")
        self.assertIs(stats.state, CircuitState.OPEN)
        self.assertFalse(stats.is_available)
        self.assertEqual(stats.trips, 1)
        self.assertGreater(stats.seconds_until_retry, 0.0)

    def test_reset_clears_history_and_peak(self) -> None:
        self.monitor.record_transfer("fast", 100 * MB, 1.0)
        for _ in range(5):
            self.monitor.record_failure("dead")
        self.monitor.reset()
        self.assertEqual(self.monitor.peak_throughput_bps, 0.0)
        self.assertEqual(self.monitor.available_mirrors(), tuple(MIRRORS))
        for stats in self.monitor.all_stats().values():
            self.assertEqual(stats.total_requests, 0)

    def test_reward_record_is_immutable(self) -> None:
        reward = self.monitor.record_transfer("fast", 8 * MB, 1.0)
        self.assertIsInstance(reward, MirrorReward)
        with self.assertRaises(Exception):
            reward.reward = 0.1  # type: ignore[misc]

    def test_repr(self) -> None:
        text = repr(self.monitor)
        self.assertIn("mirrors=3", text)
        self.assertIn("available=3", text)


class TestRewardsDriveTheBandit(unittest.TestCase):
    """
    End to end: an observed transfer becomes a weight update.

    The reward calculator exists to feed EXP3, so the two are exercised
    together. Full request dispatch is a separate concern.
    """

    def test_bandit_learns_to_prefer_the_faster_mirror(self) -> None:
        clock = FakeClock()
        pool = MirrorHealthMonitor(["fast", "slow"], clock=clock, peak_decay=0.0)
        bandit = EXP3Bandit(["fast", "slow"], weight_decay=0.0, seed=3)
        rates = {"fast": 40 * MB, "slow": 4 * MB}

        for _ in range(1500):
            selection = bandit.select()
            moved = 4 * MB
            duration = moved / rates[selection.arm]
            reward = pool.record_transfer(selection.arm, moved, duration)
            bandit.update(selection, reward.reward)

        self.assertEqual(bandit.best_arm(), "fast")
        probabilities = bandit.probabilities()
        self.assertGreater(probabilities["fast"], probabilities["slow"])
        self.assertGreater(probabilities["fast"], 0.7)

    def test_rewards_are_always_acceptable_to_the_bandit(self) -> None:
        """
        Any reward the monitor produces must satisfy the bandit's [0, 1] bound.

        The bandit rejects anything outside it, so a mismatch here would surface
        as a crash mid-transfer.
        """
        clock = FakeClock()
        pool = monitor(clock, peak_decay=0.01)
        bandit = EXP3Bandit(MIRRORS, seed=4)
        cases = [
            ("fast", 64 * MB, 0.5),
            ("slow", 1, 10.0),
            ("fast", MB, 0.0001),
            ("slow", 0, 1.0),
            ("dead", 8 * MB, 2.0),
        ]
        for mirror, moved, duration in cases * 20:
            reward = pool.record_transfer(mirror, moved, duration)
            bandit.update_arm(mirror, reward.reward, bandit.probability(mirror))
        self.assertAlmostEqual(sum(bandit.probabilities().values()), 1.0, places=9)

    def test_a_failing_mirror_is_blacklisted_then_recovers(self) -> None:
        """The two mechanisms together over one transient outage."""
        clock = FakeClock()
        pool = MirrorHealthMonitor(
            ["a", "b"], failure_threshold=3, cooldown_seconds=30.0, clock=clock
        )
        for _ in range(3):
            pool.record_failure("b")
        self.assertEqual(pool.available_mirrors(), ("a",))

        clock.advance(30.0)
        self.assertIn("b", pool.available_mirrors())
        reward = pool.record_transfer("b", 8 * MB, 1.0)
        self.assertIs(reward.circuit_state, CircuitState.CLOSED)
        self.assertGreater(reward.reward, 0.0)
        self.assertEqual(pool.available_mirrors(), ("a", "b"))


class TestThreadSafety(unittest.TestCase):
    """Workers report completions concurrently and share the pool-wide peak."""

    def test_concurrent_reporting_keeps_counts_exact(self) -> None:
        pool = MirrorHealthMonitor(MIRRORS, peak_decay=0.0, clock=FakeClock())
        errors: List[BaseException] = []
        barrier = threading.Barrier(6)

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=10)
                for step in range(500):
                    mirror = MIRRORS[step % len(MIRRORS)]
                    reward = pool.record_transfer(mirror, 8 * MB, 1.0 + index)
                    if not 0.0 <= reward.reward <= 1.0:
                        raise AssertionError(f"reward out of range: {reward.reward}")
            except BaseException as error:  # noqa: BLE001 - recorded and re-raised
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive())

        self.assertEqual(errors, [])
        total = sum(s.successes for s in pool.all_stats().values())
        self.assertEqual(total, 6 * 500)


if __name__ == "__main__":
    unittest.main()
