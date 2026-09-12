"""
Straggler detection for the SR-WSRS scheduler in ReliaDL.

A parallel range download finishes when its *slowest* worker finishes. One
connection landing on a congested path or an overloaded edge node therefore sets
the completion time for the whole transfer, no matter how fast the other
workers drained their ranges. That tail is what this module exists to find, by
projecting each worker's time to completion and flagging the ones that will
still be running long after everyone else has gone idle:

    TTC_i = BytesRemaining_i / Throughput_i

A worker is a straggler when its projection dominates the fastest active peer
by more than a fixed factor and it still holds enough bytes to be worth
splitting:

    TTC_i > 2.5 * min_{j != i} TTC_j    and    BytesRemaining_i >= 2 * S_min

Both conditions matter, and for different reasons. The ratio test identifies a
worker that is slow *relative to the same file on the same network*, which is
the only comparison that means anything — an absolute throughput floor would
condemn every worker on a slow link and none on a fast one. The remainder guard
then asks whether acting is worthwhile: bisecting a range leaves each half with
``BytesRemaining / 2``, so requiring twice the minimum split keeps both halves
above the size where a fresh connection setup costs more than the transfer it
saves. A worker 300 KB from done is slow but not worth interrupting.

What the baseline is measured over
---------------------------------
The trigger above says ``min`` over all other workers, which is not quite
usable as written. A worker that has already finished its range projects
TTC = 0, and a zero baseline makes ``2.5 * 0`` flag every worker still holding
bytes. A finished worker is not evidence about path health; it is evidence that
it has nothing left to do.

The baseline is therefore taken over *active* peers only: those still holding
bytes and carrying a throughput estimate. The same reasoning excludes workers
whose estimate has not yet been primed. A worker with no measurement cannot be
flagged and cannot anchor the comparison, because in both directions there is
no evidence to act on. With fewer than two active workers there is no baseline
at all and nobody is flagged — a single worker cannot be slow relative to
itself.

A stalled connection is the case this is built for, and it falls out without a
special path: a worker whose throughput has collapsed to zero projects an
infinite TTC, which exceeds any finite baseline by any factor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.algorithms.adaptive_chunker import MIN_CHUNK_SIZE
from src.exceptions import ConfigurationError

# Multiple of the fastest active peer's TTC beyond which a worker is a
# straggler. Chosen well above 1 so ordinary throughput jitter between healthy
# connections never trips it; a worker must be dramatically behind, not merely
# behind.
DEFAULT_STRAGGLER_FACTOR = 2.5

# Smallest range worth handing to a separate connection. Below this the request
# overhead dominates the bytes transferred.
DEFAULT_MIN_SPLIT_BYTES = MIN_CHUNK_SIZE


class StragglerReason(str, Enum):
    """Why a worker was or was not flagged, retained for scheduler logs."""

    # Projection exceeds the threshold and the remainder is worth splitting.
    STRAGGLER = "STRAGGLER"

    # Range fully received; nothing left to project or to steal.
    COMPLETE = "COMPLETE"

    # No throughput estimate yet, so no projection can be made.
    UNPRIMED = "UNPRIMED"

    # Fewer than two active workers, so there is nothing to compare against.
    NO_BASELINE = "NO_BASELINE"

    # Projection is within the threshold of the fastest active peer.
    WITHIN_THRESHOLD = "WITHIN_THRESHOLD"

    # Slow, but too near the end of its range for a split to pay for itself.
    REMAINDER_TOO_SMALL = "REMAINDER_TOO_SMALL"


@dataclass(frozen=True)
class WorkerProgress:
    """
    A point-in-time progress report for one worker's assigned byte range.

    Ranges are inclusive of both endpoints, matching HTTP Range semantics and
    ``ChunkSpec``. ``next_byte`` is the first byte *not yet received*, so it
    equals ``range_end + 1`` exactly when the range is complete; expressing
    progress as a cursor rather than a count is what lets the bisection
    protocol split a range without ever re-downloading a delivered byte.

    Attributes:
        worker_id: Stable identifier of the reporting worker.
        range_start: First byte of the assigned range, inclusive.
        range_end: Last byte of the assigned range, inclusive.
        next_byte: First byte not yet received.
        throughput_bps: Observed throughput in bytes/second, or None if the
            worker has no measurement yet. Zero means a stalled connection.
    """

    worker_id: str
    range_start: int
    range_end: int
    next_byte: int
    throughput_bps: Optional[float] = None

    def __post_init__(self) -> None:
        if not isinstance(self.worker_id, str) or not self.worker_id:
            raise ConfigurationError(
                "worker_id must be a non-empty string",
                parameter="worker_id",
                value=self.worker_id,
            )
        for name, value in (
            ("range_start", self.range_start),
            ("range_end", self.range_end),
            ("next_byte", self.next_byte),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ConfigurationError(
                    f"{name} must be an integer, got {type(value).__name__}",
                    parameter=name,
                    value=value,
                )
        if self.range_start < 0:
            raise ConfigurationError(
                f"range_start must be non-negative, got {self.range_start}",
                parameter="range_start",
                value=self.range_start,
            )
        if self.range_end < self.range_start:
            raise ConfigurationError(
                f"range_end ({self.range_end}) cannot precede range_start "
                f"({self.range_start})",
                parameter="range_end",
                value=self.range_end,
            )
        if not self.range_start <= self.next_byte <= self.range_end + 1:
            raise ConfigurationError(
                f"next_byte ({self.next_byte}) must lie within "
                f"[{self.range_start}, {self.range_end + 1}]",
                parameter="next_byte",
                value=self.next_byte,
            )
        if self.throughput_bps is not None:
            if isinstance(self.throughput_bps, bool) or not isinstance(
                self.throughput_bps, (int, float)
            ):
                raise ConfigurationError(
                    "throughput_bps must be numeric or None, got "
                    f"{type(self.throughput_bps).__name__}",
                    parameter="throughput_bps",
                    value=self.throughput_bps,
                )
            numeric = float(self.throughput_bps)
            if not math.isfinite(numeric) or numeric < 0.0:
                raise ConfigurationError(
                    "throughput_bps must be a non-negative finite number, got "
                    f"{self.throughput_bps}",
                    parameter="throughput_bps",
                    value=self.throughput_bps,
                )
            object.__setattr__(self, "throughput_bps", numeric)

    @property
    def bytes_total(self) -> int:
        """Size of the assigned range in bytes."""
        return self.range_end - self.range_start + 1

    @property
    def bytes_completed(self) -> int:
        """Bytes already received for this range."""
        return self.next_byte - self.range_start

    @property
    def bytes_remaining(self) -> int:
        """Bytes still outstanding on this range."""
        return self.range_end - self.next_byte + 1

    @property
    def is_complete(self) -> bool:
        """True once every byte of the range has been received."""
        return self.next_byte > self.range_end

    @property
    def has_estimate(self) -> bool:
        """True when a throughput measurement is available."""
        return self.throughput_bps is not None

    @property
    def is_active(self) -> bool:
        """True when the worker still holds bytes and can be projected."""
        return not self.is_complete and self.has_estimate

    @property
    def ttc_seconds(self) -> Optional[float]:
        """
        Projected seconds until this range completes.

        None when no throughput estimate exists, 0.0 when the range is already
        complete, and infinite when the connection has stalled at zero
        throughput with bytes still outstanding.
        """
        if self.is_complete:
            return 0.0
        if self.throughput_bps is None:
            return None
        if self.throughput_bps == 0.0:
            return math.inf
        return self.bytes_remaining / self.throughput_bps

    @property
    def completion_ratio(self) -> float:
        """Fraction of the assigned range already received, in [0, 1]."""
        return self.bytes_completed / self.bytes_total

    @classmethod
    def from_observation(
        cls,
        worker_id: str,
        range_start: int,
        range_end: int,
        next_byte: int,
        elapsed_seconds: float,
    ) -> "WorkerProgress":
        """
        Build a report deriving throughput from bytes received over elapsed time.

        A worker that has been connected for a while without receiving anything
        reports zero throughput rather than no estimate, which is the
        distinction that lets a stalled connection be flagged instead of
        excused as unmeasured.

        Raises:
            ConfigurationError: If elapsed_seconds is not a positive finite number.
        """
        if isinstance(elapsed_seconds, bool) or not isinstance(
            elapsed_seconds, (int, float)
        ):
            raise ConfigurationError(
                f"elapsed_seconds must be numeric, got {type(elapsed_seconds).__name__}",
                parameter="elapsed_seconds",
                value=elapsed_seconds,
            )
        elapsed = float(elapsed_seconds)
        if not math.isfinite(elapsed) or elapsed <= 0.0:
            raise ConfigurationError(
                f"elapsed_seconds must be positive and finite, got {elapsed_seconds}",
                parameter="elapsed_seconds",
                value=elapsed_seconds,
            )
        completed = next_byte - range_start
        return cls(
            worker_id=worker_id,
            range_start=range_start,
            range_end=range_end,
            next_byte=next_byte,
            throughput_bps=max(0.0, completed) / elapsed,
        )

    def __repr__(self) -> str:
        ttc = self.ttc_seconds
        rendered = "unknown" if ttc is None else f"{ttc:.3f}s"
        return (
            f"WorkerProgress(worker_id={self.worker_id!r}, "
            f"range=[{self.range_start}, {self.range_end}], "
            f"next_byte={self.next_byte}, remaining={self.bytes_remaining}, "
            f"ttc={rendered})"
        )


@dataclass(frozen=True)
class StragglerVerdict:
    """
    The detector's finding for one worker, including the terms behind it.

    Carries the baseline and ratio rather than just a boolean so a scheduler log
    can answer *why* a worker was or was not split after the fact, which is the
    only way to tune the factor against a real transfer.

    Attributes:
        worker_id: Worker this verdict concerns.
        is_straggler: Whether the worker should be considered for bisection.
        reason: Which branch of the predicate decided the outcome.
        ttc_seconds: This worker's projection, or None if unprimed.
        baseline_ttc_seconds: Fastest active peer's projection, or None.
        ratio: ttc / baseline, or None when either term is unavailable.
        bytes_remaining: Bytes still outstanding for this worker.
        threshold_ttc_seconds: Projection above which this worker would be
            flagged, or None when no baseline exists.
    """

    worker_id: str
    is_straggler: bool
    reason: StragglerReason
    ttc_seconds: Optional[float]
    baseline_ttc_seconds: Optional[float]
    ratio: Optional[float]
    bytes_remaining: int
    threshold_ttc_seconds: Optional[float]

    def __repr__(self) -> str:
        return (
            f"StragglerVerdict(worker_id={self.worker_id!r}, "
            f"is_straggler={self.is_straggler}, reason={self.reason.value}, "
            f"ratio={self.ratio})"
        )


class StragglerDetector:
    """
    Flags workers whose projected completion dominates the transfer's tail.

    Stateless: every verdict is a pure function of the reports handed in, so the
    scheduler may evaluate concurrently from any thread and no history has to be
    kept consistent across epochs. Successive calls with the same reports return
    the same verdicts.
    """

    def __init__(
        self,
        factor: float = DEFAULT_STRAGGLER_FACTOR,
        min_split_bytes: int = DEFAULT_MIN_SPLIT_BYTES,
    ) -> None:
        if isinstance(factor, bool) or not isinstance(factor, (int, float)):
            raise ConfigurationError(
                f"factor must be numeric, got {type(factor).__name__}",
                parameter="factor",
                value=factor,
            )
        numeric_factor = float(factor)
        if not math.isfinite(numeric_factor) or numeric_factor <= 1.0:
            raise ConfigurationError(
                "factor must be a finite number greater than 1; a factor at or "
                f"below 1 would flag healthy workers, got {factor}",
                parameter="factor",
                value=factor,
            )
        if isinstance(min_split_bytes, bool) or not isinstance(min_split_bytes, int):
            raise ConfigurationError(
                f"min_split_bytes must be an integer, got {type(min_split_bytes).__name__}",
                parameter="min_split_bytes",
                value=min_split_bytes,
            )
        if min_split_bytes < 1:
            raise ConfigurationError(
                f"min_split_bytes must be at least 1 byte, got {min_split_bytes}",
                parameter="min_split_bytes",
                value=min_split_bytes,
            )

        self._factor = numeric_factor
        self._min_split = min_split_bytes

    @property
    def factor(self) -> float:
        """Multiple of the fastest active peer's TTC that triggers a flag."""
        return self._factor

    @property
    def min_split_bytes(self) -> int:
        """Smallest range worth handing to a separate connection."""
        return self._min_split

    @property
    def min_remaining_bytes(self) -> int:
        """
        Smallest remainder worth interrupting a worker for.

        Twice the minimum split, because a bisection leaves each side with half
        the remainder and both halves must clear the floor.
        """
        return 2 * self._min_split

    @staticmethod
    def _validate_reports(
        reports: Iterable[WorkerProgress],
    ) -> Tuple[WorkerProgress, ...]:
        """
        Materialize the reports and reject duplicate worker identities.

        Two reports for one worker would let a worker anchor the baseline
        against its own stale projection, so this is a correctness check rather
        than a convenience.
        """
        materialized = tuple(reports)
        for report in materialized:
            if not isinstance(report, WorkerProgress):
                raise ConfigurationError(
                    "reports must contain WorkerProgress instances, got "
                    f"{type(report).__name__}",
                    parameter="reports",
                    value=report,
                )
        seen = set()
        for report in materialized:
            if report.worker_id in seen:
                raise ConfigurationError(
                    f"Duplicate progress report for worker {report.worker_id!r}",
                    parameter="reports",
                    value=report.worker_id,
                )
            seen.add(report.worker_id)
        return materialized

    def baseline_ttc(
        self,
        reports: Sequence[WorkerProgress],
        exclude_worker_id: Optional[str] = None,
    ) -> Optional[float]:
        """
        Fastest projection among active workers, excluding one if named.

        Returns None when no active peer remains. Completed and unprimed
        workers are skipped: neither carries information about how fast the
        path is currently moving bytes.
        """
        candidates = [
            report.ttc_seconds
            for report in reports
            if report.is_active and report.worker_id != exclude_worker_id
        ]
        finite = [ttc for ttc in candidates if ttc is not None]
        if not finite:
            return None
        return min(finite)

    def evaluate(self, reports: Iterable[WorkerProgress]) -> List[StragglerVerdict]:
        """
        Produce a verdict for every reported worker, in the order supplied.

        Raises:
            ConfigurationError: If a report is malformed or a worker appears twice.
        """
        materialized = self._validate_reports(reports)
        return [
            self._evaluate_one(report, materialized) for report in materialized
        ]

    def _evaluate_one(
        self, report: WorkerProgress, all_reports: Sequence[WorkerProgress]
    ) -> StragglerVerdict:
        """Apply the two-part predicate to a single worker."""
        remaining = max(0, report.bytes_remaining)

        def verdict(
            is_straggler: bool,
            reason: StragglerReason,
            baseline: Optional[float] = None,
            ratio: Optional[float] = None,
            threshold: Optional[float] = None,
        ) -> StragglerVerdict:
            return StragglerVerdict(
                worker_id=report.worker_id,
                is_straggler=is_straggler,
                reason=reason,
                ttc_seconds=report.ttc_seconds,
                baseline_ttc_seconds=baseline,
                ratio=ratio,
                bytes_remaining=remaining,
                threshold_ttc_seconds=threshold,
            )

        if report.is_complete:
            return verdict(False, StragglerReason.COMPLETE)
        if not report.has_estimate:
            return verdict(False, StragglerReason.UNPRIMED)

        baseline = self.baseline_ttc(all_reports, exclude_worker_id=report.worker_id)
        if baseline is None:
            return verdict(False, StragglerReason.NO_BASELINE)

        threshold = self._factor * baseline
        ttc = report.ttc_seconds
        # ttc is not None here: has_estimate and not is_complete were checked.
        assert ttc is not None
        ratio = math.inf if baseline == 0.0 else ttc / baseline

        if not ttc > threshold:
            return verdict(
                False, StragglerReason.WITHIN_THRESHOLD, baseline, ratio, threshold
            )
        if remaining < self.min_remaining_bytes:
            return verdict(
                False, StragglerReason.REMAINDER_TOO_SMALL, baseline, ratio, threshold
            )
        return verdict(True, StragglerReason.STRAGGLER, baseline, ratio, threshold)

    def detect(self, reports: Iterable[WorkerProgress]) -> List[StragglerVerdict]:
        """
        Return only the flagged verdicts, worst offender first.

        Ordering by descending TTC matters when idle workers are scarce: the
        worker projected to finish last is the one setting the transfer's
        completion time, so it is the one worth splitting first.
        """
        flagged = [v for v in self.evaluate(reports) if v.is_straggler]
        return sorted(
            flagged,
            key=lambda v: (v.ttc_seconds or 0.0, v.bytes_remaining),
            reverse=True,
        )

    def worst_straggler(
        self, reports: Iterable[WorkerProgress]
    ) -> Optional[StragglerVerdict]:
        """Return the single worst straggler, or None when none is flagged."""
        flagged = self.detect(reports)
        return flagged[0] if flagged else None

    def verdicts_by_worker(
        self, reports: Iterable[WorkerProgress]
    ) -> Dict[str, StragglerVerdict]:
        """Index the full verdict set by worker identifier."""
        return {verdict.worker_id: verdict for verdict in self.evaluate(reports)}

    def __repr__(self) -> str:
        return (
            f"StragglerDetector(factor={self._factor}, "
            f"min_split_bytes={self._min_split})"
        )
