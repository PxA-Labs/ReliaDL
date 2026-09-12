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

Acting on a flag
----------------
Detection only identifies the tail. Removing it is the job of the bisection
protocol in the second half of this module, which splits a flagged worker's
*undelivered* remainder at its midpoint, truncates the victim, and hands the
far half to an idle worker.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from src.algorithms.adaptive_chunker import MIN_CHUNK_SIZE
from src.exceptions import ConfigurationError
from src.models import ChunkSpec

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


# ---------------------------------------------------------------------------
# Cooperative in-flight range bisection
# ---------------------------------------------------------------------------
#
# Flagging a straggler only identifies the tail; bisection is what removes it.
# The victim's *undelivered* remainder is split at its midpoint, the victim is
# told to stop short of that boundary, and an idle worker opens a fresh range
# request for the far half:
#
#     Mid = NextByte + floor(Remaining / 2)
#
#     before   [ start ......... next_byte ################ end ]
#                                 \__________ remaining _______/
#
#     after    [ start ......... next_byte ##### Mid-1 ]   victim truncates
#                                          [ Mid ###### end ]  thief requests
#
# The split point is measured from ``next_byte`` rather than from ``start``,
# which is the whole reason no byte is transferred twice. Bytes already on disk
# sit in ``[start, next_byte)``, strictly below Mid, so they stay with the
# victim and the thief's request begins past them. Splitting the *original*
# range instead would hand the thief bytes the victim had already written.
#
# Two invariants hold structurally rather than by assertion. The halves are
# disjoint because the victim's new end is exactly ``Mid - 1`` and the thief
# starts at ``Mid``; together they are complete because the victim keeps
# ``[start, Mid-1]`` and the thief takes ``[Mid, end]``, whose union is the
# original range with nothing dropped between them.
#
# The odd byte goes to the thief: the victim takes floor(Remaining / 2) and the
# thief ceil(Remaining / 2). The thief is the worker believed to be healthy, so
# giving it the larger half is the allocation most likely to finish sooner.


class BisectionTrigger(str, Enum):
    """
    Why a range was split, which is not always because someone was slow.

    Straggler relief is the headline case, but it cannot be the only one. A
    detector needs at least two active workers to call one of them slow, so at
    the end of a transfer — when every healthy worker has finished and the last
    one is grinding through its remainder — there is no baseline and nothing is
    ever flagged. That is precisely the moment the tail is longest and the idle
    pool is largest.

    Idle capacity is therefore a trigger in its own right. Handing a spare
    worker half of a large outstanding range is worthwhile whenever a worker is
    sitting idle, regardless of whether the holder is slow: the only cost is a
    connection setup, which the minimum-split floor already bounds against the
    bytes gained. Splitting converges because each split halves the remainder
    and the floor terminates the recursion.
    """

    # A worker was flagged as disproportionately behind its active peers.
    STRAGGLER = "STRAGGLER"

    # A worker was idle and an outstanding range was large enough to share.
    IDLE_CAPACITY = "IDLE_CAPACITY"


@dataclass(frozen=True)
class RangeBisection:
    """
    One cooperative split of a straggler's remaining range.

    Describes the whole hand-off: what the victim must truncate to, and what
    range the thief should request. Immutable so it can be logged as the
    authoritative record of a reassignment and replayed during state recovery.

    Attributes:
        victim_id: Worker whose range is being truncated.
        thief_id: Worker taking over the far half, if one was assigned.
        original_start: First byte of the victim's range before the split.
        original_end: Last byte of the victim's range before the split.
        next_byte: Victim's delivery cursor at the moment of the split.
        boundary: First byte handed to the thief, the ``Mid`` of the formula.
        trigger: Which condition prompted the split.
    """

    victim_id: str
    thief_id: Optional[str]
    original_start: int
    original_end: int
    next_byte: int
    boundary: int
    trigger: BisectionTrigger = BisectionTrigger.STRAGGLER

    def __post_init__(self) -> None:
        if not self.original_start <= self.next_byte <= self.original_end:
            raise ConfigurationError(
                f"next_byte ({self.next_byte}) must lie within the original "
                f"range [{self.original_start}, {self.original_end}]",
                parameter="next_byte",
                value=self.next_byte,
            )
        # A boundary at or below the cursor would hand the thief bytes the
        # victim has already written; one past the end would leave it nothing.
        if not self.next_byte < self.boundary <= self.original_end:
            raise ConfigurationError(
                f"boundary ({self.boundary}) must lie within "
                f"({self.next_byte}, {self.original_end}]",
                parameter="boundary",
                value=self.boundary,
            )

    @property
    def victim_end(self) -> int:
        """Last byte the victim is still responsible for after truncation."""
        return self.boundary - 1

    @property
    def thief_start(self) -> int:
        """First byte the thief must request."""
        return self.boundary

    @property
    def thief_end(self) -> int:
        """Last byte the thief must request, unchanged from the original range."""
        return self.original_end

    @property
    def victim_range(self) -> Tuple[int, int]:
        """Victim's range after truncation, as an inclusive pair."""
        return (self.original_start, self.victim_end)

    @property
    def thief_range(self) -> Tuple[int, int]:
        """Thief's newly assigned range, as an inclusive pair."""
        return (self.thief_start, self.thief_end)

    @property
    def victim_bytes_remaining(self) -> int:
        """Undelivered bytes left with the victim after the split."""
        return self.victim_end - self.next_byte + 1

    @property
    def thief_bytes(self) -> int:
        """Bytes transferred to the thief."""
        return self.thief_end - self.thief_start + 1

    @property
    def bytes_bisected(self) -> int:
        """Undelivered bytes that were divided between the two workers."""
        return self.original_end - self.next_byte + 1

    @property
    def thief_range_header(self) -> str:
        """HTTP Range header value the thief should send."""
        return f"bytes={self.thief_start}-{self.thief_end}"

    def to_chunk_spec(self, index: int) -> ChunkSpec:
        """Express the thief's half as a ChunkSpec for the download engine."""
        return ChunkSpec(
            index=index, start_byte=self.thief_start, end_byte=self.thief_end
        )

    def covers_original_range(self) -> bool:
        """
        Verify the two halves exactly reconstitute the original range.

        The completeness and disjointness invariant, checkable at runtime: the
        halves must abut with no gap and no overlap, and span the original
        extent. Structurally guaranteed, and cheap enough to assert in tests
        and state-recovery paths.
        """
        return (
            self.victim_end + 1 == self.thief_start
            and self.original_start <= self.victim_end
            and self.thief_end == self.original_end
            and self.victim_bytes_remaining + self.thief_bytes == self.bytes_bisected
        )

    def __repr__(self) -> str:
        return (
            f"RangeBisection(victim={self.victim_id!r} -> "
            f"[{self.original_start}, {self.victim_end}], "
            f"thief={self.thief_id!r} -> [{self.thief_start}, {self.thief_end}], "
            f"boundary={self.boundary})"
        )


def can_bisect(
    report: WorkerProgress, min_split_bytes: int = DEFAULT_MIN_SPLIT_BYTES
) -> bool:
    """
    Whether a worker's remainder is large enough to divide usefully.

    Requires twice the minimum split, so the smaller half — the victim's
    ``floor(Remaining / 2)`` — still clears the floor on its own. This is the
    same condition the straggler predicate applies, restated here so bisection
    can be called safely without a detector.
    """
    if min_split_bytes < 1:
        raise ConfigurationError(
            f"min_split_bytes must be at least 1 byte, got {min_split_bytes}",
            parameter="min_split_bytes",
            value=min_split_bytes,
        )
    return not report.is_complete and report.bytes_remaining >= 2 * min_split_bytes


def bisect_range(
    report: WorkerProgress,
    thief_id: Optional[str] = None,
    min_split_bytes: int = DEFAULT_MIN_SPLIT_BYTES,
    trigger: BisectionTrigger = BisectionTrigger.STRAGGLER,
) -> RangeBisection:
    """
    Split a straggler's undelivered remainder at its midpoint.

    The boundary is computed from the delivery cursor, not the range start, so
    bytes already written stay with the victim and the thief's request begins
    strictly past them.

    Raises:
        ConfigurationError: If the remainder is too small to divide, which is a
            caller error — ``can_bisect`` or the detector's remainder guard
            should have ruled it out first.
    """
    if not can_bisect(report, min_split_bytes):
        raise ConfigurationError(
            f"Worker {report.worker_id!r} has {report.bytes_remaining} bytes "
            f"remaining, below the {2 * min_split_bytes} needed to bisect into "
            "two halves above the minimum split",
            parameter="bytes_remaining",
            value=report.bytes_remaining,
        )

    boundary = report.next_byte + report.bytes_remaining // 2
    return RangeBisection(
        victim_id=report.worker_id,
        thief_id=thief_id,
        original_start=report.range_start,
        original_end=report.range_end,
        next_byte=report.next_byte,
        boundary=boundary,
        trigger=trigger,
    )


@dataclass(frozen=True)
class WorkStealPlan:
    """
    One scheduling epoch's reassignments, plus what could not be arranged.

    The unmatched collections are reported rather than discarded: a scheduler
    that repeatedly finds stragglers with no idle worker to give them to is
    running with too few workers, and that is only visible if the shortfall is
    surfaced.

    Attributes:
        bisections: Reassignments to enact, worst straggler first.
        unmatched_stragglers: Flagged workers left unsplit for want of a thief.
        idle_workers: Idle workers that received no work this epoch.
    """

    bisections: Tuple[RangeBisection, ...]
    unmatched_stragglers: Tuple[StragglerVerdict, ...]
    idle_workers: Tuple[str, ...]

    @property
    def has_work(self) -> bool:
        """True when at least one reassignment was arranged."""
        return bool(self.bisections)

    @property
    def bytes_reassigned(self) -> int:
        """Total bytes moved from victims to thieves this epoch."""
        return sum(bisection.thief_bytes for bisection in self.bisections)

    def bisections_by_trigger(
        self, trigger: "BisectionTrigger"
    ) -> Tuple[RangeBisection, ...]:
        """Reassignments prompted by one specific condition."""
        return tuple(b for b in self.bisections if b.trigger is trigger)

    def __repr__(self) -> str:
        return (
            f"WorkStealPlan(bisections={len(self.bisections)}, "
            f"unmatched={len(self.unmatched_stragglers)}, "
            f"idle={len(self.idle_workers)})"
        )


class WorkStealCoordinator:
    """
    Drives one epoch of the SR-WSRS protocol: detect, pair, and bisect.

    Stateless across epochs by design. Each plan is derived entirely from the
    progress reports handed in, so a scheduler that drops or delays an epoch
    simply plans afresh from the next set of observations rather than acting on
    a stale view of where the cursors were.
    """

    def __init__(
        self,
        detector: Optional[StragglerDetector] = None,
        min_split_bytes: int = DEFAULT_MIN_SPLIT_BYTES,
        use_idle_capacity: bool = True,
    ) -> None:
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
        self._detector = detector or StragglerDetector(min_split_bytes=min_split_bytes)
        self._min_split = min_split_bytes
        self._use_idle_capacity = bool(use_idle_capacity)

    @property
    def detector(self) -> StragglerDetector:
        """Detector consulted to identify candidates for bisection."""
        return self._detector

    @property
    def min_split_bytes(self) -> int:
        """Smallest range worth handing to a separate connection."""
        return self._min_split

    @property
    def use_idle_capacity(self) -> bool:
        """Whether spare workers may claim work from unflagged ranges."""
        return self._use_idle_capacity

    def plan(
        self,
        reports: Iterable[WorkerProgress],
        idle_worker_ids: Iterable[str] = (),
    ) -> WorkStealPlan:
        """
        Pair the worst stragglers with available idle workers and split them.

        Each victim is bisected at most once per epoch. Splitting the same range
        twice would compute the second boundary from a cursor that has not
        moved and a range end that the first split already gave away, so the
        halves would overlap. The victim's next report reflects the truncation
        and it can be split again then if it is still behind.

        An idle worker that also appears in the reports as a straggler is not
        considered as a victim: it cannot both need relief and provide it, and
        the discrepancy means the two observations were taken at different
        instants.

        Planning runs in two passes. Flagged stragglers are served first,
        worst-first, because they are the workers setting the transfer's
        completion time. Any idle worker still spare afterwards then claims
        from the largest outstanding range, which is what keeps the protocol
        working at the end of a transfer: once the healthy workers have
        finished, the detector has fewer than two active workers, can no longer
        call anyone slow, and would otherwise leave the last worker to grind
        out its remainder alone with the whole pool idle.
        """
        materialized = tuple(reports)
        idle = self._unique_idle_ids(idle_worker_ids)
        idle_set = set(idle)
        flagged = self._detector.detect(materialized)

        by_id = {report.worker_id: report for report in materialized}
        bisections: List[RangeBisection] = []
        unmatched: List[StragglerVerdict] = []
        available = list(idle)
        split_victims: set = set()

        for verdict in flagged:
            if verdict.worker_id in idle_set:
                continue
            report = by_id[verdict.worker_id]
            if not can_bisect(report, self._min_split):
                # A caller-supplied detector with a smaller floor can flag a
                # worker this coordinator will not cut into slivers.
                unmatched.append(verdict)
                continue
            if not available:
                unmatched.append(verdict)
                continue
            bisections.append(
                bisect_range(
                    report,
                    thief_id=available.pop(0),
                    min_split_bytes=self._min_split,
                    trigger=BisectionTrigger.STRAGGLER,
                )
            )
            split_victims.add(verdict.worker_id)

        if self._use_idle_capacity:
            spare_candidates = sorted(
                (
                    report
                    for report in materialized
                    if report.worker_id not in split_victims
                    and report.worker_id not in idle_set
                    and can_bisect(report, self._min_split)
                ),
                key=lambda report: report.bytes_remaining,
                reverse=True,
            )
            for report in spare_candidates:
                if not available:
                    break
                bisections.append(
                    bisect_range(
                        report,
                        thief_id=available.pop(0),
                        min_split_bytes=self._min_split,
                        trigger=BisectionTrigger.IDLE_CAPACITY,
                    )
                )
                split_victims.add(report.worker_id)

        return WorkStealPlan(
            bisections=tuple(bisections),
            unmatched_stragglers=tuple(unmatched),
            idle_workers=tuple(available),
        )

    @staticmethod
    def _unique_idle_ids(idle_worker_ids: Iterable[str]) -> Tuple[str, ...]:
        """
        Validate idle identifiers, preserving order and rejecting repeats.

        A repeated identifier would let one worker be handed two ranges in a
        single epoch while another sits idle.
        """
        ordered: List[str] = []
        seen = set()
        for worker_id in idle_worker_ids:
            if not isinstance(worker_id, str) or not worker_id:
                raise ConfigurationError(
                    "idle_worker_ids must contain non-empty strings, got "
                    f"{worker_id!r}",
                    parameter="idle_worker_ids",
                    value=worker_id,
                )
            if worker_id in seen:
                raise ConfigurationError(
                    f"Duplicate idle worker identifier {worker_id!r}",
                    parameter="idle_worker_ids",
                    value=worker_id,
                )
            seen.add(worker_id)
            ordered.append(worker_id)
        return tuple(ordered)

    def __repr__(self) -> str:
        return (
            f"WorkStealCoordinator(min_split_bytes={self._min_split}, "
            f"use_idle_capacity={self._use_idle_capacity}, "
            f"detector={self._detector!r})"
        )
