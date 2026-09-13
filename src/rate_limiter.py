"""
Asynchronous token bucket bandwidth limiter for ReliaDL.

Clamps aggregate transfer rate so a download does not saturate a shared link.
The bucket fills at a constant rate up to a fixed capacity, and each read
consumes tokens equal to the bytes it wants; a read that outruns the fill rate
waits for the shortfall to accrue.

Capacity and rate are separate knobs for a reason. The rate is the sustained
throughput the link is allowed; the capacity is how much of an unused
allowance may be spent at once. A bucket with capacity equal to one second of
rate lets a transfer idle briefly and then catch up, which is what keeps a
chunked download from being penalized for the gaps between its own requests.

Refilling without a timer
-------------------------
Tokens are computed from elapsed time whenever the bucket is consulted rather
than added by a background task. There is no timer to schedule, nothing to
cancel at shutdown, and no accumulated drift from a periodic wake-up that fires
late — which it will, since the event loop is busy moving bytes. An idle
limiter costs nothing at all.

Reservation rather than retry
-----------------------------
The obvious implementation waits until enough tokens exist, then takes them.
Under concurrency that is both unfair and wasteful: every waiter wakes on every
refill, races for the same tokens, and most lose and sleep again. Throughput
collapses into a thundering herd, and a coroutine can be starved indefinitely
by luckier neighbours.

This limiter instead *reserves*. A caller deducts its tokens immediately, even
if that drives the balance negative, and then sleeps for exactly as long as the
deficit takes to accrue:

    tokens -= amount
    delay   = max(0, -tokens) / rate

Each caller therefore sleeps once, for a computed duration, and wakes when its
own tokens exist rather than when someone else's might. Service is first come
first served in arrival order, no caller can be starved, and the aggregate rate
is exactly the fill rate because the deficit is bounded by what has already
been handed out.

The negative balance is not an overdraft in any dangerous sense. It represents
capacity already promised, and it is repaid by the passage of time before any
of those callers proceed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from src.exceptions import ConfigurationError

# Capacity, in seconds of sustained rate, used when none is configured. One
# second of allowance absorbs the gaps between chunk requests without letting a
# long idle period turn into an unbounded burst.
DEFAULT_BURST_SECONDS = 1.0

# Floor on a configured rate, in bytes per second. Below this a single chunk
# would take long enough that the limiter, rather than the network, is the
# thing being tested.
MIN_RATE_BPS = 1.0


def _validate_rate(value: float, parameter: str) -> float:
    """
    Validate a byte-per-second rate.

    Raises:
        ConfigurationError: If the rate is non-numeric, non-finite, or below the
            floor.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"{parameter} must be numeric, got {type(value).__name__}",
            parameter=parameter,
            value=value,
        )
    numeric = float(value)
    if numeric != numeric or numeric in (float("inf"), float("-inf")):
        raise ConfigurationError(
            f"{parameter} must be finite, got {value}",
            parameter=parameter,
            value=value,
        )
    if numeric < MIN_RATE_BPS:
        raise ConfigurationError(
            f"{parameter} must be at least {MIN_RATE_BPS} bytes/second, got {value}",
            parameter=parameter,
            value=value,
        )
    return numeric


@dataclass(frozen=True)
class BucketState:
    """
    A snapshot of the bucket, for observability and tests.

    Attributes:
        tokens: Current balance; negative means capacity already reserved.
        capacity: Maximum tokens the bucket holds.
        rate_bps: Fill rate in bytes per second.
        reserved: Tokens promised to waiting callers but not yet accrued.
    """

    tokens: float
    capacity: float
    rate_bps: float
    reserved: float

    @property
    def is_saturated(self) -> bool:
        """True when callers are queued waiting for tokens to accrue."""
        return self.reserved > 0.0

    @property
    def fill_ratio(self) -> float:
        """Fraction of capacity currently available, clamped to [0, 1]."""
        return max(0.0, min(1.0, self.tokens / self.capacity))


class TokenBucketRateLimiter:
    """
    Coroutine-safe token bucket with millisecond-resolution pacing.

    Safe to share across tasks and across threads. The critical section is a few
    arithmetic operations under a plain lock — never held across a sleep or any
    I/O — so it cannot block the event loop for a measurable time, and it works
    unchanged when a worker pool spans threads. An asyncio lock would be
    cheaper still but would not survive being touched from another thread, which
    the download engine's thread pool does.
    """

    def __init__(
        self,
        rate_bps: float,
        capacity_bytes: Optional[float] = None,
        clock: Optional[Callable[[], float]] = None,
        sleep: Optional[Callable[[float], Awaitable[None]]] = None,
    ) -> None:
        self._rate = _validate_rate(rate_bps, "rate_bps")

        if capacity_bytes is None:
            capacity = self._rate * DEFAULT_BURST_SECONDS
        else:
            if isinstance(capacity_bytes, bool) or not isinstance(
                capacity_bytes, (int, float)
            ):
                raise ConfigurationError(
                    "capacity_bytes must be numeric, got "
                    f"{type(capacity_bytes).__name__}",
                    parameter="capacity_bytes",
                    value=capacity_bytes,
                )
            capacity = float(capacity_bytes)
            if capacity <= 0.0:
                raise ConfigurationError(
                    f"capacity_bytes must be positive, got {capacity_bytes}",
                    parameter="capacity_bytes",
                    value=capacity_bytes,
                )
        self._capacity = capacity

        self._clock = clock if clock is not None else time.monotonic
        self._sleep = sleep if sleep is not None else asyncio.sleep
        # Start full: a transfer beginning against an empty bucket would wait
        # for its first byte, which measures nothing useful and looks like a
        # stall to whoever started it.
        self._tokens = capacity
        self._updated_at = self._clock()
        self._lock = threading.Lock()
        self._granted_bytes = 0
        self._waits = 0
        self._total_delay = 0.0

    @property
    def rate_bps(self) -> float:
        """Sustained fill rate in bytes per second."""
        return self._rate

    @property
    def capacity_bytes(self) -> float:
        """Maximum tokens the bucket holds."""
        return self._capacity

    @property
    def granted_bytes(self) -> int:
        """Total bytes the limiter has authorized."""
        return self._granted_bytes

    @property
    def wait_count(self) -> int:
        """Number of acquisitions that had to wait."""
        return self._waits

    @property
    def total_delay_seconds(self) -> float:
        """Aggregate time callers spent waiting on the limiter."""
        return self._total_delay

    def _refill_locked(self) -> None:
        """
        Credit tokens for time elapsed since the last consultation.

        A clock that appears to move backwards is treated as no elapsed time
        rather than as a debit. ``time.monotonic`` will not do that, but an
        injected clock might, and crediting a negative interval would silently
        penalize the next caller.
        """
        now = self._clock()
        elapsed = now - self._updated_at
        if elapsed > 0.0:
            self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated_at = now

    def _validate_amount(self, amount: int, allow_oversize: bool = False) -> int:
        """
        Validate a requested byte count against the bucket.

        Raises:
            ConfigurationError: If the amount is not a non-negative integer, or
                exceeds capacity when that cannot be satisfied.
        """
        if isinstance(amount, bool) or not isinstance(amount, int):
            raise ConfigurationError(
                f"amount must be an integer, got {type(amount).__name__}",
                parameter="amount",
                value=amount,
            )
        if amount < 0:
            raise ConfigurationError(
                f"amount must be non-negative, got {amount}",
                parameter="amount",
                value=amount,
            )
        if not allow_oversize and amount > self._capacity:
            raise ConfigurationError(
                f"amount ({amount}) exceeds the bucket capacity "
                f"({self._capacity:.0f}). The bucket never holds more than its "
                "capacity, so this request could never be satisfied and would "
                "wait forever; raise capacity_bytes to at least the largest "
                "single read, or use acquire_up_to() to consume it in pieces",
                parameter="amount",
                value=amount,
            )
        return amount

    def _reserve(self, amount: int) -> float:
        """
        Deduct tokens and return how long the caller must wait for them.

        The whole critical section: refill, deduct, compute the delay. No sleep
        happens here, so the lock is held only for arithmetic.
        """
        with self._lock:
            self._refill_locked()
            self._tokens -= amount
            self._granted_bytes += amount
            delay = 0.0 if self._tokens >= 0.0 else (-self._tokens) / self._rate
            if delay > 0.0:
                self._waits += 1
                self._total_delay += delay
            return delay

    async def acquire(self, amount: int) -> float:
        """
        Wait until ``amount`` bytes may be transferred, returning the delay.

        Raises:
            ConfigurationError: If the amount is invalid or exceeds capacity.
        """
        self._validate_amount(amount)
        if amount == 0:
            return 0.0
        delay = self._reserve(amount)
        if delay > 0.0:
            await self._sleep(delay)
        return delay

    async def acquire_up_to(self, amount: int) -> int:
        """
        Consume as much of ``amount`` as is immediately available, without waiting.

        For a streaming reader that would rather take a short read now than block
        for a full one. Returns zero when the bucket is empty, which the caller
        should treat as a signal to wait rather than to spin.

        Raises:
            ConfigurationError: If the amount is not a non-negative integer.
        """
        self._validate_amount(amount, allow_oversize=True)
        if amount == 0:
            return 0
        with self._lock:
            self._refill_locked()
            available = int(max(0.0, self._tokens))
            granted = min(amount, available)
            if granted:
                self._tokens -= granted
                self._granted_bytes += granted
            return granted

    def try_acquire(self, amount: int) -> bool:
        """
        Take tokens only if they are already available. Never waits.

        Synchronous, so a non-async caller can participate in the same budget.

        Raises:
            ConfigurationError: If the amount is invalid or exceeds capacity.
        """
        self._validate_amount(amount)
        with self._lock:
            self._refill_locked()
            if self._tokens < amount:
                return False
            self._tokens -= amount
            self._granted_bytes += amount
            return True

    def update_rate(self, rate_bps: float, capacity_bytes: Optional[float] = None) -> None:
        """
        Change the sustained rate at runtime.

        Tokens already accrued are credited at the old rate before the change
        takes effect, so raising or lowering the limit mid-transfer neither
        grants a windfall nor confiscates an allowance that was already earned.

        Raises:
            ConfigurationError: If the new rate or capacity is invalid.
        """
        new_rate = _validate_rate(rate_bps, "rate_bps")
        with self._lock:
            self._refill_locked()
            self._rate = new_rate
            if capacity_bytes is not None:
                if capacity_bytes <= 0:
                    raise ConfigurationError(
                        f"capacity_bytes must be positive, got {capacity_bytes}",
                        parameter="capacity_bytes",
                        value=capacity_bytes,
                    )
                self._capacity = float(capacity_bytes)
            else:
                self._capacity = new_rate * DEFAULT_BURST_SECONDS
            self._tokens = min(self._tokens, self._capacity)

    def state(self) -> BucketState:
        """Snapshot the bucket without consuming anything."""
        with self._lock:
            self._refill_locked()
            return BucketState(
                tokens=self._tokens,
                capacity=self._capacity,
                rate_bps=self._rate,
                reserved=max(0.0, -self._tokens),
            )

    def reset(self) -> None:
        """Refill the bucket and clear the counters."""
        with self._lock:
            self._tokens = self._capacity
            self._updated_at = self._clock()
            self._granted_bytes = 0
            self._waits = 0
            self._total_delay = 0.0

    def __repr__(self) -> str:
        return (
            f"TokenBucketRateLimiter(rate={self._rate:.0f}B/s, "
            f"capacity={self._capacity:.0f}B, granted={self._granted_bytes}B)"
        )


class UnlimitedRateLimiter:
    """
    A limiter that never delays, for transfers with no configured cap.

    A null object rather than a ``None`` the engine has to check for. The
    alternative is an ``if limiter is not None`` at every read site, which is
    exactly the sort of conditional that gets forgotten on one path and silently
    disables throttling there.
    """

    rate_bps = float("inf")
    capacity_bytes = float("inf")

    def __init__(self) -> None:
        self._granted_bytes = 0

    @property
    def granted_bytes(self) -> int:
        """Total bytes passed through."""
        return self._granted_bytes

    @property
    def wait_count(self) -> int:
        """Always zero; this limiter never waits."""
        return 0

    @property
    def total_delay_seconds(self) -> float:
        """Always zero; this limiter never waits."""
        return 0.0

    async def acquire(self, amount: int) -> float:
        """Grant immediately."""
        self._granted_bytes += max(0, amount)
        return 0.0

    async def acquire_up_to(self, amount: int) -> int:
        """Grant the whole amount immediately."""
        granted = max(0, amount)
        self._granted_bytes += granted
        return granted

    def try_acquire(self, amount: int) -> bool:
        """Always succeeds."""
        self._granted_bytes += max(0, amount)
        return True

    def reset(self) -> None:
        """Clear the byte counter."""
        self._granted_bytes = 0

    def __repr__(self) -> str:
        return f"UnlimitedRateLimiter(granted={self._granted_bytes}B)"
