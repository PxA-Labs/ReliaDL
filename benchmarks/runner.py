"""
Executes one measurement: a (scenario, policy, seed) triple, start to finish.

Every run is independent -- fresh mirrors, fresh payload derived from the seed,
fresh output file -- so runs can be reordered or re-run individually without
changing their results. The metric that matters most is ``wasted_bytes``: bytes
the mirrors wrote that were not part of the delivered file. It is measured at the
server, not inferred from the client, because the client cannot see bytes it
abandoned.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from benchmarks.mirror_server import FaultyMirror
from benchmarks.scenarios import Policy, Scenario
from reliadl.download_engine import DownloadEngine
from reliadl.logger import configure_logger
from reliadl.manifest import DEFAULT_SUB_BLOCK_SIZE, calculate_sub_blocks, compute_merkle_root
from reliadl.models import DownloadConfig


@dataclass
class RunResult:
    """One measurement. Serialized verbatim into the results file."""

    scenario: str
    policy: str
    seed: int
    ok: bool
    verified: bool
    error: Optional[str] = None

    seconds: float = 0.0
    file_size: int = 0
    goodput_bps: float = 0.0

    bytes_written: int = 0
    """Bytes the mirrors wrote, summed. Includes everything the client discarded."""
    wasted_bytes: int = 0
    """bytes_written - file_size. The cost of every retry and abandoned stream."""
    waste_ratio: float = 0.0

    chunks: int = 0
    chunk_sizes: list[int] = field(default_factory=list)
    requests: int = 0
    failures_injected: int = 0
    corruptions_injected: int = 0
    truncations_injected: int = 0

    per_mirror_bytes: dict = field(default_factory=dict)
    mirror_probabilities: dict = field(default_factory=dict)
    corrupting_mirror_share: Optional[float] = None
    """
    Fraction of delivered bytes that came from a mirror known to corrupt.

    The headline number for the byzantine scenario: a policy that routes on
    throughput alone is drawn *towards* a fast corrupting mirror, so a lower share
    is the thing an integrity-aware policy should achieve.
    """

    def as_dict(self) -> dict:
        return asdict(self)


def _payload(size: int, seed: int) -> bytes:
    """Deterministic payload so a seed fully determines a run."""
    rng = random.Random(seed)
    return rng.randbytes(size)


def _write_manifest(
    path: Path,
    payload: bytes,
    chunk_size: int,
    *,
    sub_blocks: bool,
) -> Path:
    entries = []
    for index, start in enumerate(range(0, len(payload), chunk_size)):
        end = min(start + chunk_size, len(payload)) - 1
        data = payload[start:end + 1]
        entry = {
            "index": index,
            "start_byte": start,
            "end_byte": end,
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        }
        if sub_blocks:
            entry["sub_blocks"] = calculate_sub_blocks(data, DEFAULT_SUB_BLOCK_SIZE)
        entries.append(entry)

    document = {
        "manifest_version": "1.0.0",
        "artifact_metadata": {
            "filename": "payload.bin",
            "file_size_bytes": len(payload),
            "file_hash_sha256": hashlib.sha256(payload).hexdigest(),
        },
        "chunking_topology": {
            "default_chunk_size_bytes": chunk_size,
            "total_chunks": len(entries),
            "hash_algorithm": "sha256",
            "merkle_tree_root": compute_merkle_root([e["sha256"] for e in entries]),
        },
        "chunks": entries,
    }
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def quiet_engine_logs() -> None:
    """
    Silence the engine's own logging for the duration of a measurement.

    Per-chunk retry logging is useful when debugging a transfer and ruinous when
    running a matrix: it buries the result lines, and writing it costs time that
    lands in the measurement. Errors still surface, because a failed run is
    recorded in the result rather than read from the log.
    """
    configure_logger(level="CRITICAL", format_type="text")


def run_one(scenario: Scenario, policy: Policy, seed: int, *, timeout: float = 300.0) -> RunResult:
    """Run a single measurement and return it, including failures."""
    quiet_engine_logs()
    payload = _payload(scenario.file_size, seed)
    digest = hashlib.sha256(payload).hexdigest()

    result = RunResult(
        scenario=scenario.name,
        policy=policy.name,
        seed=seed,
        ok=False,
        verified=False,
        file_size=len(payload),
    )

    mirrors = [
        FaultyMirror(payload, profile, seed=seed + i).start()
        for i, profile in enumerate(scenario.mirrors)
    ]
    tmp = tempfile.TemporaryDirectory()
    try:
        workdir = Path(tmp.name)
        output = workdir / "out.bin"

        manifest_path: Optional[Path] = None
        if policy.sub_block_digests:
            manifest_path = _write_manifest(
                workdir / "manifest.json", payload, policy.chunk_size, sub_blocks=True
            )

        config = DownloadConfig(
            chunk_size_bytes=policy.chunk_size,
            max_parallel_workers=policy.workers,
            adaptive_chunk_sizing=policy.adaptive_chunks,
            adaptive_mirror_routing=policy.adaptive_mirrors,
            mirrors=[m.url for m in mirrors[1:]] if policy.adaptive_mirrors else [],
            manifest_path=str(manifest_path) if manifest_path else None,
            max_retries_per_chunk=8,
            retry_base_delay_seconds=0.01,
            retry_jitter_factor=0.0,
            verify_on_complete=True,
        )

        engine = DownloadEngine(config)
        started = time.perf_counter()
        try:
            asyncio.run(
                asyncio.wait_for(
                    engine.download(mirrors[0].url, output, expected_hash=digest),
                    timeout=timeout,
                )
            )
            result.ok = True
        except Exception as exc:  # recorded, not raised: a failed cell is data
            result.error = f"{type(exc).__name__}: {exc}"
        result.seconds = time.perf_counter() - started

        if result.ok and output.is_file():
            result.verified = hashlib.sha256(output.read_bytes()).hexdigest() == digest
            result.goodput_bps = len(payload) / result.seconds if result.seconds else 0.0

        if engine._state is not None:
            result.chunks = len(engine._state.chunks)
            result.chunk_sizes = sorted({c.size for c in engine._state.chunks})

        total_written = 0
        for mirror, profile in zip(mirrors, scenario.mirrors, strict=True):
            stats = mirror.stats
            total_written += stats.bytes_written
            result.requests += stats.ranged_requests
            result.failures_injected += stats.failures_injected
            result.corruptions_injected += stats.corruptions_injected
            result.truncations_injected += stats.truncations_injected
            result.per_mirror_bytes[profile.name] = stats.bytes_written

        result.bytes_written = total_written
        result.wasted_bytes = max(0, total_written - len(payload))
        result.waste_ratio = result.wasted_bytes / len(payload) if payload else 0.0

        corrupting = [
            p.name for p in scenario.mirrors if p.corruption_rate > 0
        ]
        if corrupting and total_written:
            from_corrupting = sum(result.per_mirror_bytes.get(n, 0) for n in corrupting)
            result.corrupting_mirror_share = from_corrupting / total_written

        scheduler = engine._scheduler
        if scheduler is not None and scheduler.dispatcher is not None:
            result.mirror_probabilities = {
                arm: round(p, 4)
                for arm, p in scheduler.dispatcher.bandit.probabilities().items()
            }
        return result
    finally:
        for mirror in mirrors:
            mirror.stop()
        tmp.cleanup()
