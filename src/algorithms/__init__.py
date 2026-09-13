"""
Adaptive control and scheduling algorithms for ReliaDL.

Hosts the closed-loop controllers that drive transfer decisions at runtime:
network state estimation, dynamic chunk sizing, and worker scheduling.
"""

from __future__ import annotations

from src.algorithms.adaptive_chunker import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_EPSILON,
    DEFAULT_GAMMA,
    DEFAULT_LAMBDA,
    DEFAULT_MSS_BYTES,
    DEFAULT_OVERHEAD_SECONDS,
    MAX_CHUNK_SIZE,
    MIN_CHUNK_SIZE,
    BLDCSController,
    ChunkSizingDecision,
    DynamicChunkPlanner,
    align_to_power_of_two,
    is_power_of_two,
)
from src.algorithms.task_deque import (
    DEFAULT_INITIAL_CAPACITY,
    DEFAULT_STEAL_ATTEMPTS,
    ChaseLevDeque,
    StealResult,
    StealStatus,
)
from src.algorithms.metrics_collector import (
    DEFAULT_DEVIATION_ALPHA,
    DEFAULT_FAILURE_WINDOW_SIZE,
    DEFAULT_RTT_ALPHA,
    DEFAULT_THROUGHPUT_BETA,
    EWMAEstimator,
    FailureWindow,
    NetworkMetricsCollector,
    NetworkStateSnapshot,
    TransferSample,
)

__all__ = [
    "DEFAULT_RTT_ALPHA",
    "DEFAULT_THROUGHPUT_BETA",
    "DEFAULT_DEVIATION_ALPHA",
    "DEFAULT_FAILURE_WINDOW_SIZE",
    "EWMAEstimator",
    "FailureWindow",
    "TransferSample",
    "NetworkStateSnapshot",
    "NetworkMetricsCollector",
    # BL-DCS controller
    "DEFAULT_GAMMA",
    "DEFAULT_LAMBDA",
    "DEFAULT_OVERHEAD_SECONDS",
    "DEFAULT_EPSILON",
    "DEFAULT_MSS_BYTES",
    "DEFAULT_CHUNK_SIZE",
    "MIN_CHUNK_SIZE",
    "MAX_CHUNK_SIZE",
    "BLDCSController",
    "ChunkSizingDecision",
    "DynamicChunkPlanner",
    "align_to_power_of_two",
    "is_power_of_two",
    # SR-WSRS work-stealing scheduler
    "DEFAULT_INITIAL_CAPACITY",
    "DEFAULT_STEAL_ATTEMPTS",
    "ChaseLevDeque",
    "StealResult",
    "StealStatus",
]
