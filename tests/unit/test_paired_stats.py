"""
Tests for the paired-comparison statistics in benchmarks/paired.py.

These are tested because their output goes into a paper. A sign test that
mishandles ties, or a bootstrap whose interval is off by one index, produces a
p-value or a confidence interval that looks entirely reasonable and is wrong --
the failure mode is silent by construction, which is exactly when a test earns
its place.
"""

from __future__ import annotations

import unittest

from benchmarks.paired import bootstrap_ci, compare, pairs_for, sign_test

MB = 1024 * 1024


def run(scenario: str, policy: str, seed: int, mbps: float, *, ok: bool = True) -> dict:
    return {
        "scenario": scenario,
        "policy": policy,
        "seed": seed,
        "ok": ok,
        "verified": ok,
        "goodput_bps": mbps * MB,
    }


class TestSignTest(unittest.TestCase):
    """The sign test must be exact, two-sided, and conservative about ties."""

    def test_unanimous_matches_the_closed_form(self) -> None:
        # Two-sided exact probability of all n falling one way is 2 * (1/2)^n.
        for n in (5, 10, 20, 30):
            with self.subTest(n=n):
                _, _, _, p = sign_test([1.0] * n)
                self.assertAlmostEqual(p, 2 * 0.5**n, places=12)

    def test_five_seeds_cannot_reach_significance(self) -> None:
        """
        The reason the repetition count was raised.

        With five pairs the most extreme outcome possible is p = 0.0625, so no
        five-seed comparison could have supported a directional claim at the
        conventional threshold however consistent the data looked.
        """
        _, _, _, p = sign_test([1.0] * 5)
        self.assertGreater(p, 0.05)
        _, _, _, p30 = sign_test([1.0] * 30)
        self.assertLess(p30, 0.05)

    def test_balanced_differences_give_no_evidence(self) -> None:
        _, _, _, p = sign_test([1.0, -1.0, 1.0, -1.0])
        self.assertEqual(p, 1.0)

    def test_ties_are_excluded_not_split(self) -> None:
        pos, neg, ties, p = sign_test([0.0, 0.0, 1.0])
        self.assertEqual((pos, neg, ties), (1, 0, 2))
        # n = 1 after dropping ties, so the two-sided p is 1.0, not 2*(1/2)^3.
        self.assertEqual(p, 1.0)

    def test_all_ties_report_no_evidence(self) -> None:
        _, _, _, p = sign_test([0.0, 0.0, 0.0])
        self.assertEqual(p, 1.0)

    def test_is_symmetric_under_sign_flip(self) -> None:
        values = [3.0, -1.0, 2.0, 5.0, -0.5]
        _, _, _, p = sign_test(values)
        _, _, _, flipped = sign_test([-v for v in values])
        self.assertAlmostEqual(p, flipped, places=12)


class TestBootstrap(unittest.TestCase):
    def test_zero_variance_gives_a_degenerate_interval(self) -> None:
        lo, hi = bootstrap_ci([5.0] * 30)
        self.assertAlmostEqual(lo, 5.0)
        self.assertAlmostEqual(hi, 5.0)

    def test_interval_brackets_the_mean(self) -> None:
        values = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]
        lo, hi = bootstrap_ci(values)
        self.assertLess(lo, sum(values) / len(values))
        self.assertGreater(hi, sum(values) / len(values))

    def test_is_reproducible(self) -> None:
        """A fixed seed is what lets the interval be quoted in a document."""
        values = [1.0, -2.0, 3.5, 0.5, 4.0, -1.5, 2.0]
        self.assertEqual(bootstrap_ci(values), bootstrap_ci(values))

    def test_too_few_points_returns_nan(self) -> None:
        lo, hi = bootstrap_ci([1.0])
        self.assertNotEqual(lo, lo)  # NaN
        self.assertNotEqual(hi, hi)


class TestPairing(unittest.TestCase):
    """Pairing must match on seed and refuse to compare incomplete runs."""

    def test_pairs_are_matched_by_seed(self) -> None:
        rows = [
            run("clean", "baseline", 1, 40.0), run("clean", "adaptive-chunks", 1, 44.0),
            run("clean", "baseline", 2, 50.0), run("clean", "adaptive-chunks", 2, 55.0),
        ]
        kept, dropped, seen = pairs_for(rows, "clean", "baseline", "adaptive-chunks")
        self.assertEqual([(s, a, b) for s, a, b in kept], [(1, 40.0, 44.0), (2, 50.0, 55.0)])
        self.assertEqual((dropped, seen), (0, 2))

    def test_incomplete_run_drops_the_whole_pair(self) -> None:
        rows = [
            run("clean", "baseline", 1, 40.0), run("clean", "adaptive-chunks", 1, 44.0),
            run("clean", "baseline", 2, 50.0, ok=False), run("clean", "adaptive-chunks", 2, 55.0),
        ]
        kept, dropped, seen = pairs_for(rows, "clean", "baseline", "adaptive-chunks")
        self.assertEqual(len(kept), 1)
        self.assertEqual((dropped, seen), (1, 2))

    def test_unmatched_seeds_are_not_paired(self) -> None:
        rows = [
            run("clean", "baseline", 1, 40.0),
            run("clean", "adaptive-chunks", 2, 44.0),
        ]
        kept, _, seen = pairs_for(rows, "clean", "baseline", "adaptive-chunks")
        self.assertEqual(kept, [])
        self.assertEqual(seen, 0)

    def test_other_scenarios_are_ignored(self) -> None:
        rows = [
            run("clean", "baseline", 1, 40.0), run("clean", "adaptive-chunks", 1, 44.0),
            run("lossy", "baseline", 1, 10.0), run("lossy", "adaptive-chunks", 1, 90.0),
        ]
        kept, _, _ = pairs_for(rows, "clean", "baseline", "adaptive-chunks")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0][1:], (40.0, 44.0))


class TestCompare(unittest.TestCase):
    def test_consistent_improvement_is_detected(self) -> None:
        rows = []
        for seed in range(30):
            base = 40.0 + seed * 0.5          # large between-seed spread
            rows.append(run("clean", "baseline", seed, base))
            rows.append(run("clean", "adaptive-chunks", seed, base * 1.05))
        result = compare(rows, "clean", "baseline", "adaptive-chunks")
        self.assertFalse(result["insufficient"])
        self.assertEqual(result["pairs"], 30)
        self.assertAlmostEqual(result["mean_rel_pct"], 5.0, places=6)
        self.assertGreater(result["ci95_rel_pct"][0], 0.0)
        self.assertLess(result["sign_test_p"], 0.05)

    def test_between_seed_spread_does_not_mask_a_paired_effect(self) -> None:
        """
        The motivating case: unpaired ranges overlap completely, yet every pair
        moves the same way. The paired view must still find it.
        """
        rows = []
        for seed, base in enumerate([10.0, 50.0, 20.0, 90.0, 35.0, 70.0,
                                     15.0, 60.0, 25.0, 80.0]):
            rows.append(run("clean", "baseline", seed, base))
            rows.append(run("clean", "adaptive-chunks", seed, base * 1.03))
        result = compare(rows, "clean", "baseline", "adaptive-chunks")
        self.assertLess(result["sign_test_p"], 0.01)
        self.assertGreater(result["ci95_rel_pct"][0], 0.0)

    def test_noise_yields_no_direction(self) -> None:
        deltas = [1.02, 0.98] * 10
        rows = []
        for seed, factor in enumerate(deltas):
            rows.append(run("clean", "baseline", seed, 40.0))
            rows.append(run("clean", "adaptive-chunks", seed, 40.0 * factor))
        result = compare(rows, "clean", "baseline", "adaptive-chunks")
        self.assertEqual(result["sign_test_p"], 1.0)
        lo, hi = result["ci95_rel_pct"]
        self.assertLessEqual(lo, 0.0)
        self.assertGreaterEqual(hi, 0.0)

    def test_too_few_pairs_is_reported_not_guessed(self) -> None:
        rows = [run("clean", "baseline", 1, 40.0), run("clean", "adaptive-chunks", 1, 44.0)]
        result = compare(rows, "clean", "baseline", "adaptive-chunks")
        self.assertTrue(result["insufficient"])


if __name__ == "__main__":
    unittest.main()
