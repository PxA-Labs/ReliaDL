"""
Compare ReliaDL against other download tools on identical conditions.

Usage::

    python -m benchmarks.run_baselines
    python -m benchmarks.run_baselines --scenario lossy --repetitions 5

Each repetition serves the same payload from the same mirror profile to every
tool in turn, so a comparison is between tools rather than between runs. Missing
tools are reported as missing: a result table that silently omits aria2 is worse
than one that says aria2 was not installed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

from benchmarks.baselines import TOOLS, available_tools, run_tool
from benchmarks.mirror_server import FaultyMirror
from benchmarks.runner import quiet_engine_logs
from benchmarks.scenarios import SCENARIOS_BY_NAME
from reliadl.download_engine import DownloadEngine
from reliadl.models import DownloadConfig

MB = 1024 * 1024


def _reliadl_run(url: str, digest: str, size: int, workers: int, chunk: int) -> tuple[bool, float]:
    quiet_engine_logs()
    with tempfile.TemporaryDirectory() as tmp:
        output = Path(tmp) / "out.bin"
        config = DownloadConfig(
            chunk_size_bytes=chunk,
            max_parallel_workers=workers,
            max_retries_per_chunk=8,
            retry_base_delay_seconds=0.01,
        )
        started = time.perf_counter()
        try:
            asyncio.run(DownloadEngine(config).download(url, output, expected_hash=digest))
        except Exception:
            return False, time.perf_counter() - started
        elapsed = time.perf_counter() - started
        ok = output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest() == digest
        return ok, elapsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ReliaDL against other downloaders")
    parser.add_argument("--scenario", default="clean")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--chunk-mb", type=int, default=4)
    parser.add_argument("--base-seed", type=int, default=5000)
    parser.add_argument("--json", help="Write raw results here")
    args = parser.parse_args(argv)

    if args.scenario not in SCENARIOS_BY_NAME:
        parser.error(f"unknown scenario {args.scenario!r}")
    scenario = SCENARIOS_BY_NAME[args.scenario]

    found = available_tools()
    missing = [name for name, path in found.items() if path is None]
    print(f"\nScenario: {scenario.name} -- {scenario.description}")
    print(f"Payload: {scenario.file_size // MB} MB, {args.workers} workers, "
          f"{args.repetitions} repetitions\n")
    for name, path in found.items():
        print(f"  {name:8} {'found' if path else 'NOT INSTALLED'}")
    if missing:
        print(f"\n  {len(missing)} tool(s) not installed: {', '.join(missing)}.")
        print("  Their rows below say so. Do not read a missing tool as a slow one.")
    print()

    chunk = args.chunk_mb * MB
    timings: dict[str, list[float]] = {"reliadl": []}
    failures: dict[str, str] = {}
    raw: list[dict] = []

    for rep in range(args.repetitions):
        seed = args.base_seed + rep
        payload = random.Random(seed).randbytes(scenario.file_size)
        digest = hashlib.sha256(payload).hexdigest()
        # The first mirror only: these tools take a single URL, so a multi-mirror
        # scenario would not be a like-for-like comparison.
        mirror = FaultyMirror(payload, scenario.mirrors[0], seed=seed).start()
        try:
            ok, elapsed = _reliadl_run(
                mirror.url, digest, scenario.file_size, args.workers, chunk
            )
            if ok:
                timings["reliadl"].append(elapsed)
            else:
                failures["reliadl"] = "transfer failed or hash mismatch"
            raw.append({"tool": "reliadl", "seed": seed, "ok": ok, "seconds": elapsed})

            # Bound as a default argument rather than captured, so the closure
            # reads this iteration's mirror rather than whichever one the loop
            # variable happens to hold when it is finally called.
            def written(_mirror: FaultyMirror = mirror) -> int:
                return _mirror.stats.bytes_written

            for tool in TOOLS:
                result = run_tool(
                    tool, mirror.url, digest, scenario.file_size,
                    workers=args.workers, chunk_size=chunk,
                    mirrors_bytes_written=written,
                )
                raw.append(result.as_dict())
                if not result.available:
                    failures[tool.name] = "not installed"
                elif result.ok and result.verified:
                    timings.setdefault(tool.name, []).append(result.seconds)
                else:
                    failures[tool.name] = result.error or "failed"
        finally:
            mirror.stop()

    print(f"  {'tool':10} {'median':>10} {'goodput':>12}  notes")
    print(f"  {'-' * 10} {'-' * 10} {'-' * 12}  -----")
    size_mb = scenario.file_size / MB
    ordered = sorted(timings.items(), key=lambda kv: statistics.median(kv[1]) if kv[1] else 1e9)
    for name, values in ordered:
        if not values:
            continue
        median = statistics.median(values)
        note = "parallel" if name == "reliadl" or any(
            t.name == name and t.parallel for t in TOOLS
        ) else "single stream"
        print(f"  {name:10} {median:9.2f}s {size_mb / median:9.1f} MB/s  {note}")
    for name, reason in sorted(failures.items()):
        if name not in timings or not timings.get(name):
            print(f"  {name:10} {'-':>10} {'-':>12}  {reason}")

    if args.json:
        Path(args.json).write_text(json.dumps(raw, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    if missing:
        print("\nThis comparison is incomplete while those tools are missing.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
