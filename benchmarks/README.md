# ReliaDL Benchmarks

Reproducible experiments for the chunk-sizing, mirror-routing and integrity
policies. Everything here runs on a single machine with no root, no cloud and no
Linux requirement, and every number is traceable to a seed and a script.

## Quick start

```bash
python -m benchmarks.run_all --list                 # show scenarios and policies
python -m benchmarks.run_all --repetitions 5        # run the full matrix
python -m benchmarks.analyze                        # summarize results
```

Results append to `benchmarks/results/results.jsonl` as JSON Lines, one object
per run. Re-running the same command skips completed `(scenario, policy, seed)`
triples, so an interrupted run resumes; `--fresh` starts over.

## What is measured

| Metric | Meaning |
| :--- | :--- |
| `goodput_bps` | File size divided by wall-clock time |
| `bytes_written` | Bytes the mirrors wrote to sockets, **measured at the server** |
| `wasted_bytes` | `bytes_written - file_size`: everything retries and abandoned streams cost |
| `corrupting_mirror_share` | Fraction of served bytes that came from a mirror known to corrupt |
| `chunk_sizes` | Distinct chunk sizes the run actually used |
| `mirror_probabilities` | The bandit's final selection distribution |

Waste is measured at the server rather than inferred from the client, because a
client that abandons a stream early cannot see the bytes it avoided. This is the
metric the integrity policies exist to reduce.

## Design: why an emulated mirror instead of `tc`/netem

1. **Reproducibility.** Impairments are drawn from `blake2b(seed, byte_range,
   attempt)` rather than a sequential PRNG, so they depend on *which* bytes are
   requested, never on the order requests arrive in. Two different schedulers
   fetching the same file therefore meet the same faults, which is what makes an
   ablation a fair comparison. The same seed reproduces a run on any machine.
2. **Measurement.** The server counts what it writes, so waste is observed.
3. **Corruption.** This is the decisive reason. Silent byte corruption is the
   central variable in these experiments and `tc` cannot produce it: netem
   corrupts frames, and TCP's checksum catches and retransmits them. What matters
   here is corruption that arrives looking intact, which is what a faulty disk, a
   buggy middlebox or a hostile mirror actually delivers.

The cost is that this is emulation. `validate_against_netem.py` exists to address
that: on Linux it runs a reduced scenario set under real kernel shaping and
compares the distributions. **Run it before presenting these numbers**; an
emulated testbed that has never been checked against a real one is a fair thing
for a reviewer to reject.

## Scenarios

| Name | Mirrors | What it isolates |
| :--- | ---: | :--- |
| `clean` | 1 | No-impairment ceiling |
| `lossy` | 1 | Retry and backoff under 15% request failure |
| `corrupting` | 1 | Silent corruption with nowhere to fail over to |
| `heterogeneous` | 3 | Whether routing finds the fast mirror |
| `failover` | 2 | Tracking a mirror that degrades mid-transfer |
| `byzantine` | 3 | A fast mirror that corrupts 40% of what it serves |
| `flaky` | 2 | Failures, corruption and truncation together |

## Policies

| Name | Chunk sizing | Mirror routing | Sub-block digests |
| :--- | :--- | :--- | :--- |
| `baseline` | fixed | origin only | no |
| `adaptive-chunks` | BL-DCS | origin only | no |
| `adaptive-mirrors` | fixed | EXP3.S | no |
| `localization` | fixed | origin only | yes |
| `all` | BL-DCS | EXP3.S | yes |

`baseline` is the control. Each single-policy row differs from it in exactly one
respect, so the difference is attributable.

Cells that cannot differ are skipped rather than run: mirror routing on a
one-mirror scenario is the control by construction, and localization changes
nothing where no corruption is injected.

## A constraint worth knowing before reading the results

**Adaptive chunk sizing and sub-block localization cannot be combined.**
Localization needs a manifest; a manifest's digests are computed over specific
chunk boundaries; adaptive sizing exists to move those boundaries mid-transfer. So
when a manifest is supplied the engine adopts its partition and switches the
planner off. This is a property of pre-authenticated per-chunk hashing, not an
implementation shortcut -- any scheme keeping both must publish digests at a
granularity independent of the chunking, which is what the 64 KB sub-blocks
already are and is the way out.

The matrix therefore has no "everything at once" cell. An earlier version did, and
it was misleading: it set adaptive sizing and then loaded a manifest that disabled
it, so it measured routing plus localization while claiming all three.

## Baselines against other tools

**Not yet implemented, and the results are incomplete without it.** A paper
claiming a better parallel downloader needs `aria2c -x N -s N` numbers; `curl`
and `wget` single-stream are useful references. `aria2c` is not installed on the
development machine, which is why this is outstanding rather than done.

Note when adding it: aria2's `--split` governs how many connections it actually
opens, and leaving it at the default makes aria2 look far worse than it is. State
the exact invocation alongside any number.

## Statistics

`analyze.py` reports medians with an interquartile range. Transfer times are
right-skewed -- one retry storm produces an outlier several times the typical run
-- so a mean and standard deviation over a handful of repetitions describes the
distribution badly.

With five repetitions these are descriptive statistics, not significance tests.
They say what was observed and how much it varied. They do not establish that one
policy beats another, and should not be written up as though they do.

## Known limitations

- Loopback only. There is no propagation delay to speak of, so per-request
  overhead is understated relative to a wide-area transfer and bandwidth caps are
  simulated by pacing rather than by contention.
- Single machine. Client and servers share a CPU, so a heavily parallel
  configuration competes with the mirrors it is measuring.
- No cross-machine or cross-OS results are collected, and the numbers are not
  comparable between machines. `environment.json` records where a run happened.
