"""
Observability subsystem for ReliaDL.

Exposes transfer state to a metrics scraper without the engine having to know
anything about how it is collected.
"""

from __future__ import annotations

from src.telemetry.metrics import (
    DEFAULT_DURATION_BUCKETS,
    DEFAULT_THROUGHPUT_BUCKETS,
    Counter,
    DownloadMetrics,
    Gauge,
    Histogram,
    MetricsRegistry,
    MetricsServer,
    escape_help,
    escape_label_value,
)

__all__ = [
    "DEFAULT_DURATION_BUCKETS",
    "DEFAULT_THROUGHPUT_BUCKETS",
    "Counter",
    "Gauge",
    "Histogram",
    "MetricsRegistry",
    "MetricsServer",
    "DownloadMetrics",
    "escape_help",
    "escape_label_value",
]
