"""
The concurrency-floor A/B, as a committed script.

The claim this measures is the project's one piece of original modelling: the
square-root chunk-size optimum is derived for a single sequential stream, and a
parallel downloader that follows it unconstrained grows the chunk until fewer
chunks remain than there are workers, starving the pool.

Why this file exists
--------------------
The numbers were previously hard-coded into the figure script, so the figure
could not drift from the data only in the sense that it had no data: nothing
regenerated them. They were also measured against a controller that has since
been corrected (the loss term carried a stray factor of 8). This runs the
comparison instead of asserting it.

Method
------
Both arms run in one process, interleaved seed by seed, so drift in machine load
falls on both rather than on whichever ran second. The only difference between
arms is whether ``TransferScheduler._apply_concurrency_floor`` is active; the
"off" arm replaces it with the identity, which is exactly the unconstrained
controller. Everything else -- payload, impairments, worker count -- is fixed by
the seed and the scenario.

Usage::

    python -m benchmarks.ablations.concurrency_floor
    python -m benchmarks.ablations.concurrency_floor --repetitions 5 --json out.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Optional

from benchmarks.runner import run_one
from benchmarks.scenarios import SCENARIOS, Policy
from reliadl.scheduling import TransferScheduler

MB = 1024 * 1024

# Adaptive sizing is the whole point: with a fixed chunk size the floor never
# binds, because a fixed size is already below remaining/workers for most of the
# transfer.
POLICY = Policy(
    name="adaptive-chunks",
    description="BL-DCS sizing, origin only",
    adaptive_chunks=True,
)

DEFAULT_SCENARIOS = ("clean", "heterogeneous", "failover", "lossy")


def _scenario(name: str):
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise SystemExit(f"unknown scenario: {name}")


def _median_goodput(results) -> Optional[float]:
    values = [r.goodput_bps / MB for r in results if r.ok and r.verified]
    return statistics.median(values) if values else None


def run(scenario_names, repetitions: int, base_seed: int):
    original = TransferScheduler._apply_concurrency_floor
    rows = []

    for name in scenario_names:
        scenario = _scenario(name)
        arms: dict[str, list] = {"off": [], "on": []}

        for rep in range(repetitions):
            seed = base_seed + rep
            # Interleaved: the two arms for one seed run back to back, so a
            # transient on this machine cannot land on one arm only.
            for arm in ("off", "on"):
                if arm == "off":
                    TransferScheduler._apply_concurrency_floor = (
                        lambda self, spec: spec  # type: ignore[assignment]
                    )
                else:
                    TransferScheduler._apply_concurrency_floor = original  # type: ignore[assignment]
                try:
                    arms[arm].append(run_one(scenario, POLICY, seed))
                finally:
                    TransferScheduler._apply_concurrency_floor = original  # type: ignore[assignment]

        off = _median_goodput(arms["off"])
        on = _median_goodput(arms["on"])
        rows.append({
            "scenario": name,
            "floor_off_mbps": off,
            "floor_on_mbps": on,
            "change_pct": None if not off or not on else (on / off - 1.0) * 100.0,
            "runs_off": sum(1 for r in arms["off"] if r.ok and r.verified),
            "runs_on": sum(1 for r in arms["on"] if r.ok and r.verified),
            "repetitions": repetitions,
        })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Concurrency-floor A/B")
    parser.add_argument("--repetitions", type=int, default=5)
    parser.add_argument("--base-seed", type=int, default=7000)
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--json", help="Write raw rows here")
    args = parser.parse_args()

    names = args.scenarios or list(DEFAULT_SCENARIOS)
    rows = run(names, args.repetitions, args.base_seed)

    print(f"\nConcurrency floor A/B -- {args.repetitions} seeds per arm, interleaved")
    print("Median goodput over runs that completed and verified.\n")
    print(f"  {'scenario':16s} {'floor off':>11s} {'floor on':>11s} {'change':>9s}  {'completed':>11s}")
    print(f"  {'-' * 16} {'-' * 11} {'-' * 11} {'-' * 9}  {'-' * 11}")
    for row in rows:
        off = "-" if row["floor_off_mbps"] is None else f"{row['floor_off_mbps']:.1f} MB/s"
        on = "-" if row["floor_on_mbps"] is None else f"{row['floor_on_mbps']:.1f} MB/s"
        change = "-" if row["change_pct"] is None else f"{row['change_pct']:+.0f}%"
        completed = f"{row['runs_off']}/{row['repetitions']} vs {row['runs_on']}/{row['repetitions']}"
        print(f"  {row['scenario']:16s} {off:>11s} {on:>11s} {change:>9s}  {completed:>11s}")

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
