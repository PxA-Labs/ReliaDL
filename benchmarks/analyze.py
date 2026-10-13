"""
Aggregate raw results into the tables a paper would report.

Reports medians with an interquartile range rather than means with a standard
deviation. Download times are right-skewed -- a retry storm produces an outlier
several times the typical run -- and a mean plus SD over a handful of repetitions
describes that distribution badly. The median and IQR say what actually happened
without assuming a shape the data does not have.

With few repetitions these are descriptive statistics, not significance tests.
Nothing here licenses a claim that one policy beats another; it reports what was
observed and how much it varied.
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Iterable

MB = 1024 * 1024


def load(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _iqr(values: list[float]) -> tuple[float, float]:
    if len(values) < 4:
        return (min(values), max(values)) if values else (0.0, 0.0)
    ordered = sorted(values)
    mid = len(ordered) // 2
    lower = statistics.median(ordered[:mid])
    upper = statistics.median(ordered[-mid:])
    return lower, upper


def summarize(rows: Iterable[dict]) -> dict:
    rows = list(rows)
    ok = [r for r in rows if r["ok"] and r["verified"]]
    out: dict = {
        "runs": len(rows),
        "succeeded": len(ok),
        "failed": len(rows) - len(ok),
    }
    if not ok:
        errors = {r.get("error") for r in rows if r.get("error")}
        out["errors"] = sorted(e for e in errors if e)[:3]
        return out

    for key, source in (
        ("goodput_mbps", lambda r: r["goodput_bps"] / MB),
        ("seconds", lambda r: r["seconds"]),
        ("waste_pct", lambda r: r["waste_ratio"] * 100),
        ("requests", lambda r: float(r["requests"])),
    ):
        values = [source(r) for r in ok]
        lo, hi = _iqr(values)
        out[key] = {
            "median": round(statistics.median(values), 3),
            "iqr_low": round(lo, 3),
            "iqr_high": round(hi, 3),
        }

    shares = [r["corrupting_mirror_share"] for r in ok if r.get("corrupting_mirror_share") is not None]
    if shares:
        out["corrupting_mirror_share"] = {
            "median": round(statistics.median(shares), 4),
            "iqr_low": round(_iqr(shares)[0], 4),
            "iqr_high": round(_iqr(shares)[1], 4),
        }

    sizes = sorted({s for r in ok for s in r.get("chunk_sizes", [])})
    if sizes:
        out["chunk_sizes_kb"] = [s // 1024 for s in sizes]
    return out


def _fmt(stat: dict | None, unit: str = "") -> str:
    if not stat:
        return "       -"
    return f"{stat['median']:7.2f}{unit} [{stat['iqr_low']:.2f}-{stat['iqr_high']:.2f}]"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize ReliaDL benchmark results")
    parser.add_argument("--results", default="benchmarks/results/results.jsonl")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of tables")
    args = parser.parse_args(argv)

    path = Path(args.results)
    if not path.is_file():
        parser.error(f"no results at {path}; run `python -m benchmarks.run_all` first")

    rows = load(path)
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["scenario"], row["policy"])].append(row)

    summary = {f"{s}/{p}": summarize(v) for (s, p), v in sorted(grouped.items())}
    if args.json:
        print(json.dumps(summary, indent=2))
        return 0

    scenarios = sorted({s for s, _ in grouped})
    print(f"\n{len(rows)} runs from {path}\n")
    print("Median [interquartile range] over repetitions. Waste is bytes the")
    print("mirrors wrote beyond the file size, as a percentage of the file.\n")

    for scenario in scenarios:
        print(f"{scenario}")
        print(f"  {'policy':18} {'goodput MB/s':>26} {'waste %':>26} {'runs':>10}")
        print(f"  {'-' * 18} {'-' * 26} {'-' * 26} {'-' * 10}")
        for (s, p), runs in sorted(grouped.items()):
            if s != scenario:
                continue
            stat = summarize(runs)
            tag = f"{stat['succeeded']}/{stat['runs']}"
            if stat["succeeded"] == 0:
                print(f"  {p:18} {'all runs failed':>26} {'':>26} {tag:>10}")
                continue
            print(
                f"  {p:18} {_fmt(stat.get('goodput_mbps')):>26} "
                f"{_fmt(stat.get('waste_pct')):>26} {tag:>10}"
            )
        shares = {
            p: summarize(runs).get("corrupting_mirror_share")
            for (s, p), runs in sorted(grouped.items())
            if s == scenario
        }
        if any(shares.values()):
            print(f"\n  {'policy':18} {'bytes from corrupting mirror':>30}")
            for p, stat in shares.items():
                if stat:
                    print(f"  {p:18} {stat['median'] * 100:>29.1f}%")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
