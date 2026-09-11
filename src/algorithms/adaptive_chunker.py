"""
BL-DCS (Bandwidth-Loss Closed-Loop Dynamic Chunk Sizing) controller for ReliaDL.

Translates the smoothed network state produced by the metrics collector into an
optimal chunk size at each decision epoch:

    S* = clamp( gamma * sqrt( tau_overhead * mu_k * MSS / (8 * p_k + eps) )
                + lambda * mu_k * tau_k,
                S_min, S_max )

Two corrections to the bare formulation are applied, both required for the
controller to produce byte counts that mean anything.

The MSS factor inside the radical gives dimensional consistency: it makes the
radicand a byte-squared quantity, so the root yields bytes rather than the
unit-dependent sqrt(bytes) of the bare form. It falls out of minimizing the
per-byte cost (tau_overhead * mu)/S + p_seg * S/MSS with respect to S.

The drop probability is also rescaled. The metrics collector observes failures
per chunk, while the loss model is expressed per segment; at an 8 MB chunk the
two differ by roughly four orders of magnitude. Closing that loop with the
transfer size that produced the observation,

    p_seg = 1 - (1 - p_chunk) ** (MSS / S_observed)

converts the measured chunk failure ratio into the per-segment loss rate the
radical expects.

The two terms encode opposing pressures. The loss term shrinks the chunk as the
empirical drop probability rises, bounding the payload lost to a single failed
range request. The bandwidth-delay term grows the chunk in proportion to the
path BDP so a fast, clean link is not throttled by per-chunk request overhead.
The result is quantized to a power of two for allocator and page alignment.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from typing import Optional

from src.algorithms.metrics_collector import NetworkStateSnapshot
from src.exceptions import ConfigurationError
from src.models import ChunkSpec

# Scaling coefficient applied to the loss-sensitivity term.
DEFAULT_GAMMA = 1.0

# Scaling coefficient applied to the bandwidth-delay product term.
DEFAULT_LAMBDA = 1.0

# Nominal per-chunk request overhead in seconds (connection reuse assumed).
DEFAULT_OVERHEAD_SECONDS = 0.025

# Numerical floor preventing division by zero on a lossless path.
DEFAULT_EPSILON = 1e-9

# Maximum segment size assumed for the loss model, in bytes (typical Ethernet
# MSS after IPv4 and TCP headers).
DEFAULT_MSS_BYTES = 1460

# Hardware-aligned chunk size bounds.
MIN_CHUNK_SIZE = 1 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024

# Chunk size used before the estimators are primed.
DEFAULT_CHUNK_SIZE = 8 * 1024 * 1024


def _validate_positive(value: float, parameter: str) -> float:
    """
    Validate that a coefficient is a positive, finite number.

    Raises:
        ConfigurationError: If the value is non-numeric, non-finite, or <= 0.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(
            f"{parameter} must be numeric, got {type(value).__name__}",
            parameter=parameter,
            value=value,
        )
    numeric = float(value)
    if not math.isfinite(numeric) or numeric <= 0.0:
        raise ConfigurationError(
            f"{parameter} must be a positive finite number, got {value}",
            parameter=parameter,
            value=value,
        )
    return numeric


def is_power_of_two(value: int) -> bool:
    """Return True when value is a positive integral power of two."""
    return value > 0 and (value & (value - 1)) == 0


def align_to_power_of_two(value: float) -> int:
    """
    Quantize a byte count to the nearest power of two in log space.

    Geometric rather than arithmetic rounding: 12 MB is equidistant from 8 MB
    and 16 MB on a linear scale, but the controller reasons multiplicatively
    about capacity, so the nearer power in log2 terms is the right target.
    """
    if value <= 1.0:
        return 1
    exponent = round(math.log2(value))
    return 1 << max(0, int(exponent))


@dataclass(frozen=True)
class ChunkSizingDecision:
    """
    One controller output, retaining the intermediate terms for observability.

    Attributes:
        chunk_size: Final chunk size in bytes, aligned and clamped.
        raw_size: Controller output before alignment and clamping.
        loss_term: Contribution of the loss-sensitivity term, in bytes.
        segment_drop: Per-segment loss rate derived from the chunk failure ratio.
        bdp_term: Contribution of the bandwidth-delay term, in bytes.
        clamped_low: Whether the raw output fell below the configured minimum.
        clamped_high: Whether the raw output exceeded the configured maximum.
        primed: Whether the decision used live estimates or the cold-start default.
    """

    chunk_size: int
    raw_size: float
    loss_term: float
    bdp_term: float
    segment_drop: float
    clamped_low: bool
    clamped_high: bool
    primed: bool

    @property
    def clamped(self) -> bool:
        """True when the raw output was constrained by either bound."""
        return self.clamped_low or self.clamped_high


class BLDCSController:
    """
    Closed-loop chunk sizing controller.

    Stateless with respect to transfer history: every decision is a pure
    function of the snapshot handed in, so the controller can be queried
    concurrently by multiple workers without synchronization.
    """

    def __init__(
        self,
        gamma: float = DEFAULT_GAMMA,
        lambda_bdp: float = DEFAULT_LAMBDA,
        overhead_seconds: float = DEFAULT_OVERHEAD_SECONDS,
        min_chunk_size: int = MIN_CHUNK_SIZE,
        max_chunk_size: int = MAX_CHUNK_SIZE,
        default_chunk_size: int = DEFAULT_CHUNK_SIZE,
        epsilon: float = DEFAULT_EPSILON,
        mss_bytes: int = DEFAULT_MSS_BYTES,
        align_power_of_two: bool = True,
    ) -> None:
        self._gamma = _validate_positive(gamma, "gamma")
        self._lambda = _validate_positive(lambda_bdp, "lambda_bdp")
        self._overhead = _validate_positive(overhead_seconds, "overhead_seconds")
        self._epsilon = _validate_positive(epsilon, "epsilon")
        self._mss = int(_validate_positive(mss_bytes, "mss_bytes"))

        for name, bound in (
            ("min_chunk_size", min_chunk_size),
            ("max_chunk_size", max_chunk_size),
            ("default_chunk_size", default_chunk_size),
        ):
            if isinstance(bound, bool) or not isinstance(bound, int):
                raise ConfigurationError(
                    f"{name} must be an integer, got {type(bound).__name__}",
                    parameter=name,
                    value=bound,
                )
            if bound < 1:
                raise ConfigurationError(
                    f"{name} must be at least 1 byte, got {bound}",
                    parameter=name,
                    value=bound,
                )

        if min_chunk_size > max_chunk_size:
            raise ConfigurationError(
                f"min_chunk_size ({min_chunk_size}) exceeds max_chunk_size "
                f"({max_chunk_size})",
                parameter="min_chunk_size",
                value=min_chunk_size,
            )
        if not min_chunk_size <= default_chunk_size <= max_chunk_size:
            raise ConfigurationError(
                f"default_chunk_size ({default_chunk_size}) must lie within "
                f"[{min_chunk_size}, {max_chunk_size}]",
                parameter="default_chunk_size",
                value=default_chunk_size,
            )
        if align_power_of_two and not (
            is_power_of_two(min_chunk_size) and is_power_of_two(max_chunk_size)
        ):
            raise ConfigurationError(
                "min_chunk_size and max_chunk_size must be powers of two when "
                "power-of-two alignment is enabled",
                parameter="min_chunk_size",
                value=min_chunk_size,
            )

        self._min = min_chunk_size
        self._max = max_chunk_size
        self._default = default_chunk_size
        self._align = bool(align_power_of_two)

    @property
    def gamma(self) -> float:
        """Loss-term scaling coefficient."""
        return self._gamma

    @property
    def lambda_bdp(self) -> float:
        """Bandwidth-delay term scaling coefficient."""
        return self._lambda

    @property
    def overhead_seconds(self) -> float:
        """Nominal per-chunk request overhead."""
        return self._overhead

    @property
    def mss_bytes(self) -> int:
        """Maximum segment size assumed by the loss model."""
        return self._mss

    @property
    def min_chunk_size(self) -> int:
        """Lower clamp on the controller output."""
        return self._min

    @property
    def max_chunk_size(self) -> int:
        """Upper clamp on the controller output."""
        return self._max

    @property
    def default_chunk_size(self) -> int:
        """Chunk size used before the estimators are primed."""
        return self._default

    @property
    def aligns_to_power_of_two(self) -> bool:
        """Whether outputs are quantized to powers of two."""
        return self._align

    def _segment_loss_rate(self, chunk_drop: float, observed_size: float) -> float:
        """
        Convert an observed per-chunk failure ratio into a per-segment loss rate.

        A chunk spanning n = S/MSS segments survives only if every segment does,
        so p_chunk = 1 - (1 - p_seg)^n inverts to the expression below. Guards
        the degenerate ends: a chunk that always fails carries no information
        about which segment caused it, and is reported as total segment loss.
        """
        if chunk_drop <= 0.0:
            return 0.0
        if chunk_drop >= 1.0:
            return 1.0
        segments = max(1.0, observed_size / float(self._mss))
        return 1.0 - (1.0 - chunk_drop) ** (1.0 / segments)

    def decide(self, state: NetworkStateSnapshot) -> ChunkSizingDecision:
        """
        Compute the chunk size for the current network state.

        Falls back to the configured default while the estimators are unprimed:
        with no throughput measurement there is no basis for either term, and
        guessing small would starve a fast link for the opening transfers.
        """
        if not state.is_primed:
            return ChunkSizingDecision(
                chunk_size=self._default,
                raw_size=float(self._default),
                loss_term=0.0,
                bdp_term=0.0,
                segment_drop=0.0,
                clamped_low=False,
                clamped_high=False,
                primed=False,
            )

        throughput = float(state.throughput_bps or 0.0)
        rtt = float(state.rtt_seconds or 0.0)
        drop = max(0.0, min(1.0, state.drop_probability))
        observed_size = state.mean_transfer_bytes or float(self._default)
        segment_drop = self._segment_loss_rate(drop, observed_size)

        loss_term = self._gamma * math.sqrt(
            (self._overhead * throughput * self._mss)
            / (8.0 * segment_drop + self._epsilon)
        )
        bdp_term = self._lambda * throughput * rtt
        raw = loss_term + bdp_term

        clamped_low = raw < self._min
        clamped_high = raw > self._max

        bounded = min(max(raw, float(self._min)), float(self._max))
        if self._align:
            bounded = float(align_to_power_of_two(bounded))
            # Alignment can round outside the range; re-clamp to guarantee bounds.
            bounded = min(max(bounded, float(self._min)), float(self._max))

        return ChunkSizingDecision(
            chunk_size=int(bounded),
            raw_size=raw,
            loss_term=loss_term,
            bdp_term=bdp_term,
            segment_drop=segment_drop,
            clamped_low=clamped_low,
            clamped_high=clamped_high,
            primed=True,
        )

    def compute_chunk_size(self, state: NetworkStateSnapshot) -> int:
        """Return only the chunk size in bytes for the given network state."""
        return self.decide(state).chunk_size

    def __repr__(self) -> str:
        return (
            f"BLDCSController(gamma={self._gamma}, lambda={self._lambda}, "
            f"overhead={self._overhead}s, bounds=[{self._min}, {self._max}])"
        )


class DynamicChunkPlanner:
    """
    Generates variable-sized chunk boundaries on demand during a transfer.

    Unlike a static partitioner, which fixes every boundary before the first
    byte moves, the planner issues one range at a time and asks the controller
    for a fresh size at each request. A transfer that degrades halfway through
    therefore narrows its remaining chunks instead of committing to a plan made
    under conditions that no longer hold.

    Boundaries are contiguous and non-overlapping by construction: each chunk
    begins where the previous one ended, and the final chunk terminates at
    exactly ``file_size - 1``.
    """

    def __init__(
        self,
        file_size: int,
        controller: Optional[BLDCSController] = None,
        start_offset: int = 0,
        start_index: int = 0,
    ) -> None:
        if isinstance(file_size, bool) or not isinstance(file_size, int):
            raise ConfigurationError(
                f"file_size must be an integer, got {type(file_size).__name__}",
                parameter="file_size",
                value=file_size,
            )
        if file_size < 0:
            raise ConfigurationError(
                f"file_size must be non-negative, got {file_size}",
                parameter="file_size",
                value=file_size,
            )
        if not 0 <= start_offset <= file_size:
            raise ConfigurationError(
                f"start_offset ({start_offset}) must lie within [0, {file_size}]",
                parameter="start_offset",
                value=start_offset,
            )
        if start_index < 0:
            raise ConfigurationError(
                f"start_index must be non-negative, got {start_index}",
                parameter="start_index",
                value=start_index,
            )

        self._file_size = file_size
        self._controller = controller or BLDCSController()
        self._offset = start_offset
        self._index = start_index
        self._lock = threading.RLock()

    @property
    def file_size(self) -> int:
        """Total size of the artifact being partitioned."""
        return self._file_size

    @property
    def controller(self) -> BLDCSController:
        """Sizing controller consulted for each boundary."""
        return self._controller

    @property
    def next_offset(self) -> int:
        """Byte offset at which the next chunk will begin."""
        return self._offset

    @property
    def next_index(self) -> int:
        """Index that will be assigned to the next chunk."""
        return self._index

    @property
    def remaining_bytes(self) -> int:
        """Bytes not yet covered by an issued chunk."""
        return self._file_size - self._offset

    @property
    def is_exhausted(self) -> bool:
        """True once every byte of the artifact has been assigned to a chunk."""
        return self._offset >= self._file_size

    def next_chunk(
        self, state: Optional[NetworkStateSnapshot] = None
    ) -> Optional[ChunkSpec]:
        """
        Issue the next chunk specification, or None when the file is covered.

        The size is taken from the controller for the supplied network state;
        the final chunk is truncated to whatever remains so the range always
        ends at ``file_size - 1``.
        """
        with self._lock:
            if self.is_exhausted:
                return None

            if state is None:
                size = self._controller.default_chunk_size
            else:
                size = self._controller.compute_chunk_size(state)

            size = min(size, self.remaining_bytes)
            start = self._offset
            end = start + size - 1

            spec = ChunkSpec(index=self._index, start_byte=start, end_byte=end)
            self._offset = end + 1
            self._index += 1
            return spec

    def plan_all(
        self, state: Optional[NetworkStateSnapshot] = None
    ) -> list[ChunkSpec]:
        """
        Drain the planner into a complete partition under a fixed network state.

        Intended for manifest generation and tests; a live transfer should call
        next_chunk() per request so each boundary reflects current conditions.
        """
        specs: list[ChunkSpec] = []
        while True:
            spec = self.next_chunk(state)
            if spec is None:
                return specs
            specs.append(spec)

    def reset(self) -> None:
        """Return the planner to the start of the artifact."""
        with self._lock:
            self._offset = 0
            self._index = 0

    def to_dict(self) -> dict:
        """
        Serialize planner position for crash-resilient resume.

        Only the cursor is persisted. Chunk sizes are not replayed, so a resumed
        transfer re-partitions the remaining bytes under current conditions
        rather than inheriting boundaries chosen for a stale network state.
        """
        with self._lock:
            return {
                "file_size": self._file_size,
                "next_offset": self._offset,
                "next_index": self._index,
            }

    @classmethod
    def from_dict(
        cls, data: dict, controller: Optional[BLDCSController] = None
    ) -> "DynamicChunkPlanner":
        """Restore a planner from its serialized cursor."""
        for key in ("file_size", "next_offset", "next_index"):
            if key not in data:
                raise ConfigurationError(
                    f"Serialized planner is missing required key '{key}'",
                    parameter=key,
                    value=data,
                )
        return cls(
            file_size=data["file_size"],
            controller=controller,
            start_offset=data["next_offset"],
            start_index=data["next_index"],
        )

    def __repr__(self) -> str:
        return (
            f"DynamicChunkPlanner(file_size={self._file_size}, "
            f"next_offset={self._offset}, next_index={self._index}, "
            f"remaining={self.remaining_bytes})"
        )
