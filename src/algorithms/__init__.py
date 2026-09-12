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
from src.algorithms.mirror_bandit import (
    DEFAULT_BACKOFF_MULTIPLIER,
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_EXPLORATION_RATE,
    DEFAULT_FAILURE_THRESHOLD,
    DEFAULT_HORIZON_ROUNDS,
    DEFAULT_MAX_COOLDOWN_SECONDS,
    DEFAULT_MIN_PEAK_SAMPLE_BYTES,
    DEFAULT_PEAK_DECAY,
    DEFAULT_WEIGHT_DECAY,
    ArmSelection,
    CircuitBreaker,
    CircuitState,
    DispatchedRequest,
    DispatchOutcome,
    EXP3Bandit,
    MirrorDispatcher,
    MirrorEndpoint,
    MirrorHealthMonitor,
    MirrorReward,
    MirrorStats,
    WeightUpdate,
    decay_for_horizon,
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
    # EXP3 mirror router
    "DEFAULT_EXPLORATION_RATE",
    "DEFAULT_WEIGHT_DECAY",
    "DEFAULT_HORIZON_ROUNDS",
    "ArmSelection",
    "WeightUpdate",
    "EXP3Bandit",
    "decay_for_horizon",
    # Mirror health and reward calculation
    "DEFAULT_FAILURE_THRESHOLD",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_BACKOFF_MULTIPLIER",
    "DEFAULT_MAX_COOLDOWN_SECONDS",
    "DEFAULT_PEAK_DECAY",
    "DEFAULT_MIN_PEAK_SAMPLE_BYTES",
    "CircuitState",
    "CircuitBreaker",
    "MirrorReward",
    "MirrorStats",
    "MirrorHealthMonitor",
    # Worker request dispatch
    "MirrorEndpoint",
    "DispatchedRequest",
    "DispatchOutcome",
    "MirrorDispatcher",
]
