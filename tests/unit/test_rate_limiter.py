"""
Unit tests for the asynchronous token bucket in src.rate_limiter.

Throughput accuracy is measured on a virtual clock so the acceptance criterion
is checked exactly rather than against wall-clock noise, with a separate
real-event-loop test confirming the limiter behaves the same when actually
awaited. Also covers the reservation discipline that keeps concurrent callers
fair, capacity and refill behaviour, and the oversize request that would
otherwise wait forever.
"""

from __future__ import annotations

import asyncio
import threading
import time
import unittest
from typing import List, Tuple

from src.exceptions import ConfigurationError
from src.rate_limiter import (
    DEFAULT_BURST_SECONDS,
    BucketState,
    TokenBucketRateLimiter,
    UnlimitedRateLimiter,
)

KB = 1024
MB = 1024 * 1024


class VirtualClock:
    """
    A clock that advances only when a sleep is awaited.

    Makes pacing exactly measurable: elapsed virtual time is precisely the delay
    the limiter asked for, with none of the scheduling noise that makes a
    wall-clock assertion on a few hundred milliseconds flaky on shared CI.
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: List[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay
        # Yield so other tasks can run, as a real sleep would.
        await asyncio.sleep(0)


def limiter(rate_bps: float = MB, capacity_bytes: float = 64 * KB) -> Tuple[
    TokenBucketRateLimiter, VirtualClock
]:
    """Build a limiter driven by a virtual clock."""
    clock = VirtualClock()
    return (
        TokenBucketRateLimiter(
            rate_bps=rate_bps,
            capacity_bytes=capacity_bytes,
            clock=clock,
            sleep=clock.sleep,
        ),
        clock,
    )


class TestThroughputClamping(unittest.TestCase):
    """
    Acceptance criterion: sustained throughput within +/-3% of the target.

    Measured on the virtual clock, so the figure is the limiter's own pacing
    rather than a measurement of the test machine.
    """

    def _measure(
        self, rate_bps: float, capacity: float, chunk: int, chunks: int
    ) -> float:
        """Return the effective rate achieved over a run."""

        async def run() -> float:
            bucket, clock = limiter(rate_bps=rate_bps, capacity_bytes=capacity)
            for _ in range(chunks):
                await bucket.acquire(chunk)
            self.assertGreater(clock.now, 0.0, "no time passed; nothing was paced")
            return (chunk * chunks) / clock.now

        return asyncio.run(run())

    def test_within_three_percent_across_rates(self) -> None:
        for rate, capacity, chunk, chunks in (
            (1 * MB, 64 * KB, 64 * KB, 320),      # 20 MB at 1 MB/s
            (8 * MB, 1 * MB, 256 * KB, 320),      # 80 MB at 8 MB/s
            (512 * KB, 32 * KB, 16 * KB, 640),    # 10 MB at 512 KB/s
            (100 * MB, 4 * MB, 1 * MB, 400),      # 400 MB at 100 MB/s
        ):
            with self.subTest(rate=rate, chunk=chunk):
                effective = self._measure(rate, capacity, chunk, chunks)
                error = abs(effective - rate) / rate
                self.assertLess(
                    error,
                    0.03,
                    f"effective {effective:.0f} B/s vs target {rate} B/s "
                    f"({error * 100:.2f}% off)",
                )

    def test_accuracy_improves_over_a_longer_run(self) -> None:
        """
        The only systematic error is the initial full bucket.

        It is a fixed head start of one capacity, so its share of the total
        shrinks as the transfer grows — which is why the burst allowance is
        harmless for anything but a very short transfer.
        """
        short = abs(self._measure(MB, 64 * KB, 64 * KB, 20) - MB) / MB
        long = abs(self._measure(MB, 64 * KB, 64 * KB, 2000) - MB) / MB
        self.assertLess(long, short)
        self.assertLess(long, 0.005)

    def test_a_transfer_under_the_limit_is_never_delayed(self) -> None:
        async def run() -> None:
            bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            for _ in range(10):
                delay = await bucket.acquire(1 * KB)
                self.assertEqual(delay, 0.0)
                # Enough virtual time for the tokens to be replaced.
                clock.now += 1.0
            self.assertEqual(bucket.wait_count, 0)

        asyncio.run(run())


class TestReservationDiscipline(unittest.TestCase):
    """
    Callers reserve tokens and sleep once, rather than waking to compete.

    A retry loop would have every waiter wake on each refill, race for the same
    tokens, and mostly lose — collapsing throughput into a thundering herd and
    allowing a coroutine to be starved indefinitely.
    """

    def test_each_acquisition_sleeps_at_most_once(self) -> None:
        async def run() -> None:
            bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            for _ in range(50):
                await bucket.acquire(64 * KB)
            # 50 acquisitions, the first satisfied from the full bucket.
            self.assertLessEqual(len(clock.sleeps), 50)
            self.assertEqual(len(clock.sleeps), bucket.wait_count)

        asyncio.run(run())

    def test_concurrent_callers_are_served_in_arrival_order(self) -> None:
        """First come, first served: no caller can be starved by a neighbour."""

        async def run() -> List[int] :
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=16 * KB)
            completed: List[int] = []

            async def caller(index: int) -> None:
                await bucket.acquire(16 * KB)
                completed.append(index)

            # Created in order; each reserves at creation and sleeps its own share.
            await asyncio.gather(*(caller(i) for i in range(8)))
            return completed

        self.assertEqual(asyncio.run(run()), list(range(8)))

    def test_delay_grows_with_queue_position(self) -> None:
        """
        A later caller waits longer because more capacity is already promised.

        That monotonicity is what makes the queue fair rather than arbitrary.
        """

        async def run() -> List[float]:
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=16 * KB)
            return [await bucket.acquire(16 * KB) for _ in range(1)] + [
                bucket._reserve(16 * KB) for _ in range(4)
            ]

        delays = asyncio.run(run())
        reservations = delays[1:]
        for earlier, later in zip(reservations, reservations[1:]):
            self.assertGreater(later, earlier)

    def test_reserved_capacity_is_reported(self) -> None:
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=16 * KB)
        bucket._reserve(48 * KB)
        state = bucket.state()
        self.assertIsInstance(state, BucketState)
        self.assertTrue(state.is_saturated)
        self.assertAlmostEqual(state.reserved, 32 * KB, delta=1.0)
        self.assertEqual(state.fill_ratio, 0.0)

    def test_aggregate_rate_holds_under_concurrency(self) -> None:
        """Many concurrent callers must not exceed the configured rate."""

        async def run() -> float:
            bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)

            async def caller() -> None:
                for _ in range(10):
                    await bucket.acquire(32 * KB)

            await asyncio.gather(*(caller() for _ in range(16)))
            return (16 * 10 * 32 * KB) / clock.now

        effective = asyncio.run(run())
        self.assertLess(abs(effective - MB) / MB, 0.03)


class TestRefillBehaviour(unittest.TestCase):
    """Tokens accrue from elapsed time, with no background task."""

    def test_tokens_accrue_while_idle(self) -> None:
        bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        self.assertTrue(bucket.try_acquire(64 * KB))
        self.assertFalse(bucket.try_acquire(64 * KB))
        clock.now += 0.5  # half a second at 1 MB/s
        self.assertTrue(bucket.try_acquire(64 * KB))

    def test_idle_accrual_is_capped_at_capacity(self) -> None:
        """
        An hour of idling must not buy an hour of unthrottled transfer.

        Capacity is the whole point: it bounds how much unused allowance can be
        spent at once.
        """
        bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        bucket.try_acquire(64 * KB)
        clock.now += 3600.0
        self.assertTrue(bucket.try_acquire(64 * KB))
        self.assertFalse(bucket.try_acquire(1))

    def test_bucket_starts_full(self) -> None:
        """
        Starting empty would make a transfer wait for its first byte.

        That measures nothing useful and looks like a stall to whoever started
        it.
        """
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        self.assertEqual(bucket.state().tokens, 64 * KB)
        self.assertTrue(bucket.try_acquire(64 * KB))

    def test_backwards_clock_does_not_debit(self) -> None:
        """
        monotonic will not go backwards, but an injected clock might.

        Crediting a negative interval would silently penalize the next caller.
        """
        clock = VirtualClock()
        bucket = TokenBucketRateLimiter(
            rate_bps=MB, capacity_bytes=64 * KB, clock=clock, sleep=clock.sleep
        )
        bucket.try_acquire(32 * KB)
        before = bucket.state().tokens
        clock.now -= 10.0
        self.assertGreaterEqual(bucket.state().tokens, before)

    def test_default_capacity_is_one_second_of_rate(self) -> None:
        bucket = TokenBucketRateLimiter(rate_bps=2 * MB)
        self.assertEqual(bucket.capacity_bytes, 2 * MB * DEFAULT_BURST_SECONDS)


class TestOversizeRequests(unittest.TestCase):
    """
    A request larger than capacity could never be satisfied.

    The bucket never holds more than its capacity, so waiting for more would
    wait forever. Refusing loudly beats a download that silently hangs.
    """

    def test_oversize_acquire_is_refused(self) -> None:
        async def run() -> None:
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            with self.assertRaises(ConfigurationError) as caught:
                await bucket.acquire(1 * MB)
            self.assertIn("wait forever", str(caught.exception))

        asyncio.run(run())

    def test_oversize_try_acquire_is_refused(self) -> None:
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        with self.assertRaises(ConfigurationError):
            bucket.try_acquire(1 * MB)

    def test_exactly_capacity_is_allowed(self) -> None:
        async def run() -> None:
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            self.assertEqual(await bucket.acquire(64 * KB), 0.0)

        asyncio.run(run())

    def test_acquire_up_to_accepts_an_oversize_request(self) -> None:
        """The streaming path takes what is available instead of refusing."""

        async def run() -> None:
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            granted = await bucket.acquire_up_to(10 * MB)
            self.assertEqual(granted, 64 * KB)

        asyncio.run(run())

    def test_acquire_up_to_returns_zero_when_empty(self) -> None:
        async def run() -> None:
            bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            await bucket.acquire_up_to(64 * KB)
            self.assertEqual(await bucket.acquire_up_to(1 * KB), 0)

        asyncio.run(run())

    def test_acquire_up_to_never_waits(self) -> None:
        async def run() -> None:
            bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            for _ in range(20):
                await bucket.acquire_up_to(64 * KB)
            self.assertEqual(clock.sleeps, [])

        asyncio.run(run())


class TestValidation(unittest.TestCase):
    """Configuration and argument validation."""

    def test_invalid_rate_rejected(self) -> None:
        for bad in (0, -1, 0.5, float("inf"), float("nan")):
            with self.assertRaises(ConfigurationError):
                TokenBucketRateLimiter(rate_bps=bad)
        for bad_type in ("1000", None, True):
            with self.assertRaises(ConfigurationError):
                TokenBucketRateLimiter(rate_bps=bad_type)  # type: ignore[arg-type]

    def test_invalid_capacity_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                TokenBucketRateLimiter(rate_bps=MB, capacity_bytes=bad)
        for bad_type in ("1024", True):
            with self.assertRaises(ConfigurationError):
                TokenBucketRateLimiter(
                    rate_bps=MB, capacity_bytes=bad_type  # type: ignore[arg-type]
                )

    def test_invalid_amount_rejected(self) -> None:
        async def run() -> None:
            bucket, _ = limiter()
            for bad in (-1, -1024):
                with self.assertRaises(ConfigurationError):
                    await bucket.acquire(bad)
            for bad_type in (1.5, "1024", True, None):
                with self.assertRaises(ConfigurationError):
                    await bucket.acquire(bad_type)  # type: ignore[arg-type]

        asyncio.run(run())

    def test_zero_amount_is_a_no_op(self) -> None:
        async def run() -> None:
            bucket, clock = limiter()
            self.assertEqual(await bucket.acquire(0), 0.0)
            self.assertEqual(await bucket.acquire_up_to(0), 0)
            self.assertEqual(clock.sleeps, [])
            self.assertEqual(bucket.granted_bytes, 0)

        asyncio.run(run())

    def test_repr(self) -> None:
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        text = repr(bucket)
        self.assertIn("rate=1048576B/s", text)
        self.assertIn("capacity=65536B", text)


class TestRuntimeRateChange(unittest.TestCase):
    """Adaptive throttling changes the limit mid-transfer."""

    def test_rate_change_takes_effect(self) -> None:
        async def run() -> float:
            bucket, clock = limiter(rate_bps=MB, capacity_bytes=64 * KB)
            for _ in range(20):
                await bucket.acquire(64 * KB)
            bucket.update_rate(4 * MB)
            start = clock.now
            for _ in range(40):
                await bucket.acquire(64 * KB)
            return (40 * 64 * KB) / (clock.now - start)

        effective = asyncio.run(run())
        self.assertLess(abs(effective - 4 * MB) / (4 * MB), 0.05)

    def test_accrued_tokens_are_credited_at_the_old_rate(self) -> None:
        """
        Raising the limit must not retroactively grant a windfall.

        Nor should lowering it confiscate an allowance already earned.
        """
        bucket, clock = limiter(rate_bps=MB, capacity_bytes=1 * MB)
        bucket.try_acquire(1 * MB)
        clock.now += 0.5  # earns 512 KB at the old rate
        bucket.update_rate(100 * MB, capacity_bytes=100 * MB)
        self.assertAlmostEqual(bucket.state().tokens, 512 * KB, delta=1024)

    def test_capacity_shrinks_with_the_rate(self) -> None:
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=1 * MB)
        bucket.update_rate(100 * KB)
        self.assertEqual(bucket.capacity_bytes, 100 * KB * DEFAULT_BURST_SECONDS)
        self.assertLessEqual(bucket.state().tokens, bucket.capacity_bytes)

    def test_invalid_rate_change_rejected(self) -> None:
        bucket, _ = limiter()
        with self.assertRaises(ConfigurationError):
            bucket.update_rate(0)
        with self.assertRaises(ConfigurationError):
            bucket.update_rate(MB, capacity_bytes=-1)

    def test_reset_refills_and_clears_counters(self) -> None:
        bucket, _ = limiter(rate_bps=MB, capacity_bytes=64 * KB)
        bucket.try_acquire(64 * KB)
        bucket.reset()
        self.assertEqual(bucket.state().tokens, 64 * KB)
        self.assertEqual(bucket.granted_bytes, 0)
        self.assertEqual(bucket.wait_count, 0)


class TestRealEventLoop(unittest.TestCase):
    """
    The same behaviour when genuinely awaited, not just on a virtual clock.

    Tolerances are loose on purpose: this confirms the limiter integrates with
    asyncio and paces in the right direction, while the exact figure is the
    virtual-clock test's job. Asserting tight wall-clock bounds here would be a
    test of the machine's scheduler.
    """

    def test_pacing_delays_a_real_transfer(self) -> None:
        async def run() -> float:
            bucket = TokenBucketRateLimiter(rate_bps=512 * KB, capacity_bytes=16 * KB)
            started = time.monotonic()
            for _ in range(16):
                await bucket.acquire(16 * KB)
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        # 256 KB at 512 KB/s, less the 16 KB the full bucket covers ~= 0.47s.
        self.assertGreater(elapsed, 0.2)
        self.assertLess(elapsed, 2.0)

    def test_unthrottled_transfer_returns_promptly(self) -> None:
        async def run() -> float:
            bucket = TokenBucketRateLimiter(rate_bps=100 * MB, capacity_bytes=10 * MB)
            started = time.monotonic()
            for _ in range(50):
                await bucket.acquire(64 * KB)
            return time.monotonic() - started

        self.assertLess(asyncio.run(run()), 0.5)

    def test_concurrent_tasks_share_one_budget(self) -> None:
        async def run() -> int:
            bucket = TokenBucketRateLimiter(rate_bps=4 * MB, capacity_bytes=1 * MB)

            async def worker() -> None:
                for _ in range(8):
                    await bucket.acquire(64 * KB)

            await asyncio.gather(*(worker() for _ in range(8)))
            return bucket.granted_bytes

        self.assertEqual(asyncio.run(run()), 8 * 8 * 64 * KB)


class TestThreadSafety(unittest.TestCase):
    """
    The engine's worker pool spans threads, so the bucket must too.

    An asyncio lock would be cheaper but would not survive being touched from
    another thread.
    """

    def test_concurrent_try_acquire_never_overdraws(self) -> None:
        bucket = TokenBucketRateLimiter(
            rate_bps=MB, capacity_bytes=64 * KB, clock=lambda: 0.0
        )
        granted: List[int] = []
        guard = threading.Lock()
        barrier = threading.Barrier(8)

        def worker() -> None:
            local = 0
            barrier.wait(timeout=10)
            for _ in range(500):
                if bucket.try_acquire(1024):
                    local += 1
            with guard:
                granted.append(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        for thread in threads:
            self.assertFalse(thread.is_alive())

        # The clock never moves, so exactly the initial capacity is available.
        self.assertEqual(sum(granted) * 1024, 64 * KB)
        self.assertEqual(bucket.granted_bytes, 64 * KB)


class TestUnlimitedRateLimiter(unittest.TestCase):
    """
    A null object rather than a None the engine has to check for.

    An `if limiter is not None` at every read site is exactly the conditional
    that gets forgotten on one path, silently disabling throttling there.
    """

    def test_never_delays(self) -> None:
        async def run() -> None:
            bucket = UnlimitedRateLimiter()
            for _ in range(100):
                self.assertEqual(await bucket.acquire(100 * MB), 0.0)
            self.assertEqual(bucket.wait_count, 0)
            self.assertEqual(bucket.total_delay_seconds, 0.0)

        asyncio.run(run())

    def test_counts_bytes(self) -> None:
        async def run() -> None:
            bucket = UnlimitedRateLimiter()
            await bucket.acquire(1000)
            self.assertEqual(await bucket.acquire_up_to(500), 500)
            self.assertTrue(bucket.try_acquire(250))
            self.assertEqual(bucket.granted_bytes, 1750)
            bucket.reset()
            self.assertEqual(bucket.granted_bytes, 0)

        asyncio.run(run())

    def test_interface_matches_the_real_limiter(self) -> None:
        """Substitutable without the call site knowing which it holds."""
        real, _ = limiter()
        unlimited = UnlimitedRateLimiter()
        for name in (
            "acquire", "acquire_up_to", "try_acquire", "reset",
            "granted_bytes", "wait_count", "total_delay_seconds",
        ):
            self.assertTrue(hasattr(real, name), name)
            self.assertTrue(hasattr(unlimited, name), name)


if __name__ == "__main__":
    unittest.main()
