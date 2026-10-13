"""
Named network scenarios and the configuration matrix for the experiments.

A scenario fixes the *environment* (how many mirrors, how each behaves). A policy
fixes the *system under test* (which ReliaDL mechanisms are enabled). Every
measurement is one (scenario, policy, seed) triple, and an ablation is the
difference between two policies on the same scenario and seed.
"""

from __future__ import annotations

from dataclasses import dataclass
from benchmarks.mirror_server import MirrorProfile

MB = 1024 * 1024


@dataclass(frozen=True)
class Scenario:
    """One environment: a set of mirrors and how they misbehave."""

    name: str
    description: str
    mirrors: tuple[MirrorProfile, ...]
    file_size: int = 64 * MB

    @property
    def mirror_count(self) -> int:
        return len(self.mirrors)


@dataclass(frozen=True)
class Policy:
    """One system configuration under test."""

    name: str
    description: str
    adaptive_chunks: bool = False
    adaptive_mirrors: bool = False
    sub_block_digests: bool = False
    chunk_size: int = 4 * MB
    workers: int = 4


# --------------------------------------------------------------------- scenarios

_CLEAN = MirrorProfile(name="clean", bandwidth_bps=40 * MB, latency_seconds=0.005)

SCENARIOS: tuple[Scenario, ...] = (
    Scenario(
        name="clean",
        description="Single well-behaved mirror. Establishes the no-impairment ceiling.",
        mirrors=(_CLEAN,),
    ),
    Scenario(
        name="lossy",
        description="Single mirror failing 15% of range requests. Exercises retry and backoff.",
        mirrors=(
            MirrorProfile(
                name="lossy", bandwidth_bps=40 * MB, latency_seconds=0.01, failure_rate=0.15
            ),
        ),
    ),
    Scenario(
        name="corrupting",
        description=(
            "Single mirror silently corrupting one byte in 20% of responses. "
            "The integrity path is the only thing that catches this."
        ),
        mirrors=(
            MirrorProfile(
                name="corrupting",
                bandwidth_bps=40 * MB,
                latency_seconds=0.01,
                corruption_rate=0.20,
            ),
        ),
    ),
    Scenario(
        name="heterogeneous",
        description=(
            "Three mirrors at 40, 8 and 20 MB/s. Tests whether routing finds the "
            "fast one rather than spreading load evenly."
        ),
        mirrors=(
            MirrorProfile(name="fast", bandwidth_bps=40 * MB, latency_seconds=0.005),
            MirrorProfile(name="slow", bandwidth_bps=8 * MB, latency_seconds=0.05),
            MirrorProfile(name="medium", bandwidth_bps=20 * MB, latency_seconds=0.02),
        ),
    ),
    Scenario(
        name="failover",
        description=(
            "Two mirrors; the initially fastest collapses to 2 MB/s a quarter of "
            "the way in. A policy that converges rather than tracks loses here."
        ),
        mirrors=(
            MirrorProfile(
                name="degrading",
                bandwidth_bps=40 * MB,
                latency_seconds=0.005,
                degrade_after_bytes=16 * MB,
                degraded=MirrorProfile(
                    name="degraded", bandwidth_bps=2 * MB, latency_seconds=0.15
                ),
            ),
            MirrorProfile(name="steady", bandwidth_bps=16 * MB, latency_seconds=0.02),
        ),
    ),
    Scenario(
        name="byzantine",
        description=(
            "Three mirrors, one corrupting 40% of what it serves while remaining "
            "the fastest. Throughput-only routing is actively misled here."
        ),
        mirrors=(
            MirrorProfile(
                name="fast-corrupting",
                bandwidth_bps=40 * MB,
                latency_seconds=0.005,
                corruption_rate=0.40,
            ),
            MirrorProfile(name="honest-a", bandwidth_bps=16 * MB, latency_seconds=0.02),
            MirrorProfile(name="honest-b", bandwidth_bps=16 * MB, latency_seconds=0.02),
        ),
    ),
    Scenario(
        name="flaky",
        description="Mixed impairments: failures, corruption and truncation together.",
        mirrors=(
            MirrorProfile(
                name="flaky",
                bandwidth_bps=24 * MB,
                latency_seconds=0.03,
                jitter_seconds=0.02,
                failure_rate=0.08,
                corruption_rate=0.08,
                truncation_rate=0.05,
            ),
            MirrorProfile(name="backup", bandwidth_bps=12 * MB, latency_seconds=0.04),
        ),
    ),
)


# ---------------------------------------------------------------------- policies

POLICIES: tuple[Policy, ...] = (
    Policy(
        name="baseline",
        description="Fixed chunks, origin only, whole-chunk verification. The control.",
    ),
    Policy(
        name="adaptive-chunks",
        description="BL-DCS chunk sizing; everything else as the control.",
        adaptive_chunks=True,
    ),
    Policy(
        name="adaptive-mirrors",
        description="EXP3.S mirror routing; everything else as the control.",
        adaptive_mirrors=True,
    ),
    Policy(
        name="localization",
        description="Sub-block digests so a corrupt chunk is repaired, not re-fetched.",
        sub_block_digests=True,
    ),
    Policy(
        name="routing+localization",
        description=(
            "Mirror routing plus sub-block digests. NOT chunk sizing as well -- "
            "see the note below; the two cannot be combined."
        ),
        adaptive_mirrors=True,
        sub_block_digests=True,
    ),
)

# A tension worth stating plainly, because it constrains what can be combined.
#
# Sub-block localization needs a manifest, and a manifest's digests are computed
# over specific chunk boundaries. Adaptive chunk sizing exists precisely to move
# those boundaries during the transfer. The two are therefore mutually exclusive
# as implemented: when a manifest is supplied the engine adopts its partition and
# switches the planner off, because a digest computed over bytes [0, 4MB) says
# nothing about a chunk the controller decided should be [0, 16MB).
#
# This is not an implementation shortcut that a later version can remove. Any
# scheme that keeps both must either publish digests at a granularity independent
# of the chunking (which is what the 64 KB sub-blocks already are, and is the way
# out), or give up on pre-authenticated per-chunk hashes. The earlier
# "all policies at once" cell in this matrix quietly hid the conflict: it set
# adaptive_chunk_sizing and then loaded a manifest that disabled it, so it
# measured routing plus localization while claiming to measure all three.
NOTE_CHUNKING_VS_MANIFEST = __doc__


SCENARIOS_BY_NAME = {s.name: s for s in SCENARIOS}
POLICIES_BY_NAME = {p.name: p for p in POLICIES}


def default_matrix() -> list[tuple[Scenario, Policy]]:
    """
    Every (scenario, policy) pair worth running.

    Pairs that cannot differ are skipped rather than measured: mirror routing on a
    single-mirror scenario is the control by construction, and localization
    changes nothing where no corruption is injected. Running them anyway would pad
    the results table with cells whose equality proves nothing.
    """
    pairs: list[tuple[Scenario, Policy]] = []
    for scenario in SCENARIOS:
        corrupting = any(m.corruption_rate > 0 for m in scenario.mirrors)
        for policy in POLICIES:
            if policy.adaptive_mirrors and scenario.mirror_count < 2:
                continue
            if policy.sub_block_digests and not corrupting:
                continue
            pairs.append((scenario, policy))
    return pairs
