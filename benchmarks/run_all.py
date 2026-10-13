"""
Run the experiment matrix and write raw results.

Usage::

    python -m benchmarks.run_all --repetitions 5 --out results/
    python -m benchmarks.run_all --scenario byzantine --policy all --repetitions 3
    python -m benchmarks.run_all --list

Every run is identified by (scenario, policy, seed) and seeds are derived from
``--base-seed``, so the whole matrix is reproducible from the command line alone.
Results are appended as JSON Lines as they complete, so a long run that is
interrupted still leaves usable data and can be resumed by re-running with the
same arguments -- completed triples are skipped.
"""

from __future__ import annotations

import argparse
import json
import platform
import sys
import time
from pathlib import Path

from benchmarks.runner import run_one
from benchmarks.scenarios import (
    POLICIES,
    POLICIES_BY_NAME,
    SCENARIOS,
    SCENARIOS_BY_NAME,
    default_matrix,
)


def _environment() -> dict:
    """Recorded with the results; numbers are not comparable across machines."""
    return {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "processor": platform.processor() or platform.machine(),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def _completed(path: Path) -> set[tuple[str, str, int]]:
    if not path.is_file():
        return set()
    done: set[tuple[str, str, int]] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        done.add((row["scenario"], row["policy"], row["seed"]))
    return done


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the ReliaDL experiment matrix")
    parser.add_argument("--out", default="benchmarks/results", help="Output directory")
    parser.add_argument("--repetitions", type=int, default=5, help="Runs per cell")
    parser.add_argument("--base-seed", type=int, default=1000)
    parser.add_argument("--scenario", action="append", help="Limit to these scenarios")
    parser.add_argument("--policy", action="append", help="Limit to these policies")
    parser.add_argument("--timeout", type=float, default=300.0, help="Per-run timeout")
    parser.add_argument("--list", action="store_true", help="List scenarios and policies")
    parser.add_argument("--fresh", action="store_true", help="Ignore existing results")
    args = parser.parse_args(argv)

    if args.list:
        print("Scenarios:")
        for s in SCENARIOS:
            print(f"  {s.name:16} {s.mirror_count} mirror(s)  {s.description}")
        print("\nPolicies:")
        for p in POLICIES:
            print(f"  {p.name:16} {p.description}")
        print(f"\nDefault matrix: {len(default_matrix())} cells")
        return 0

    pairs = default_matrix()
    if args.scenario:
        unknown = set(args.scenario) - set(SCENARIOS_BY_NAME)
        if unknown:
            parser.error(f"unknown scenario(s): {sorted(unknown)}")
        pairs = [(s, p) for s, p in pairs if s.name in args.scenario]
    if args.policy:
        unknown = set(args.policy) - set(POLICIES_BY_NAME)
        if unknown:
            parser.error(f"unknown policy(ies): {sorted(unknown)}")
        pairs = [(s, p) for s, p in pairs if p.name in args.policy]
    if not pairs:
        parser.error("no cells selected")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.jsonl"
    (out_dir / "environment.json").write_text(
        json.dumps(_environment(), indent=2), encoding="utf-8"
    )

    if args.fresh and results_path.exists():
        results_path.unlink()
    done = _completed(results_path)

    total = len(pairs) * args.repetitions
    index = 0
    started = time.perf_counter()
    with results_path.open("a", encoding="utf-8") as sink:
        for scenario, policy in pairs:
            for rep in range(args.repetitions):
                seed = args.base_seed + rep
                index += 1
                key = (scenario.name, policy.name, seed)
                if key in done:
                    print(f"[{index}/{total}] skip {scenario.name}/{policy.name} seed={seed}")
                    continue
                print(
                    f"[{index}/{total}] {scenario.name}/{policy.name} seed={seed} ... ",
                    end="",
                    flush=True,
                )
                result = run_one(scenario, policy, seed, timeout=args.timeout)
                sink.write(json.dumps(result.as_dict()) + "\n")
                sink.flush()
                if result.ok and result.verified:
                    print(
                        f"{result.seconds:6.2f}s  "
                        f"{result.goodput_bps / (1024 * 1024):6.1f} MB/s  "
                        f"waste {result.waste_ratio * 100:5.1f}%"
                    )
                else:
                    print(f"FAILED  {result.error}")

    elapsed = time.perf_counter() - started
    print(f"\nWrote {results_path} in {elapsed / 60:.1f} min")
    print(f"Analyze with: python -m benchmarks.analyze --results {results_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
