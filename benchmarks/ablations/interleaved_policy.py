"""
Interleaved paired comparison of two policies on one scenario.

Why this exists rather than reusing the matrix
----------------------------------------------
`run_all` executes a whole cell before starting the next: all repetitions of
policy A, then all repetitions of policy B. Faults are seed-derived, so seed s
under A and seed s under B meet identical impairments -- but they do not run at
the same moment, and on a shared machine the moment matters. Any drift in
machine state over the minutes a cell takes is therefore confounded with the
policy, and a paired analysis of that output silently inherits the confound.

This was not hypothetical. A 30-seed matrix run showed `adaptive-chunks` on the
heterogeneous scenario sitting at ~28 MB/s for nine consecutive runs and then
stepping to ~48 MB/s for the remaining twenty-one, while the `baseline` cell --
which had already finished -- stayed flat at ~28 throughout. Paired across cells
that reads as a 51% improvement with p = 0.0014. It is a step change inside one
cell that no per-seed property explains.

So: run the two arms back to back on each seed. Drift that lands between seeds
hits both arms equally and cancels in the per-pair difference; only drift within
a single pair survives, and that window is two transfers rather than a whole
cell. This is the same discipline `concurrency_floor.py` uses, for the same
reason.

What this does and does not fix
-------------------------------
It fixes the confound between policy and wall-clock position. It does not make
the loopback testbed representative of a wide-area path, and it does not help
with byte-count metrics -- completion and waste are counts, unaffected by timing
drift, and the matrix is the right source for those.

Usage::

    python -m benchmarks.ablations.interleaved_policy
    python -m benchmarks.ablations.interleaved_policy --a baseline --b adaptive-chunks \\
        --scenario clean --scenario lossy --repetitions 30 --json out.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from benchmarks.paired import bootstrap_ci, sign_test
from benchmarks.runner import run_one
from benchmarks.scenarios import POLICIES, SCENARIOS

MB = 1024 * 1024
DEFAULT_SCENARIOS = ("clean", "lossy", "heterogeneous", "failover")


def _scenario(name: str):
    for scenario in SCENARIOS:
        if scenario.name == name:
            return scenario
    raise SystemExit(f"unknown scenario: {name}")


def _policy(name: str):
    for policy in POLICIES:
        if policy.name == name:
            return policy
    raise SystemExit(f"unknown policy: {name}")


def run(scenario_names, policy_a, policy_b, repetitions: int, base_seed: int):
    rows = []
    for name in scenario_names:
        scenario = _scenario(name)
        pairs, dropped = [], 0

        for rep in range(repetitions):
            seed = base_seed + rep
            # Back to back on the same seed. Order is fixed rather than
            # alternated: alternating would trade a constant offset for a
            # varying one, and a constant offset is the easier thing to reason
            # about when it shows up in the interval.
            ra = run_one(scenario, policy_a, seed)
            rb = run_one(scenario, policy_b, seed)
            if ra.ok and ra.verified and rb.ok and rb.verified:
                pairs.append((ra.goodput_bps / MB, rb.goodput_bps / MB))
            else:
                dropped += 1

        if len(pairs) < 2:
            rows.append({
                "scenario": name, "a": policy_a.name, "b": policy_b.name,
                "pairs": len(pairs), "dropped": dropped, "insufficient": True,
            })
            continue

        rel = [(b - a) / a * 100.0 for a, b in pairs]
        lo, hi = bootstrap_ci(rel)
        pos, neg, ties, p = sign_test([b - a for a, b in pairs])
        rows.append({
            "scenario": name, "a": policy_a.name, "b": policy_b.name,
            "pairs": len(pairs), "dropped": dropped,
            "median_a_mbps": sorted(a for a, _ in pairs)[len(pairs) // 2],
            "median_b_mbps": sorted(b for _, b in pairs)[len(pairs) // 2],
            "mean_rel_pct": sum(rel) / len(rel),
            "ci95_rel_pct": [lo, hi],
            "b_faster_in": pos, "a_faster_in": neg, "ties": ties,
            "sign_test_p": p,
            "insufficient": False,
        })
    return rows


def _verdict(row: dict) -> str:
    lo, hi = row["ci95_rel_pct"]
    if lo > 0:
        return f"B faster, CI excludes 0 (p={row['sign_test_p']:.4f})"
    if hi < 0:
        return f"B slower, CI excludes 0 (p={row['sign_test_p']:.4f})"
    return f"no consistent direction (p={row['sign_test_p']:.3f})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Interleaved paired policy comparison")
    parser.add_argument("--a", default="baseline")
    parser.add_argument("--b", default="adaptive-chunks")
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--repetitions", type=int, default=30)
    parser.add_argument("--base-seed", type=int, default=9000)
    parser.add_argument("--json")
    args = parser.parse_args(argv)

    names = args.scenarios or list(DEFAULT_SCENARIOS)
    rows = run(names, _policy(args.a), _policy(args.b), args.repetitions, args.base_seed)

    print(f"\nInterleaved paired comparison: {args.b} against {args.a}")
    print(f"{args.repetitions} seeds, arms run back to back per seed so drift falls on both.\n")
    head = f"  {'scenario':16s} {'pairs':>6s} {'A MB/s':>8s} {'B MB/s':>8s} {'mean diff':>10s} {'95% CI':>16s}  verdict"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for r in rows:
        if r["insufficient"]:
            print(f"  {r['scenario']:16s} {r['pairs']:>6d} {'-':>8s} {'-':>8s} {'-':>10s} {'-':>16s}  too few complete pairs")
            continue
        lo, hi = r["ci95_rel_pct"]
        print(
            f"  {r['scenario']:16s} {r['pairs']:>6d} {r['median_a_mbps']:>8.1f} {r['median_b_mbps']:>8.1f} "
            f"{r['mean_rel_pct']:>+9.1f}% {f'[{lo:+.1f}, {hi:+.1f}]':>16s}  {_verdict(r)}"
        )

    if args.json:
        Path(args.json).write_text(json.dumps(rows, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
