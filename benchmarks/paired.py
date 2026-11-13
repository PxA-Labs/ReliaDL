"""
Paired comparison between two policies on the same scenario.

Why pairing is legitimate here
------------------------------
Impairments are drawn from ``blake2b(seed, byte_range, attempt)``, so they depend
on which bytes are requested and not on when the request arrives. Two policies
run at the same seed therefore meet the *same* faults. That makes a run under
policy A and a run under policy B at seed s a matched pair, and the right
question is not "do these two samples overlap" but "what is the per-seed
difference, and is it consistently signed".

This matters because the unpaired view is badly underpowered here. Between-seed
variance is large -- a retry storm on one seed moves that run several-fold --
while the within-pair difference is small and systematic. Comparing medians with
interquartile ranges throws away the pairing and hides a real effect inside the
seed-to-seed spread, which is exactly what happened when the chunk-sizing result
was reported over five seeds.

What is reported
----------------
* the number of usable pairs, and how many were dropped because one side did not
  complete (an incomplete run has no goodput to compare);
* the median and mean of the per-pair relative difference;
* a percentile bootstrap 95% confidence interval on the mean difference,
  resampled with a fixed seed so the interval is reproducible;
* an exact two-sided sign test, which assumes nothing about the shape of the
  difference distribution -- only that under the null a difference is equally
  likely to fall either way.

The sign test is deliberately conservative: it uses only the direction of each
difference, not its size. A result that survives it is not resting on the
distributional assumptions a t-test would need.

Usage::

    python -m benchmarks.paired
    python -m benchmarks.paired --a baseline --b adaptive-chunks
    python -m benchmarks.paired --results path/to/results.jsonl --json out.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
from pathlib import Path
from typing import Optional

MB = 1024 * 1024
DEFAULT_RESULTS = Path("benchmarks/results/results.jsonl")
BOOTSTRAP_RESAMPLES = 20000
BOOTSTRAP_SEED = 20260101


def load(path: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _usable(row: dict) -> bool:
    """A run contributes a goodput number only if it finished and verified."""
    return bool(row.get("ok")) and bool(row.get("verified"))


def pairs_for(rows: list[dict], scenario: str, policy_a: str, policy_b: str):
    """Match runs of two policies by seed, keeping only pairs usable on both sides."""
    by_policy: dict[str, dict[int, dict]] = {policy_a: {}, policy_b: {}}
    for row in rows:
        if row.get("scenario") != scenario:
            continue
        if row.get("policy") in by_policy:
            by_policy[row["policy"]][row["seed"]] = row

    seeds = sorted(set(by_policy[policy_a]) & set(by_policy[policy_b]))
    kept, dropped = [], 0
    for seed in seeds:
        ra, rb = by_policy[policy_a][seed], by_policy[policy_b][seed]
        if _usable(ra) and _usable(rb):
            kept.append((seed, ra["goodput_bps"] / MB, rb["goodput_bps"] / MB))
        else:
            dropped += 1
    return kept, dropped, len(seeds)


def bootstrap_ci(values: list[float], confidence: float = 0.95) -> tuple[float, float]:
    """Percentile bootstrap interval on the mean. Fixed seed, so it reproduces."""
    if len(values) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(BOOTSTRAP_SEED)
    n = len(values)
    means = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = (1.0 - confidence) / 2.0
    return means[int(lo * BOOTSTRAP_RESAMPLES)], means[int((1.0 - lo) * BOOTSTRAP_RESAMPLES) - 1]


def sign_test(differences: list[float]) -> tuple[int, int, int, float]:
    """
    Exact two-sided sign test.

    Ties contribute no directional evidence and are excluded from n, which is the
    standard treatment and is conservative: it shrinks the sample rather than
    splitting ties in whichever direction would help.
    """
    pos = sum(1 for d in differences if d > 0)
    neg = sum(1 for d in differences if d < 0)
    ties = len(differences) - pos - neg
    n = pos + neg
    if n == 0:
        return pos, neg, ties, 1.0
    k = min(pos, neg)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / (2.0**n)
    return pos, neg, ties, min(1.0, 2.0 * tail)


def compare(rows: list[dict], scenario: str, policy_a: str, policy_b: str) -> Optional[dict]:
    kept, dropped, total = pairs_for(rows, scenario, policy_a, policy_b)
    if len(kept) < 2:
        return {
            "scenario": scenario, "a": policy_a, "b": policy_b,
            "pairs": len(kept), "dropped": dropped, "seeds_seen": total,
            "insufficient": True,
        }

    rel = [(b - a) / a * 100.0 for _, a, b in kept]
    abs_diff = [b - a for _, a, b in kept]
    lo, hi = bootstrap_ci(rel)
    pos, neg, ties, p = sign_test(abs_diff)
    return {
        "scenario": scenario, "a": policy_a, "b": policy_b,
        "pairs": len(kept), "dropped": dropped, "seeds_seen": total,
        "median_a_mbps": statistics.median([a for _, a, _ in kept]),
        "median_b_mbps": statistics.median([b for _, _, b in kept]),
        "mean_rel_pct": statistics.mean(rel),
        "median_rel_pct": statistics.median(rel),
        "ci95_rel_pct": [lo, hi],
        "b_faster_in": pos, "a_faster_in": neg, "ties": ties,
        "sign_test_p": p,
        "insufficient": False,
    }


def _verdict(row: dict) -> str:
    """
    Turn the interval into a sentence, without overstating it.

    A confidence interval that straddles zero is reported as exactly that: no
    consistent direction. It is not evidence of equivalence, which would need an
    equivalence bound stated in advance.
    """
    lo, hi = row["ci95_rel_pct"]
    if lo > 0:
        return f"B faster, CI excludes 0 (p={row['sign_test_p']:.4f})"
    if hi < 0:
        return f"B slower, CI excludes 0 (p={row['sign_test_p']:.4f})"
    return f"no consistent direction (p={row['sign_test_p']:.3f})"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Paired policy comparison")
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--a", default="baseline", help="Reference policy")
    parser.add_argument("--b", default="adaptive-chunks", help="Policy under test")
    parser.add_argument("--scenario", action="append", dest="scenarios")
    parser.add_argument("--json", help="Write raw rows here")
    args = parser.parse_args(argv)

    rows = load(args.results)
    scenarios = args.scenarios or sorted(
        {r["scenario"] for r in rows if r.get("policy") in (args.a, args.b)}
    )

    out = []
    for scenario in scenarios:
        row = compare(rows, scenario, args.a, args.b)
        if row:
            out.append(row)

    usable = [r for r in out if not r["insufficient"]]
    print(f"\nPaired comparison: {args.b} against {args.a}")
    print("Per-seed differences; faults are seed-determined, so each pair met identical impairments.")
    print(f"Bootstrap: {BOOTSTRAP_RESAMPLES:,} resamples, seed {BOOTSTRAP_SEED}. Sign test is exact and two-sided.\n")
    head = f"  {'scenario':16s} {'pairs':>6s} {'A MB/s':>8s} {'B MB/s':>8s} {'mean diff':>10s} {'95% CI':>16s}  verdict"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for r in out:
        if r["insufficient"]:
            print(f"  {r['scenario']:16s} {r['pairs']:>6d} {'-':>8s} {'-':>8s} {'-':>10s} {'-':>16s}  too few complete pairs")
            continue
        lo, hi = r["ci95_rel_pct"]
        print(
            f"  {r['scenario']:16s} {r['pairs']:>6d} {r['median_a_mbps']:>8.1f} {r['median_b_mbps']:>8.1f} "
            f"{r['mean_rel_pct']:>+9.1f}% {f'[{lo:+.1f}, {hi:+.1f}]':>16s}  {_verdict(r)}"
        )

    dropped = sum(r["dropped"] for r in out)
    if dropped:
        print(f"\n  {dropped} seed(s) dropped: one side did not complete, so there was no goodput to pair.")
    if usable:
        print("\n  Sign test counts B faster / A faster / ties: "
              + ", ".join(f"{r['scenario']}={r['b_faster_in']}/{r['a_faster_in']}/{r['ties']}" for r in usable))

    if args.json:
        Path(args.json).write_text(json.dumps(out, indent=2), encoding="utf-8")
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
