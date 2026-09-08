"""
ReliaDL Core Package.
Fault-tolerant, high-throughput chunked download engine.
"""

from __future__ import annotations

from src.models import (
    ChunkResult,
    ChunkSpec,
    ChunkState,
    ChunkStatus,
    DownloadConfig,
    DownloadResult,
    DownloadState,
    DownloadStatistics,
    DownloadStatus,
    ProgressReport,
    VerificationResult,
)

__all__ = [
    "ChunkStatus",
    "DownloadStatus",
    "ChunkSpec",
    "ChunkResult",
    "ChunkState",
    "DownloadStatistics",
    "DownloadState",
    "DownloadResult",
    "VerificationResult",
    "ProgressReport",
    "DownloadConfig",
]
