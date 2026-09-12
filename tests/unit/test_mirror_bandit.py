"""
Unit tests for the EXP3 probability and weight engine in
src.algorithms.mirror_bandit. Verifies the selection distribution and its
uniform floor, the unbiased importance-weighted estimator, log-space weight
updates and rescaling, overflow and underflow protection over long runs,
regret against the best mirror in hindsight under stationary and
non-stationary schedules, serialization, and thread safety.
"""

from __future__ import annotations

import math
import random
import threading
import unittest
from typing import Callable, Dict, List

from src.algorithms.mirror_bandit import (
    DEFAULT_EXPLORATION_RATE,
    DEFAULT_HORIZON_ROUNDS,
    DEFAULT_WEIGHT_DECAY,
    ArmSelection,
    EXP3Bandit,
    WeightUpdate,
    decay_for_horizon,
)
from src.exceptions import ConfigurationError

MIRRORS = ["m1", "m2", "m3"]


def drive(
    bandit: EXP3Bandit,
    rounds: int,
    means: Callable[[int], Dict[str, float]],
    seed: int = 0,
) -> Dict[str, float]:
    """
    Run a bandit against a reward schedule, returning realized and best totals.

    Rewards are Bernoulli draws at the scheduled mean, which is what a mirror
    either delivering or stalling on a chunk looks like.

    The reward stream is seeded from a string so it cannot coincide with the
    bandit's own integer-seeded sampler. Sharing a seed makes the two streams
    emit identical values in lockstep — one draw each per round — which couples
    the reward to the arm that draw selected and silently rewards whichever
    mirror sits first in the cumulative distribution. That is an experimental
    artifact, not a property of the algorithm, and it is easy to introduce.
    """
    rng = random.Random(f"rewards-{seed}")
    gained = 0.0
    best_possible = 0.0
    for index in range(rounds):
        schedule = means(index)
        selection = bandit.select()
        reward = 1.0 if rng.random() < schedule[selection.arm] else 0.0
        bandit.update(selection, reward)
        gained += schedule[selection.arm]
        best_possible += max(schedule.values())
    return {"gained": gained, "best": best_possible, "regret": best_possible - gained}


def average_probabilities(
    rounds: int,
    means: Callable[[int], Dict[str, float]],
    decay: float,
    trials: int = 12,
) -> Dict[str, float]:
    """
    Mean final distribution over several seeded runs.

    A single bandit run is a noisy sample, so asserting on one trial pins
    incidental luck rather than behaviour. Fixed seeds keep the average exactly
    reproducible while still measuring the property.
    """
    totals = {arm: 0.0 for arm in MIRRORS}
    for seed in range(trials):
        bandit = EXP3Bandit(MIRRORS, weight_decay=decay, seed=seed)
        drive(bandit, rounds, means, seed=seed)
        for arm, probability in bandit.probabilities().items():
            totals[arm] += probability
    return {arm: total / trials for arm, total in totals.items()}


class TestConstruction(unittest.TestCase):
    """The mirror set and rates must be validated up front."""

    def test_uniform_prior(self) -> None:
        """Before the first request there is no evidence to prefer any mirror."""
        bandit = EXP3Bandit(MIRRORS)
        probabilities = bandit.probabilities()
        for mirror in MIRRORS:
            self.assertAlmostEqual(probabilities[mirror], 1 / 3, places=9)
        for weight in bandit.weights().values():
            self.assertAlmostEqual(weight, 1 / 3, places=9)

    def test_defaults(self) -> None:
        bandit = EXP3Bandit(MIRRORS)
        self.assertEqual(bandit.exploration_rate, DEFAULT_EXPLORATION_RATE)
        self.assertEqual(bandit.weight_decay, DEFAULT_WEIGHT_DECAY)
        self.assertEqual(bandit.arm_count, 3)
        self.assertEqual(bandit.arms, tuple(MIRRORS))
        self.assertEqual(bandit.rounds, 0)

    def test_single_mirror_is_valid(self) -> None:
        bandit = EXP3Bandit(["only"])
        self.assertAlmostEqual(bandit.probabilities()["only"], 1.0, places=9)

    def test_empty_mirror_set_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            EXP3Bandit([])

    def test_duplicate_mirror_rejected(self) -> None:
        """Two entries would split one mirror's evidence and double its mass."""
        with self.assertRaises(ConfigurationError):
            EXP3Bandit(["m1", "m2", "m1"])

    def test_invalid_mirror_identifier_rejected(self) -> None:
        for bad in ("", None, 5):
            with self.assertRaises(ConfigurationError):
                EXP3Bandit(["m1", bad])  # type: ignore[list-item]

    def test_exploration_rate_out_of_range_rejected(self) -> None:
        for bad in (0.0, -0.1, 1.1, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                EXP3Bandit(MIRRORS, exploration_rate=bad)

    def test_exploration_rate_of_one_is_valid(self) -> None:
        """Pure exploration: the distribution collapses to uniform."""
        bandit = EXP3Bandit(MIRRORS, exploration_rate=1.0)
        bandit.update_arm("m1", 1.0, 1 / 3)
        for probability in bandit.probabilities().values():
            self.assertAlmostEqual(probability, 1 / 3, places=9)

    def test_weight_decay_zero_is_valid(self) -> None:
        self.assertEqual(EXP3Bandit(MIRRORS, weight_decay=0.0).weight_decay, 0.0)

    def test_weight_decay_out_of_range_rejected(self) -> None:
        for bad in (-0.1, 1.1, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                EXP3Bandit(MIRRORS, weight_decay=bad)

    def test_non_numeric_rates_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            EXP3Bandit(MIRRORS, exploration_rate="0.1")  # type: ignore[arg-type]
        with self.assertRaises(ConfigurationError):
            EXP3Bandit(MIRRORS, weight_decay=True)  # type: ignore[arg-type]

    def test_repr(self) -> None:
        text = repr(EXP3Bandit(MIRRORS, exploration_rate=0.2))
        self.assertIn("arms=3", text)
        self.assertIn("eta=0.2", text)


class TestDecayForHorizon(unittest.TestCase):
    """alpha ~ 1/T, the EXP3.S guidance the default was measured against."""

    def test_reciprocal_of_the_horizon(self) -> None:
        self.assertAlmostEqual(decay_for_horizon(5000), 0.0002, places=9)
        self.assertAlmostEqual(decay_for_horizon(1000), 0.001, places=9)

    def test_default_matches_the_documented_horizon(self) -> None:
        self.assertAlmostEqual(
            decay_for_horizon(DEFAULT_HORIZON_ROUNDS), DEFAULT_WEIGHT_DECAY, places=9
        )

    def test_single_round_horizon_clamps_to_one(self) -> None:
        """A one-round transfer has nothing to forget, and 1 stays valid."""
        self.assertEqual(decay_for_horizon(1), 1.0)

    def test_result_is_always_a_valid_mixing_coefficient(self) -> None:
        for horizon in (1, 2, 100, 10_000, 10_000_000):
            decay = decay_for_horizon(horizon)
            self.assertTrue(0.0 < decay <= 1.0)
            EXP3Bandit(MIRRORS, weight_decay=decay)

    def test_invalid_horizon_rejected(self) -> None:
        for bad in (0, -1):
            with self.assertRaises(ConfigurationError):
                decay_for_horizon(bad)
        for bad_type in (1.5, "1000", True, None):
            with self.assertRaises(ConfigurationError):
                decay_for_horizon(bad_type)  # type: ignore[arg-type]


class TestProbabilityDistribution(unittest.TestCase):
    """p = (1 - eta) * w/sum(w) + eta/M, and its floor."""

    def test_distribution_sums_to_one(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=1)
        for _ in range(50):
            selection = bandit.select()
            bandit.update(selection, 0.9)
            self.assertAlmostEqual(sum(bandit.probabilities().values()), 1.0, places=9)

    def test_uniform_floor_is_respected(self) -> None:
        """
        Every mirror keeps at least eta/M probability, however badly it scores.

        This is what lets a recovered edge be rediscovered, and what bounds the
        importance weight 1/p.
        """
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.3, weight_decay=0.0, seed=2)
        for _ in range(2000):
            bandit.update_arm("m1", 1.0, bandit.probability("m1"))
        floor = bandit.min_probability
        self.assertAlmostEqual(floor, 0.1, places=9)
        for mirror, probability in bandit.probabilities().items():
            self.assertGreaterEqual(
                probability, floor - 1e-12, f"{mirror} fell below the floor"
            )

    def test_probability_formula_matches_weights(self) -> None:
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.2, weight_decay=0.0, seed=3)
        for _ in range(20):
            bandit.update_arm("m2", 1.0, 0.5)
        weights = bandit.weights()
        probabilities = bandit.probabilities()
        for mirror in MIRRORS:
            expected = 0.8 * weights[mirror] + 0.2 / 3
            self.assertAlmostEqual(probabilities[mirror], expected, places=9)

    def test_rewarded_mirror_gains_probability(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=4)
        before = bandit.probability("m1")
        bandit.update_arm("m1", 1.0, before)
        self.assertGreater(bandit.probability("m1"), before)

    def test_zero_reward_leaves_weights_untouched(self) -> None:
        """
        A zero reward is not a penalty: exp(0) = 1.

        EXP3 demotes a mirror only relatively, by rewarding others. A mirror
        that returns nothing keeps its weight while the rest climb past it,
        which is why no explicit penalty term is needed.
        """
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=5)
        before = bandit.weights()
        update = bandit.update_arm("m1", 0.0, 1 / 3)
        self.assertEqual(update.log_weight_delta, 0.0)
        self.assertEqual(bandit.weights(), before)

    def test_probability_of_unknown_mirror_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            EXP3Bandit(MIRRORS).probability("nope")

    def test_single_mirror_always_has_probability_one(self) -> None:
        bandit = EXP3Bandit(["solo"])
        for _ in range(100):
            selection = bandit.select()
            self.assertEqual(selection.arm, "solo")
            self.assertAlmostEqual(selection.probability, 1.0, places=9)
            bandit.update(selection, 0.5)


class TestImportanceWeightedEstimator(unittest.TestCase):
    """r_hat = r / p, the correction that keeps the estimate unbiased."""

    def test_estimate_scales_by_inverse_probability(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=6)
        update = bandit.update_arm("m1", 0.5, 0.25)
        self.assertAlmostEqual(update.estimated_reward, 2.0, places=9)

    def test_log_weight_delta_matches_the_formula(self) -> None:
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.15, weight_decay=0.0, seed=7)
        update = bandit.update_arm("m1", 0.8, 0.4)
        expected = 0.15 * (0.8 / 0.4) / 3
        self.assertAlmostEqual(update.log_weight_delta, expected, places=12)

    def test_rarely_chosen_mirror_is_compensated(self) -> None:
        """
        Equal reward at a tenth the probability must move the weight tenfold.

        Without this the router would conflate "rarely chosen" with "low
        reward" and never escape its first impression.
        """
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=8)
        frequent = bandit.update_arm("m1", 1.0, 0.5)
        rare = bandit.update_arm("m2", 1.0, 0.05)
        self.assertAlmostEqual(
            rare.log_weight_delta / frequent.log_weight_delta, 10.0, places=9
        )

    def test_importance_weight_is_bounded_by_the_floor(self) -> None:
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.1)
        selection = bandit.select()
        self.assertLessEqual(
            selection.importance_weight, 1.0 / bandit.min_probability + 1e-9
        )

    def test_single_round_cannot_move_a_log_weight_past_one(self) -> None:
        """
        The bound eta * r_hat / M <= 1 holds for every configuration.

        It is what makes log-space growth linear in the round count rather than
        unbounded, and it holds because r <= 1 and p >= eta/M.
        """
        for eta in (0.01, 0.1, 0.5, 1.0):
            for count in (1, 2, 5, 20):
                bandit = EXP3Bandit(
                    [f"m{i}" for i in range(count)], exploration_rate=eta
                )
                self.assertAlmostEqual(bandit.max_log_weight_delta, 1.0, places=9)
                update = bandit.update_arm("m0", 1.0, bandit.min_probability)
                self.assertLessEqual(update.log_weight_delta, 1.0 + 1e-12)

    def test_reward_outside_the_unit_interval_rejected(self) -> None:
        """
        An unnormalized throughput would erase every other mirror in one round.

        The bound on the weight update rests entirely on r <= 1.
        """
        bandit = EXP3Bandit(MIRRORS)
        for bad in (-0.01, 1.01, 1e9, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                bandit.update_arm("m1", bad, 0.5)

    def test_non_numeric_reward_rejected(self) -> None:
        bandit = EXP3Bandit(MIRRORS)
        for bad in ("1.0", None, True):
            with self.assertRaises(ConfigurationError):
                bandit.update_arm("m1", bad, 0.5)  # type: ignore[arg-type]

    def test_invalid_probability_rejected(self) -> None:
        bandit = EXP3Bandit(MIRRORS)
        for bad in (0.0, -0.1, 1.1, math.inf, math.nan):
            with self.assertRaises(ConfigurationError):
                bandit.update_arm("m1", 0.5, bad)
        for bad_type in ("0.5", None, True):
            with self.assertRaises(ConfigurationError):
                bandit.update_arm("m1", 0.5, bad_type)  # type: ignore[arg-type]

    def test_unknown_mirror_update_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            EXP3Bandit(MIRRORS).update_arm("ghost", 0.5, 0.5)

    def test_update_record_is_immutable(self) -> None:
        update = EXP3Bandit(MIRRORS).update_arm("m1", 0.5, 0.5)
        self.assertIsInstance(update, WeightUpdate)
        with self.assertRaises(Exception):
            update.reward = 0.9  # type: ignore[misc]


class TestSelectionProbabilityProvenance(unittest.TestCase):
    """
    The probability must travel with the selection, not be recomputed later.

    A download issues many concurrent range requests, so rewards routinely
    arrive after intervening updates have already shifted the distribution.
    """

    def test_selection_carries_its_own_probability(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=9)
        selection = bandit.select()
        self.assertIsInstance(selection, ArmSelection)
        self.assertAlmostEqual(
            selection.probability, bandit.probability(selection.arm), places=12
        )
        self.assertEqual(selection.round_index, 0)

    def test_stale_selection_still_scales_by_its_original_probability(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=10)
        in_flight = bandit.select()
        original = in_flight.probability

        # Many other requests complete before this one's reward arrives.
        for _ in range(200):
            other = bandit.select()
            bandit.update(other, 1.0)

        self.assertNotAlmostEqual(
            bandit.probability(in_flight.arm), original, places=6
        )
        update = bandit.update(in_flight, 1.0)
        self.assertAlmostEqual(update.probability, original, places=12)
        self.assertAlmostEqual(
            update.estimated_reward, 1.0 / original, places=9
        )

    def test_selection_is_immutable(self) -> None:
        selection = EXP3Bandit(MIRRORS).select()
        with self.assertRaises(Exception):
            selection.probability = 0.5  # type: ignore[misc]

    def test_round_index_advances_with_updates(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=11)
        for expected in range(5):
            selection = bandit.select()
            self.assertEqual(selection.round_index, expected)
            update = bandit.update(selection, 0.5)
            self.assertEqual(update.round_index, expected)
        self.assertEqual(bandit.rounds, 5)


class TestNumericalStability(unittest.TestCase):
    """
    Overflow and underflow protection, the reason weights live in log space.

    A log weight can grow by up to 1 per round, so over a long transfer the
    linear weight would reach e^T. A download makes tens of thousands of range
    requests and e^10000 is not representable.
    """

    def test_rescaling_pins_the_largest_log_weight_to_zero(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=12)
        for _ in range(500):
            bandit.update_arm("m1", 1.0, bandit.min_probability)
        self.assertAlmostEqual(max(bandit.log_weights().values()), 0.0, places=12)

    def test_long_run_at_maximum_gain_stays_finite(self) -> None:
        """
        The exact scenario that overflows a linear representation.

        50000 rounds each adding the maximum increment of 1 would drive a linear
        weight to e^50000.
        """
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.1, weight_decay=0.0, seed=13)
        for _ in range(50_000):
            bandit.update_arm("m1", 1.0, bandit.min_probability)

        for value in bandit.log_weights().values():
            self.assertFalse(math.isnan(value))
            self.assertFalse(value == math.inf)
        probabilities = bandit.probabilities()
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=9)
        for probability in probabilities.values():
            self.assertFalse(math.isnan(probability))
            self.assertGreaterEqual(probability, bandit.min_probability - 1e-12)

    def test_starved_mirror_underflows_gracefully(self) -> None:
        """A mirror driven far down must reach its floor, never NaN."""
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=14)
        for _ in range(20_000):
            bandit.update_arm("m1", 1.0, bandit.min_probability)
        starved = bandit.probabilities()["m3"]
        self.assertFalse(math.isnan(starved))
        self.assertAlmostEqual(starved, bandit.min_probability, places=9)
        self.assertEqual(bandit.weights()["m3"], 0.0)

    def test_distribution_survives_total_weight_collapse(self) -> None:
        """
        With every weight at zero the router falls back to uniform.

        Reachable only through a restored state, but dividing by a zero total
        would otherwise produce NaN for every mirror.
        """
        bandit = EXP3Bandit(MIRRORS)
        with bandit._lock:
            bandit._log_weights = [-math.inf] * bandit.arm_count
        probabilities = bandit.probabilities()
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=9)
        for probability in probabilities.values():
            self.assertAlmostEqual(probability, 1 / 3, places=9)
        self.assertEqual(set(bandit.weights().values()), {0.0})

    def test_decay_keeps_weights_bounded_over_a_long_run(self) -> None:
        """Finite memory means a bounded spread, not merely a bounded scale."""
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.001, seed=15)
        for _ in range(20_000):
            bandit.update_arm("m1", 1.0, bandit.min_probability)
        spread = max(bandit.log_weights().values()) - min(bandit.log_weights().values())
        self.assertLess(spread, 50.0)
        self.assertTrue(all(math.isfinite(v) for v in bandit.log_weights().values()))

    def test_extreme_exploration_rates_remain_stable(self) -> None:
        for eta in (1e-6, 0.5, 1.0):
            bandit = EXP3Bandit(MIRRORS, exploration_rate=eta, seed=16)
            for _ in range(1000):
                selection = bandit.select()
                bandit.update(selection, 1.0)
            self.assertAlmostEqual(sum(bandit.probabilities().values()), 1.0, places=9)


class TestWeightDecay(unittest.TestCase):
    """The EXP3.S mixing step that gives the router a finite memory."""

    def test_decay_pulls_weights_toward_the_mean(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.2, seed=17)
        for _ in range(50):
            bandit.update_arm("m1", 1.0, bandit.min_probability)
        decayed_spread = max(bandit.weights().values()) - min(bandit.weights().values())

        undecayed = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=17)
        for _ in range(50):
            undecayed.update_arm("m1", 1.0, undecayed.min_probability)
        undecayed_spread = max(undecayed.weights().values()) - min(
            undecayed.weights().values()
        )
        self.assertLess(decayed_spread, undecayed_spread)

    def test_decay_preserves_the_distribution_sum(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.5, seed=18)
        for _ in range(100):
            selection = bandit.select()
            bandit.update(selection, 0.7)
            self.assertAlmostEqual(sum(bandit.weights().values()), 1.0, places=9)

    def test_full_decay_returns_to_uniform_each_round(self) -> None:
        """alpha = 1 discards all history: every round starts from scratch."""
        bandit = EXP3Bandit(MIRRORS, weight_decay=1.0, seed=19)
        for _ in range(20):
            bandit.update_arm("m1", 1.0, bandit.min_probability)
            for weight in bandit.weights().values():
                self.assertAlmostEqual(weight, 1 / 3, places=9)

    def test_zero_decay_is_classic_exp3(self) -> None:
        """No mixing step at all, so weights accumulate without bound."""
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.1, weight_decay=0.0, seed=20)
        bandit.update_arm("m1", 1.0, 0.5)
        expected = 0.1 * (1.0 / 0.5) / 3
        log_weights = bandit.log_weights()
        self.assertAlmostEqual(log_weights["m1"], 0.0, places=12)
        self.assertAlmostEqual(log_weights["m2"], -expected, places=12)


class TestRegretBound(unittest.TestCase):
    """
    Regret against the best mirror in hindsight, the acceptance criterion.

    Measured against two reference points, because beating only one is easy: a
    uniform router that never learns, and the best fixed mirror, which is the
    strongest benchmark available without counterfactual knowledge.
    """

    def test_learns_the_best_mirror_when_rewards_are_stationary(self) -> None:
        """
        Classic EXP3 drives the best mirror to the ceiling the floor permits.

        The most any arm can hold is 1 - eta + eta/M, since eta of the mass is
        always spread uniformly. Reaching it means the weight-proportional term
        has gone entirely to m1.
        """
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=21)
        outcome = drive(
            bandit, 4000, lambda t: {"m1": 0.9, "m2": 0.4, "m3": 0.1}, seed=21
        )
        ceiling = 1.0 - bandit.exploration_rate + bandit.min_probability

        self.assertEqual(bandit.best_arm(), "m1")
        probabilities = bandit.probabilities()
        self.assertAlmostEqual(probabilities["m1"], ceiling, places=6)
        self.assertGreater(probabilities["m1"], probabilities["m2"])
        self.assertGreater(probabilities["m1"], probabilities["m3"])
        # Uniform routing would earn (0.9 + 0.4 + 0.1) / 3 per round.
        self.assertGreater(outcome["gained"], 4000 * (1.4 / 3) * 1.5)

    def test_classic_exp3_loses_the_ordering_among_losing_mirrors(self) -> None:
        """
        Pins a real limitation rather than pretending it away.

        With no decay, a clearly beaten mirror's weight underflows to zero, so
        every loser lands on the exploration floor and becomes
        indistinguishable — the router knows which mirror is best but has no
        opinion about second place. Measured across 12 seeds, m2 never once
        ranks above m3 despite a four-fold reward advantage.

        A positive decay bounds how far weights may separate, so the losers
        stay off the floor and their ordering survives.
        """
        schedule = lambda t: {"m1": 0.9, "m2": 0.4, "m3": 0.1}  # noqa: E731

        classic = average_probabilities(4000, schedule, decay=0.0)
        self.assertAlmostEqual(classic["m2"], classic["m3"], places=9)
        self.assertAlmostEqual(
            classic["m2"], EXP3Bandit(MIRRORS).min_probability, places=9
        )

        tracking = average_probabilities(4000, schedule, decay=DEFAULT_WEIGHT_DECAY)
        self.assertGreater(tracking["m1"], tracking["m2"])
        self.assertGreater(
            tracking["m2"],
            tracking["m3"],
            "a bounded-memory router should still rank the also-rans",
        )

    def test_regret_grows_sublinearly(self) -> None:
        """
        The defining property: average regret per round must fall as T grows.

        Linear regret would mean the router never learns anything.
        """
        per_round: List[float] = []
        for rounds in (500, 2000, 8000):
            bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=22)
            outcome = drive(
                bandit, rounds, lambda t: {"m1": 0.9, "m2": 0.4, "m3": 0.1}, seed=22
            )
            per_round.append(outcome["regret"] / rounds)
        self.assertLess(per_round[1], per_round[0])
        self.assertLess(per_round[2], per_round[1])

    def test_beats_uniform_routing_substantially(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=23)
        schedule = {"m1": 0.9, "m2": 0.4, "m3": 0.1}
        outcome = drive(bandit, 5000, lambda t: schedule, seed=23)
        uniform_total = 5000 * (sum(schedule.values()) / 3)
        self.assertGreater(outcome["gained"], uniform_total * 1.5)

    def test_recovers_after_a_mirror_degrades(self) -> None:
        """
        Non-stationary acceptance criterion: the leader fails mid-transfer.

        The router must migrate its mass onto the mirror that took over.

        Classic EXP3 gets there too, eventually — measured over 12 seeds it also
        ends with most of its mass on m3, because a dead mirror stops earning
        weight while its replacement accumulates. What separates them is the
        cost of the journey, not the destination: it spends most of the
        post-failover rounds working through the lead it had already built, at
        roughly four times the regret. That comparison is
        test_decay_outperforms_classic_exp3_on_a_failover; this test pins the
        migration itself.
        """
        def schedule(index: int) -> Dict[str, float]:
            if index < 2500:
                return {"m1": 0.9, "m2": 0.4, "m3": 0.2}
            return {"m1": 0.05, "m2": 0.4, "m3": 0.85}

        averaged = average_probabilities(
            5000, schedule, decay=decay_for_horizon(5000)
        )
        self.assertGreater(
            averaged["m3"],
            averaged["m1"],
            f"router did not migrate off the failed mirror: {averaged}",
        )
        self.assertGreater(averaged["m3"], averaged["m2"])
        self.assertGreater(averaged["m3"], 0.5)

    def test_decay_outperforms_classic_exp3_on_a_failover(self) -> None:
        """
        The measurement behind the default, asserted rather than described.

        Classic EXP3 wins a stationary schedule and is unusable on a failover;
        a small decay costs little stationary regret and transforms the
        non-stationary case.
        """
        def failover(index: int) -> Dict[str, float]:
            if index < 2500:
                return {"m1": 0.9, "m2": 0.5, "m3": 0.2}
            return {"m1": 0.1, "m2": 0.5, "m3": 0.85}

        def mean_regret(decay: float, trials: int = 12) -> float:
            total = 0.0
            for seed in range(trials):
                bandit = EXP3Bandit(MIRRORS, weight_decay=decay, seed=seed)
                total += drive(bandit, 5000, failover, seed=seed)["regret"]
            return total / trials

        classic = mean_regret(0.0)
        tracking = mean_regret(decay_for_horizon(5000))
        self.assertLess(
            tracking,
            classic / 2.0,
            f"tracking regret {tracking:.0f} vs classic {classic:.0f}",
        )

    def test_identical_mirrors_cost_nothing_however_the_mass_settles(self) -> None:
        """
        With nothing to learn, regret is zero whatever the distribution does.

        It is tempting to assert the distribution stays near uniform, but that
        is false and would be the wrong requirement anyway: with equal rewards
        the accumulated noise random-walks the weights, and measured over 12
        seeds the mass drifts as far as 0.6 from uniform. It costs exactly
        nothing, because every mirror is the best mirror. What must hold is
        that regret stays at zero and no mirror is pushed below its floor.
        """
        schedule = lambda t: {"m1": 0.5, "m2": 0.5, "m3": 0.5}  # noqa: E731
        for seed in range(6):
            bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=seed)
            outcome = drive(bandit, 3000, schedule, seed=seed)
            self.assertAlmostEqual(outcome["regret"], 0.0, places=9)
            for probability in bandit.probabilities().values():
                self.assertGreaterEqual(probability, bandit.min_probability - 1e-12)

    def test_every_mirror_keeps_being_sampled(self) -> None:
        """
        The floor in practice: a bad mirror must still be retried.

        Without it a mirror that failed early could never be rediscovered after
        recovering.
        """
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.1, weight_decay=0.0, seed=27)
        counts = {mirror: 0 for mirror in MIRRORS}
        rng = random.Random(27)
        for _ in range(6000):
            selection = bandit.select()
            counts[selection.arm] += 1
            means = {"m1": 0.95, "m2": 0.05, "m3": 0.05}
            bandit.update(selection, 1.0 if rng.random() < means[selection.arm] else 0.0)
        for mirror, count in counts.items():
            self.assertGreater(count, 50, f"{mirror} was effectively abandoned")


class TestSelectionSampling(unittest.TestCase):
    """Draws must follow the distribution and be reproducible."""

    def test_seeded_runs_are_reproducible(self) -> None:
        first = [EXP3Bandit(MIRRORS, seed=99).select().arm for _ in range(1)]
        second = [EXP3Bandit(MIRRORS, seed=99).select().arm for _ in range(1)]
        self.assertEqual(first, second)

        left = EXP3Bandit(MIRRORS, seed=42)
        right = EXP3Bandit(MIRRORS, seed=42)
        for _ in range(100):
            left_selection = left.select()
            right_selection = right.select()
            self.assertEqual(left_selection.arm, right_selection.arm)
            left.update(left_selection, 0.6)
            right.update(right_selection, 0.6)

    def test_empirical_frequencies_track_the_distribution(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=28)
        for _ in range(400):
            bandit.update_arm("m1", 1.0, bandit.probability("m1"))
        expected = bandit.probabilities()

        counts = {mirror: 0 for mirror in MIRRORS}
        trials = 30_000
        for _ in range(trials):
            counts[bandit.select().arm] += 1
        for mirror in MIRRORS:
            observed = counts[mirror] / trials
            self.assertAlmostEqual(observed, expected[mirror], delta=0.02)

    def test_selection_never_returns_an_unknown_arm(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=29)
        for _ in range(500):
            selection = bandit.select()
            self.assertIn(selection.arm, MIRRORS)
            self.assertGreater(selection.probability, 0.0)
            bandit.update(selection, 0.3)


class TestResetAndBestArm(unittest.TestCase):
    """Housekeeping surfaces."""

    def test_reset_restores_the_uniform_prior(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=30)
        for _ in range(100):
            bandit.update_arm("m1", 1.0, 0.5)
        bandit.reset()
        self.assertEqual(bandit.rounds, 0)
        for weight in bandit.weights().values():
            self.assertAlmostEqual(weight, 1 / 3, places=9)

    def test_best_arm_reports_belief_not_the_next_draw(self) -> None:
        bandit = EXP3Bandit(MIRRORS, weight_decay=0.0, seed=31)
        for _ in range(200):
            bandit.update_arm("m2", 1.0, 0.4)
        self.assertEqual(bandit.best_arm(), "m2")

    def test_best_arm_on_a_fresh_bandit(self) -> None:
        self.assertIn(EXP3Bandit(MIRRORS).best_arm(), MIRRORS)


class TestSerialization(unittest.TestCase):
    """Learned routing state must survive a resumed transfer."""

    def test_round_trip_preserves_the_distribution(self) -> None:
        bandit = EXP3Bandit(MIRRORS, exploration_rate=0.2, weight_decay=0.01, seed=32)
        for _ in range(300):
            selection = bandit.select()
            bandit.update(selection, 0.8 if selection.arm == "m1" else 0.2)

        restored = EXP3Bandit.from_dict(bandit.to_dict())
        self.assertEqual(restored.arms, bandit.arms)
        self.assertEqual(restored.exploration_rate, bandit.exploration_rate)
        self.assertEqual(restored.weight_decay, bandit.weight_decay)
        self.assertEqual(restored.rounds, bandit.rounds)
        for mirror in MIRRORS:
            self.assertAlmostEqual(
                restored.probability(mirror), bandit.probability(mirror), places=12
            )

    def test_payload_is_json_serializable(self) -> None:
        import json

        bandit = EXP3Bandit(MIRRORS, seed=33)
        bandit.update_arm("m1", 0.5, 0.4)
        revived = EXP3Bandit.from_dict(json.loads(json.dumps(bandit.to_dict())))
        self.assertEqual(revived.arms, bandit.arms)

    def test_missing_keys_rejected(self) -> None:
        payload = EXP3Bandit(MIRRORS).to_dict()
        for key in ("arms", "log_weights", "exploration_rate", "weight_decay"):
            broken = dict(payload)
            del broken[key]
            with self.assertRaises(ConfigurationError):
                EXP3Bandit.from_dict(broken)

    def test_weight_vector_length_mismatch_rejected(self) -> None:
        payload = EXP3Bandit(MIRRORS).to_dict()
        payload["log_weights"] = [0.0, 0.0]
        with self.assertRaises(ConfigurationError):
            EXP3Bandit.from_dict(payload)

    def test_non_sequence_fields_rejected(self) -> None:
        payload = EXP3Bandit(MIRRORS).to_dict()
        payload["log_weights"] = "0,0,0"
        with self.assertRaises(ConfigurationError):
            EXP3Bandit.from_dict(payload)

    def test_corrupt_weight_values_rejected(self) -> None:
        for bad in (math.nan, math.inf, "0.5", None):
            payload = EXP3Bandit(MIRRORS).to_dict()
            payload["log_weights"] = [0.0, bad, 0.0]
            with self.assertRaises(ConfigurationError):
                EXP3Bandit.from_dict(payload)

    def test_negative_infinity_weight_is_accepted(self) -> None:
        """A starved mirror legitimately reaches -inf and must restore."""
        payload = EXP3Bandit(MIRRORS).to_dict()
        payload["log_weights"] = [0.0, -math.inf, -1.0]
        restored = EXP3Bandit.from_dict(payload)
        self.assertAlmostEqual(
            restored.probability("m2"), restored.min_probability, places=12
        )

    def test_restored_weights_are_rescaled(self) -> None:
        """Absolute scale is arbitrary; only the differences are state."""
        payload = EXP3Bandit(MIRRORS).to_dict()
        payload["log_weights"] = [500.0, 499.0, 498.0]
        restored = EXP3Bandit.from_dict(payload)
        self.assertAlmostEqual(max(restored.log_weights().values()), 0.0, places=12)
        for probability in restored.probabilities().values():
            self.assertFalse(math.isnan(probability))


class TestThreadSafety(unittest.TestCase):
    """
    A download engine dispatches range requests from many workers at once.

    Interleaved selection and update must leave the distribution a valid
    probability vector, never a torn or NaN one.
    """

    def test_concurrent_select_and_update(self) -> None:
        bandit = EXP3Bandit([f"m{i}" for i in range(5)], seed=34)
        errors: List[BaseException] = []
        barrier = threading.Barrier(8)

        def worker(index: int) -> None:
            rng = random.Random(index)
            try:
                barrier.wait(timeout=10)
                for _ in range(2000):
                    selection = bandit.select()
                    bandit.update(selection, rng.random())
            except BaseException as error:  # noqa: BLE001 - recorded and re-raised
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        for thread in threads:
            self.assertFalse(thread.is_alive(), "a worker did not terminate")

        self.assertEqual(errors, [])
        self.assertEqual(bandit.rounds, 8 * 2000)
        probabilities = bandit.probabilities()
        self.assertAlmostEqual(sum(probabilities.values()), 1.0, places=9)
        for probability in probabilities.values():
            self.assertFalse(math.isnan(probability))
            self.assertGreaterEqual(probability, bandit.min_probability - 1e-12)

    def test_concurrent_updates_lose_no_rounds(self) -> None:
        bandit = EXP3Bandit(MIRRORS, seed=35)

        def worker() -> None:
            for _ in range(1000):
                bandit.update_arm("m1", 0.5, 0.5)

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(bandit.rounds, 6000)


if __name__ == "__main__":
    unittest.main()
