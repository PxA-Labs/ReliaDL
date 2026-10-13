"""
Check the emulated mirror against real kernel shaping.

The experiments in this directory impair traffic in Python rather than with
`tc`/netem. That buys reproducibility, server-side measurement and byte-level
corruption, none of which netem provides -- but it also means every number rests
on the claim that the emulation behaves like a network. This script tests that
claim instead of asserting it.

Method
------
For each of a few (bandwidth, latency) points, measure a transfer twice:

* **emulated** -- a `FaultyMirror` with `bandwidth_bps` and `latency_seconds` set.
* **shaped** -- an unimpaired `FaultyMirror` with the same values applied to the
  loopback interface by `tc qdisc ... netem delay ... rate ...`.

If the emulation is faithful, goodput and wall-clock time agree within a stated
tolerance. Where they do not, the direction and size of the gap is reported, so
it can be quoted as a limitation rather than discovered by a reviewer.

Only bandwidth and latency are compared. Failure, truncation and corruption rates
are deliberately out of scope: netem cannot inject application-visible corruption
at all, which is the reason the emulator exists.

Requirements
------------
Linux, root (or `CAP_NET_ADMIN`), and `iproute2`. On any other platform this exits
with a clear message rather than pretending to have run.

Usage::

    sudo python -m benchmarks.validate_against_netem
    sudo python -m benchmarks.validate_against_netem --repetitions 5 --json out.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

MB = 1024 * 1024
INTERFACE = "lo"


@dataclass(frozen=True)
class Point:
    """One (bandwidth, latency) operating point to compare."""

    name: str
    bandwidth_bps: float
    latency_seconds: float

    @property
    def netem_rate(self) -> str:
        return f"{int(self.bandwidth_bps * 8 / 1000)}kbit"

    @property
    def netem_delay(self) -> str:
        # netem applies the delay per direction; halve it so a round trip matches
        # the emulator, which delays only the response.
        return f"{self.latency_seconds * 1000 / 2:.3f}ms"


POINTS: tuple[Point, ...] = (
    Point("8MBps-20ms", 8 * MB, 0.020),
    Point("20MBps-10ms", 20 * MB, 0.010),
    Point("40MBps-5ms", 40 * MB, 0.005),
)


def _preflight() -> str | None:
    """Return a reason this cannot run, or None if it can."""
    if platform.system() != "Linux":
        return (
            f"netem validation requires Linux; this is {platform.system()}. "
            "Run it on a Linux box or in WSL2 before presenting emulated results."
        )
    if shutil.which("tc") is None:
        return "`tc` not found; install iproute2."
    if hasattr(os, "geteuid") and os.geteuid() != 0:
        return "root (or CAP_NET_ADMIN) is required to change qdiscs; re-run with sudo."
    return None


def _tc(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tc", *args], capture_output=True, text=True)


def _clear_qdisc() -> None:
    _tc("qdisc", "del", "dev", INTERFACE, "root")


def _apply_qdisc(point: Point) -> None:
    _clear_qdisc()
    result = _tc(
        "qdisc", "add", "dev", INTERFACE, "root", "netem",
        "delay", point.netem_delay,
        "rate", point.netem_rate,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tc failed: {result.stderr.strip()}")


def _measure(bandwidth_bps: float | None, latency_seconds: float, size: int, seed: int) -> float:
    """One transfer; returns seconds elapsed."""
    import asyncio
    import hashlib
    import random
    import tempfile

    from benchmarks.mirror_server import FaultyMirror, MirrorProfile
    from reliadl.download_engine import DownloadEngine
    from reliadl.models import DownloadConfig

    payload = random.Random(seed).randbytes(size)
    digest = hashlib.sha256(payload).hexdigest()
    profile = MirrorProfile(
        name="probe", bandwidth_bps=bandwidth_bps, latency_seconds=latency_seconds
    )
    with FaultyMirror(payload, profile, seed=seed) as mirror:
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "out.bin"
            config = DownloadConfig(
                chunk_size_bytes=4 * MB,
                max_parallel_workers=4,
                verify_on_complete=False,
            )
            started = time.perf_counter()
            asyncio.run(DownloadEngine(config).download(mirror.url, output, expected_hash=digest))
            return time.perf_counter() - started


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the emulator against netem")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--size-mb", type=int, default=32)
    parser.add_argument("--tolerance", type=float, default=0.25,
                        help="Relative gap treated as agreement (default 0.25)")
    parser.add_argument("--json", help="Write the comparison here")
    args = parser.parse_args(argv)

    problem = _preflight()
    if problem:
        print(f"SKIPPED: {problem}", file=sys.stderr)
        return 2

    size = args.size_mb * MB
    rows = []
    try:
        for point in POINTS:
            emulated, shaped = [], []
            for rep in range(args.repetitions):
                seed = 7000 + rep
                emulated.append(_measure(point.bandwidth_bps, point.latency_seconds, size, seed))
                _apply_qdisc(point)
                try:
                    shaped.append(_measure(None, 0.0, size, seed))
                finally:
                    _clear_qdisc()

            em, sh = statistics.median(emulated), statistics.median(shaped)
            gap = abs(em - sh) / sh if sh else float("inf")
            rows.append({
                "point": point.name,
                "emulated_seconds": round(em, 3),
                "netem_seconds": round(sh, 3),
                "relative_gap": round(gap, 3),
                "agrees": gap <= args.tolerance,
                "emulated_mbps": round(size / MB / em, 2) if em else 0.0,
                "netem_mbps": round(size / MB / sh, 2) if sh else 0.0,
            })
    finally:
        _clear_qdisc()

    print(f"\n{args.size_mb} MB transfer, median of {args.repetitions} runs, "
          f"tolerance {args.tolerance:.0%}\n")
    print(f"  {'point':14} {'emulated':>12} {'netem':>12} {'gap':>8}  verdict")
    print(f"  {'-' * 14} {'-' * 12} {'-' * 12} {'-' * 8}  -------")
    for row in rows:
        verdict = "agrees" if row["agrees"] else "DIFFERS"
        print(f"  {row['point']:14} {row['emulated_mbps']:9.2f}MB/s "
              f"{row['netem_mbps']:9.2f}MB/s {row['relative_gap']:7.1%}  {verdict}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")

    disagreements = [r for r in rows if not r["agrees"]]
    if disagreements:
        print(f"\n{len(disagreements)} of {len(rows)} points disagree beyond tolerance.")
        print("Report this as a limitation rather than suppressing it: the emulator")
        print("is a model, and where it diverges is exactly what a reader needs told.")
        return 1
    print("\nThe emulator tracks kernel shaping at every point tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
