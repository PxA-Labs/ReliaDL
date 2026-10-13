"""
Scheduling seam between the download engine and the algorithm modules.

The algorithms in ``reliadl.algorithms`` were written as standalone, unit-tested
libraries with no dependency on the transfer engine. This module is the one place
that knows about both, so the engine stays readable and each policy can be turned
off independently -- which is also what makes the policies measurable, since an
ablation is just a different ``TransferScheduler`` configuration.

Two policies are offered, and both default to off:

``adaptive_chunk_sizing``
    Chunk boundaries are issued lazily by ``DynamicChunkPlanner`` from the
    measured network state instead of being fixed at ``chunk_size_bytes`` for the
    whole transfer. Boundaries must be decided *during* the transfer for this to
    mean anything, so the engine refills its queue in batches rather than planning
    the file up front.

``adaptive_mirror_routing``
    When the same payload is available from several URLs, each range request is
    routed by ``MirrorDispatcher`` (EXP3.S plus a circuit breaker) rather than
    always going to the origin.

With both off, ``TransferScheduler`` is inert: ``plan_initial`` returns the same
fixed-size partition the engine has always produced, and ``select_mirror``
returns the origin URL. That is deliberate -- the no-policy path is the control
condition every measurement is compared against, so it must not change.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional, Sequence
from urllib.parse import urlsplit

from reliadl.algorithms.adaptive_chunker import BLDCSController, DynamicChunkPlanner
from reliadl.algorithms.metrics_collector import (
    NetworkMetricsCollector,
    NetworkStateSnapshot,
    TransferSample,
)
from reliadl.algorithms.mirror_bandit import (
    DispatchedRequest,
    MirrorDispatcher,
    MirrorEndpoint,
)
from reliadl.models import ChunkState

# How many chunks to plan per refill when sizing adaptively. Planning one at a
# time would consult the controller for every request, which is cheap, but it
# also means the queue is empty whenever a worker finishes; planning the whole
# file at once would defeat adaptivity entirely. A batch keeps workers fed while
# still re-reading the network state several times over a transfer.
DEFAULT_PLAN_BATCH = 8

# Floor on how many chunks must remain queued before a refill is triggered.
DEFAULT_REFILL_THRESHOLD = 2


@dataclass(frozen=True)
class MirrorSelection:
    """A routing decision, and the handle needed to report its outcome."""

    url: str
    request: Optional[DispatchedRequest] = None

    @property
    def mirror_id(self) -> Optional[str]:
        return None if self.request is None else self.request.mirror


def derive_mirror_id(url: str, index: int) -> str:
    """
    Stable identifier for a mirror URL.

    The host is used rather than the full URL so that learned weights survive a
    path or query change on the same edge, which is the distinction
    ``MirrorEndpoint`` exists to preserve. The index disambiguates two URLs on
    one host and guarantees uniqueness, which the bandit requires of its arms.
    """
    host = urlsplit(url).hostname or "origin"
    return f"{index}:{host}"


class TransferScheduler:
    """
    Owns the chunk planner, the mirror router, and the metrics they both read.

    Thread-safe: the engine's workers run on one event loop today, but the
    planner and dispatcher are themselves lock-guarded and this class is the
    shared mutable state between them, so it takes its own lock rather than
    relying on the absence of an await in any particular method.
    """

    def __init__(
        self,
        *,
        origin_url: str,
        file_size: int,
        fixed_chunk_size: int,
        adaptive_chunk_sizing: bool = False,
        adaptive_mirror_routing: bool = False,
        mirrors: Sequence[str] = (),
        hash_algorithm: str = "sha256",
        plan_batch: int = DEFAULT_PLAN_BATCH,
        seed: Optional[int] = None,
    ) -> None:
        self._origin_url = origin_url
        self._file_size = file_size
        self._fixed_chunk_size = fixed_chunk_size
        self._adaptive_chunks = bool(adaptive_chunk_sizing)
        self._plan_batch = max(1, plan_batch)
        self._lock = threading.RLock()

        # The collector is always created: the mirror router and the chunk sizer
        # read the same estimates, and recording samples is cheap enough that
        # doing it unconditionally keeps one code path in the engine.
        self._collector = NetworkMetricsCollector()

        self._planner: Optional[DynamicChunkPlanner] = None
        if self._adaptive_chunks:
            self._planner = DynamicChunkPlanner(
                file_size=file_size,
                controller=BLDCSController(default_chunk_size=fixed_chunk_size),
            )

        self._dispatcher: Optional[MirrorDispatcher] = None
        urls = [origin_url, *[m for m in mirrors if m and m != origin_url]]
        # Routing needs somewhere to route to. With a single endpoint the bandit
        # would degenerate harmlessly, but the circuit breaker would not: it
        # exists to divert traffic away from an unhealthy mirror, and with no
        # alternative it diverts the transfer into a stall instead. A pool of one
        # is therefore served directly, which still records every metric because
        # those come from the collector rather than the dispatcher.
        if adaptive_mirror_routing and len(urls) > 1:
            endpoints = [
                MirrorEndpoint(mirror_id=derive_mirror_id(url, i), url=url)
                for i, url in enumerate(urls)
            ]
            self._dispatcher = MirrorDispatcher(endpoints=endpoints, seed=seed)
        self._routing_requested = bool(adaptive_mirror_routing)
        self._single_mirror = len(urls) <= 1

    # ---------------------------------------------------------------- state

    @property
    def adaptive_chunks(self) -> bool:
        return self._adaptive_chunks

    @property
    def dispatcher(self) -> Optional[MirrorDispatcher]:
        return self._dispatcher

    @property
    def collector(self) -> NetworkMetricsCollector:
        return self._collector

    def snapshot(self) -> NetworkStateSnapshot:
        return self._collector.snapshot()

    # ---------------------------------------------------------------- chunks

    def plan_initial(self) -> list[ChunkState]:
        """
        Produce the first batch of chunks for a fresh transfer.

        With adaptive sizing off this is the whole file, matching the engine's
        historical behaviour exactly. With it on this is one batch; the engine
        calls :meth:`plan_more` as the queue drains.
        """
        if not self._adaptive_chunks:
            return _fixed_partition(self._file_size, self._fixed_chunk_size)
        return self.plan_more(self._plan_batch)

    def plan_more(self, count: Optional[int] = None) -> list[ChunkState]:
        """
        Issue up to ``count`` further chunks from the current network state.

        Returns an empty list once the file is fully covered. Each call re-reads
        the smoothed network state, so a transfer that speeds up or starts losing
        chunks changes the boundaries it plans from that point on.
        """
        if not self._adaptive_chunks or self._planner is None:
            return []
        wanted = self._plan_batch if count is None else max(1, count)
        state = self._collector.snapshot()
        # Until the estimators are primed there is nothing to adapt to, so the
        # planner is handed None and falls back to the configured default rather
        # than sizing from a half-formed estimate.
        effective = state if state.is_primed else None

        planned: list[ChunkState] = []
        with self._lock:
            for _ in range(wanted):
                spec = self._planner.next_chunk(effective)
                if spec is None:
                    break
                planned.append(
                    ChunkState(
                        index=spec.index,
                        start_byte=spec.start_byte,
                        end_byte=spec.end_byte,
                    )
                )
        return planned

    def disable_planning(self) -> None:
        """
        Stop issuing chunks, because something else owns the partition.

        A manifest's hashes are computed over its own chunk boundaries, so when
        one is supplied it defines the partition and the planner must not add to
        it. Mirror routing and metrics collection are unaffected.
        """
        self._adaptive_chunks = False
        self._planner = None

    def advance_to(self, offset: int) -> None:
        """
        Skip the planner past a region an earlier session already partitioned.

        Resume must not re-plan bytes the state file already describes, or the
        two partitions would disagree and the same bytes would be fetched under
        two different chunk indices. The planner is replayed forward rather than
        mutated, so its index and offset stay consistent with each other.
        """
        if not self._adaptive_chunks or self._planner is None or offset <= 0:
            return
        with self._lock:
            while not self._planner.is_exhausted and self._planner.next_offset < offset:
                if self._planner.next_chunk(None) is None:
                    break

    @property
    def planning_exhausted(self) -> bool:
        if not self._adaptive_chunks or self._planner is None:
            return True
        return self._planner.is_exhausted

    # ---------------------------------------------------------------- mirrors

    def select_mirror(self, exclude: Sequence[str] = ()) -> Optional[MirrorSelection]:
        """
        Choose where to send the next range request.

        Returns the origin when routing is disabled. When routing is enabled,
        returns None if every mirror is currently in breaker cooldown -- a wait
        condition, not a failure; the caller should sleep for
        ``seconds_until_retry`` and ask again.
        """
        if self._dispatcher is None:
            return MirrorSelection(url=self._origin_url)
        request = self._dispatcher.dispatch(exclude=exclude)
        if request is None:
            return None
        return MirrorSelection(url=self._dispatcher.url_for(request.mirror), request=request)

    @property
    def seconds_until_retry(self) -> float:
        return 0.0 if self._dispatcher is None else self._dispatcher.seconds_until_retry

    # ---------------------------------------------------------------- outcomes

    def record_success(
        self,
        selection: Optional[MirrorSelection],
        *,
        bytes_transferred: int,
        duration_seconds: float,
        rtt_seconds: Optional[float] = None,
    ) -> None:
        """Score a completed range request for both the sizer and the router."""
        if duration_seconds > 0 and bytes_transferred > 0:
            self._collector.record_transfer(
                TransferSample(
                    bytes_transferred=bytes_transferred,
                    duration_seconds=duration_seconds,
                    success=True,
                )
            )
        if rtt_seconds is not None and rtt_seconds > 0:
            self._collector.record_rtt(rtt_seconds)
        if self._dispatcher is not None and selection is not None and selection.request is not None:
            self._dispatcher.complete(
                selection.request,
                bytes_transferred=bytes_transferred,
                duration_seconds=duration_seconds,
            )

    def record_failure(self, selection: Optional[MirrorSelection]) -> None:
        """
        Score a failed range request.

        A failure feeds the chunk sizer too, not only the router: the drop
        probability it reads is exactly this ratio, and it is what shrinks the
        chunk when a path starts losing requests.
        """
        self._collector.record_failure()
        if self._dispatcher is not None and selection is not None and selection.request is not None:
            self._dispatcher.fail(selection.request)

    def abandon(self, selection: Optional[MirrorSelection]) -> None:
        """Retire an in-flight request that was cancelled rather than failed."""
        if self._dispatcher is not None and selection is not None and selection.request is not None:
            self._dispatcher.abandon(selection.request)

    def stats(self) -> dict:
        state = self._collector.snapshot()
        out: dict = {
            "adaptive_chunks": self._adaptive_chunks,
            "adaptive_mirrors": self._dispatcher is not None,
            "routing_requested": self._routing_requested,
            "single_mirror_pool": self._single_mirror,
            "rtt_seconds": state.rtt_seconds,
            "throughput_bps": state.throughput_bps,
            "drop_probability": state.drop_probability,
            "samples": state.samples,
            "failures": state.failures,
        }
        if self._dispatcher is not None:
            out["mirrors"] = self._dispatcher.stats()
        return out


def _fixed_partition(file_size: int, chunk_size: int) -> list[ChunkState]:
    """Equal-sized partition; the engine's behaviour when no policy is enabled."""
    if file_size <= 0:
        return []
    size = max(1, chunk_size)
    return [
        ChunkState(
            index=index,
            start_byte=start,
            end_byte=min(start + size, file_size) - 1,
        )
        for index, start in enumerate(range(0, file_size, size))
    ]
