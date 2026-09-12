"""
Real-time network metrics collection and filtering subsystem for ReliaDL.

Ingests per-transfer socket observations (byte counts, wall-clock duration,
response header latency) and maintains the smoothed network state vector
consumed by the BL-DCS adaptive chunk sizing controller:

    s_k = (RTT_k, sigma_RTT_k, mu_k, p_k, BDP_k)

RTT and throughput are filtered with exponentially weighted moving averages;
the empirical drop probability is estimated over a bounded sliding window of
recent transfer outcomes so the controller tracks non-stationary paths rather
than the lifetime average.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Optional

from src.exceptions import ConfigurationError

# EWMA smoothing factor for round-trip time (higher = faster adaptation).
DEFAULT_RTT_ALPHA = 0.2

# EWMA smoothing factor for observed path throughput.
DEFAULT_THROUGHPUT_BETA = 0.3

# Smoothing factor for RTT mean deviation, following RFC 6298 RTTVAR.
DEFAULT_DEVIATION_ALPHA = 0.25

# Number of recent transfer outcomes retained for drop probability estimation.
DEFAULT_FAILURE_WINDOW_SIZE = 50


def _validate_smoothing_factor(value: float, parameter: str) -> float:
    """
    Validate that an EWMA smoothing factor lies in the open interval (0, 1].

    Raises:
        ConfigurationError: If the factor is non-numeric or out of range.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"Smoothing factor must be numeric, got {type(value).__name__}",
            parameter=parameter,
            value=value,
        )
    if not 0.0 < float(value) <= 1.0:
        raise ConfigurationError(
            f"Smoothing factor must lie in (0, 1], got {value}",
            parameter=parameter,
            value=value,
        )
    return float(value)


class EWMAEstimator:
    """
    Exponentially weighted moving average filter with RFC 6298 style deviation.

    The first observation seeds the estimator directly rather than decaying from
    zero, which avoids the cold-start bias that would otherwise drive the chunk
    sizing controller toward its lower clamp during the opening transfers.
    """

    __slots__ = ("_alpha", "_deviation_alpha", "_value", "_deviation", "_count")

    def __init__(
        self,
        alpha: float = DEFAULT_RTT_ALPHA,
        deviation_alpha: float = DEFAULT_DEVIATION_ALPHA,
    ) -> None:
        self._alpha = _validate_smoothing_factor(alpha, "alpha")
        self._deviation_alpha = _validate_smoothing_factor(
            deviation_alpha, "deviation_alpha"
        )
        self._value: Optional[float] = None
        self._deviation: float = 0.0
        self._count: int = 0

    @property
    def alpha(self) -> float:
        """Smoothing factor applied to each new observation."""
        return self._alpha

    @property
    def value(self) -> Optional[float]:
        """Current smoothed estimate, or None before the first observation."""
        return self._value

    @property
    def deviation(self) -> float:
        """Smoothed mean absolute deviation of observations from the estimate."""
        return self._deviation

    @property
    def count(self) -> int:
        """Number of observations absorbed by the filter."""
        return self._count

    @property
    def is_initialized(self) -> bool:
        """True once at least one observation has been recorded."""
        return self._value is not None

    def update(self, sample: float) -> float:
        """
        Absorb a new observation and return the updated smoothed estimate.

        Raises:
            ConfigurationError: If the sample is non-numeric or non-finite.
        """
        if isinstance(sample, bool) or not isinstance(sample, (int, float)):
            raise ConfigurationError(
                f"Sample must be numeric, got {type(sample).__name__}",
                parameter="sample",
                value=sample,
            )
        observation = float(sample)
        if observation != observation or observation in (
            float("inf"),
            float("-inf"),
        ):
            raise ConfigurationError(
                f"Sample must be finite, got {sample}",
                parameter="sample",
                value=sample,
            )

        if self._value is None:
            self._value = observation
            self._deviation = 0.0
        else:
            # Deviation is measured against the prior estimate, before the
            # estimate absorbs this observation (RFC 6298 ordering).
            error = abs(observation - self._value)
            self._deviation = (
                1.0 - self._deviation_alpha
            ) * self._deviation + self._deviation_alpha * error
            self._value = (
                1.0 - self._alpha
            ) * self._value + self._alpha * observation

        self._count += 1
        return self._value

    def reset(self) -> None:
        """Discard all accumulated state and return to the uninitialized filter."""
        self._value = None
        self._deviation = 0.0
        self._count = 0

    def __repr__(self) -> str:
        return (
            f"EWMAEstimator(alpha={self._alpha}, value={self._value}, "
            f"deviation={self._deviation:.6f}, count={self._count})"
        )


class FailureWindow:
    """
    Bounded sliding window of recent transfer outcomes.

    Retains the last ``size`` boolean results and reports the observed failure
    ratio, giving the controller a drop probability that decays out of scope as
    conditions change instead of averaging over the whole session.
    """

    __slots__ = ("_window", "_size", "_failures", "_total_recorded")

    def __init__(self, size: int = DEFAULT_FAILURE_WINDOW_SIZE) -> None:
        if isinstance(size, bool) or not isinstance(size, int):
            raise ConfigurationError(
                f"Window size must be an integer, got {type(size).__name__}",
                parameter="size",
                value=size,
            )
        if size < 1:
            raise ConfigurationError(
                f"Window size must be at least 1, got {size}",
                parameter="size",
                value=size,
            )
        self._size = size
        self._window: Deque[bool] = deque(maxlen=size)
        self._failures = 0
        self._total_recorded = 0

    @property
    def size(self) -> int:
        """Maximum number of outcomes retained."""
        return self._size

    @property
    def observations(self) -> int:
        """Number of outcomes currently inside the window."""
        return len(self._window)

    @property
    def failures(self) -> int:
        """Number of failed outcomes currently inside the window."""
        return self._failures

    @property
    def total_recorded(self) -> int:
        """Lifetime count of outcomes recorded, including evicted ones."""
        return self._total_recorded

    @property
    def failure_ratio(self) -> float:
        """
        Empirical drop probability over the window.

        Returns 0.0 for an empty window so an unprimed controller treats the
        path as healthy rather than maximally lossy.
        """
        if not self._window:
            return 0.0
        return self._failures / len(self._window)

    def record(self, success: bool) -> None:
        """Record a single transfer outcome, evicting the oldest if full."""
        if len(self._window) == self._size:
            evicted = self._window[0]
            if not evicted:
                self._failures -= 1
        self._window.append(bool(success))
        if not success:
            self._failures += 1
        self._total_recorded += 1

    def record_success(self) -> None:
        """Record a successful transfer outcome."""
        self.record(True)

    def record_failure(self) -> None:
        """Record a failed transfer outcome."""
        self.record(False)

    def reset(self) -> None:
        """Clear the window and all derived counters."""
        self._window.clear()
        self._failures = 0
        self._total_recorded = 0

    def __len__(self) -> int:
        return len(self._window)

    def __repr__(self) -> str:
        return (
            f"FailureWindow(size={self._size}, observations={len(self._window)}, "
            f"failure_ratio={self.failure_ratio:.4f})"
        )


@dataclass(frozen=True)
class TransferSample:
    """
    A single observed chunk transfer, as reported by a download worker.

    Attributes:
        bytes_transferred: Payload bytes received for this transfer.
        duration_seconds: Wall-clock time from request dispatch to last byte.
        header_latency_seconds: Time to first response header, used as the RTT
            proxy since it excludes body transfer time.
        success: Whether the transfer completed and passed verification.
    """

    bytes_transferred: int
    duration_seconds: float
    header_latency_seconds: Optional[float] = None
    success: bool = True

    def __post_init__(self) -> None:
        if self.bytes_transferred < 0:
            raise ConfigurationError(
                f"bytes_transferred must be non-negative, got {self.bytes_transferred}",
                parameter="bytes_transferred",
                value=self.bytes_transferred,
            )
        if self.duration_seconds <= 0.0:
            raise ConfigurationError(
                f"duration_seconds must be positive, got {self.duration_seconds}",
                parameter="duration_seconds",
                value=self.duration_seconds,
            )
        if (
            self.header_latency_seconds is not None
            and self.header_latency_seconds < 0.0
        ):
            raise ConfigurationError(
                "header_latency_seconds must be non-negative, got "
                f"{self.header_latency_seconds}",
                parameter="header_latency_seconds",
                value=self.header_latency_seconds,
            )

    @property
    def throughput_bps(self) -> float:
        """Observed throughput for this transfer in bytes per second."""
        return self.bytes_transferred / self.duration_seconds


@dataclass(frozen=True)
class NetworkStateSnapshot:
    """
    Immutable view of the smoothed network state at one decision epoch.

    Consumed by the BL-DCS controller; frozen so a controller cannot mutate the
    estimates it was handed mid-computation.
    """

    rtt_seconds: Optional[float]
    rtt_deviation_seconds: float
    throughput_bps: Optional[float]
    drop_probability: float
    bdp_bytes: Optional[float]
    mean_transfer_bytes: Optional[float]
    samples: int
    failures: int

    @property
    def is_primed(self) -> bool:
        """True when both RTT and throughput estimates are available."""
        return self.rtt_seconds is not None and self.throughput_bps is not None


class NetworkMetricsCollector:
    """
    Thread-safe aggregator of per-worker transfer observations.

    Download workers run concurrently and report into a single collector, so
    every mutation and every snapshot is taken under one lock. Snapshots are
    consistent: the RTT, throughput, and drop probability in a snapshot always
    reflect the same set of absorbed samples.
    """

    def __init__(
        self,
        rtt_alpha: float = DEFAULT_RTT_ALPHA,
        throughput_beta: float = DEFAULT_THROUGHPUT_BETA,
        deviation_alpha: float = DEFAULT_DEVIATION_ALPHA,
        failure_window_size: int = DEFAULT_FAILURE_WINDOW_SIZE,
    ) -> None:
        self._rtt = EWMAEstimator(alpha=rtt_alpha, deviation_alpha=deviation_alpha)
        self._throughput = EWMAEstimator(
            alpha=throughput_beta, deviation_alpha=deviation_alpha
        )
        self._transfer_bytes = EWMAEstimator(
            alpha=throughput_beta, deviation_alpha=deviation_alpha
        )
        self._failures = FailureWindow(size=failure_window_size)
        self._lock = threading.RLock()

    @property
    def rtt_estimator(self) -> EWMAEstimator:
        """Underlying RTT filter, exposed for introspection and telemetry."""
        return self._rtt

    @property
    def throughput_estimator(self) -> EWMAEstimator:
        """Underlying throughput filter, exposed for introspection."""
        return self._throughput

    @property
    def transfer_size_estimator(self) -> EWMAEstimator:
        """Smoothed size of recent transfers, used to scale the loss model."""
        return self._transfer_bytes

    @property
    def failure_window(self) -> FailureWindow:
        """Underlying sliding failure window."""
        return self._failures

    def record_transfer(self, sample: TransferSample) -> None:
        """
        Absorb one completed transfer observation.

        Throughput is only credited for successful transfers: a failed range
        request typically aborts partway, so its byte rate understates the path
        and would drag the estimate down twice, once here and once through the
        drop probability.
        """
        with self._lock:
            if sample.header_latency_seconds is not None:
                self._rtt.update(sample.header_latency_seconds)
            if sample.success and sample.bytes_transferred > 0:
                self._throughput.update(sample.throughput_bps)
                self._transfer_bytes.update(float(sample.bytes_transferred))
            self._failures.record(sample.success)

    def record_sample(
        self,
        bytes_transferred: int,
        duration_seconds: float,
        header_latency_seconds: Optional[float] = None,
        success: bool = True,
    ) -> None:
        """Convenience wrapper constructing a TransferSample from raw values."""
        self.record_transfer(
            TransferSample(
                bytes_transferred=bytes_transferred,
                duration_seconds=duration_seconds,
                header_latency_seconds=header_latency_seconds,
                success=success,
            )
        )

    def record_failure(self) -> None:
        """Record a transfer failure carrying no usable timing observation."""
        with self._lock:
            self._failures.record_failure()

    def record_rtt(self, rtt_seconds: float) -> None:
        """Record a standalone RTT observation, e.g. from a HEAD probe."""
        if rtt_seconds < 0.0:
            raise ConfigurationError(
                f"rtt_seconds must be non-negative, got {rtt_seconds}",
                parameter="rtt_seconds",
                value=rtt_seconds,
            )
        with self._lock:
            self._rtt.update(rtt_seconds)

    def snapshot(self) -> NetworkStateSnapshot:
        """Capture a consistent view of the current smoothed network state."""
        with self._lock:
            rtt = self._rtt.value
            throughput = self._throughput.value
            bdp = None if rtt is None or throughput is None else throughput * rtt
            return NetworkStateSnapshot(
                rtt_seconds=rtt,
                rtt_deviation_seconds=self._rtt.deviation,
                throughput_bps=throughput,
                drop_probability=self._failures.failure_ratio,
                bdp_bytes=bdp,
                mean_transfer_bytes=self._transfer_bytes.value,
                samples=self._failures.observations,
                failures=self._failures.failures,
            )

    def reset(self) -> None:
        """Clear every estimator and the failure window."""
        with self._lock:
            self._rtt.reset()
            self._throughput.reset()
            self._transfer_bytes.reset()
            self._failures.reset()

    def __repr__(self) -> str:
        state = self.snapshot()
        return (
            f"NetworkMetricsCollector(rtt={state.rtt_seconds}, "
            f"throughput={state.throughput_bps}, "
            f"drop_probability={state.drop_probability:.4f})"
        )
