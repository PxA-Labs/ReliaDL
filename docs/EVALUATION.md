# Evaluation

What has been measured about ReliaDL, how, and what the numbers do and do not
support. Every figure here is reproducible from `benchmarks/` with a stated seed.

Read `benchmarks/README.md` for how to run it. Read the limitations section
before quoting anything.

## Method

- **Workload.** A 64 MB payload generated from the run seed, fetched to a
  temporary file and verified against its SHA-256.
- **Origins.** One to three `FaultyMirror` instances on loopback, each with a
  profile fixing bandwidth, latency, jitter, request-failure rate, silent
  byte-corruption rate, truncation rate, and optionally a degraded profile
  adopted after a byte threshold.
- **Determinism.** Impairments are drawn from `blake2b(seed, byte_range,
  attempt)`, so they depend on which bytes are requested and not on the order
  requests arrive. Two policies fetching the same file meet the same faults,
  which is what makes an ablation a comparison rather than a coincidence.
- **Repetitions.** Thirty seeds per cell, 690 runs in the matrix. Tables report
  the median with the interquartile range. Transfer times are right-skewed -- one
  retry storm produces an outlier several times the typical run -- so a mean and
  standard deviation would describe the distribution badly.
- **Waste** is measured at the server: bytes the mirrors wrote, minus the file
  size. It is measured there because a client that abandons a stream early cannot
  see the bytes it avoided.

### Why goodput is measured by a separate interleaved run

`run_all` executes one cell at a time: all thirty repetitions of a policy, then
all thirty of the next. Faults are seed-derived, so seed *s* under policy A and
seed *s* under policy B meet identical impairments -- but they do not run at the
same moment, and on a shared machine the moment matters.

That is not a theoretical worry here. Absolute goodput on this testbed is not
reproducible between runs: the median for `heterogeneous`/`baseline`, one fixed
configuration, measured 46.7 MB/s in one matrix run and 28.2 MB/s in another, a
66% spread, while staying flat within each run. In the second of those runs
`adaptive-chunks` sat at ~28 MB/s for nine consecutive repetitions and then
stepped to ~48 MB/s for the remaining twenty-one, with the already-finished
`baseline` cell pinned at ~28 throughout. Paired across cells that reads as a
51% improvement at p = 0.0014. It is an artefact of when each cell ran.

So **every goodput comparison below comes from an interleaved run**, not from
the matrix: `benchmarks/ablations/interleaved_policy.py` runs the two arms back
to back on each seed, so drift between seeds falls on both arms and cancels in
the per-pair difference. The same discipline applies to
`benchmarks/ablations/concurrency_floor.py`.

The matrix remains the source for **completion rate, wasted bytes and source
attribution**, which are byte counts and are unaffected by timing drift
entirely.

### Statistics

Paired comparisons report the mean per-pair relative difference, a percentile
bootstrap 95% confidence interval (20,000 resamples, fixed seed, so the interval
reproduces), and an **exact two-sided sign test**. The sign test uses only the
direction of each difference, never its size, so a result that survives it is
not resting on an assumed distribution.

Both are reported because they can disagree, and the disagreement carries
information: a confidence interval on the *mean* that excludes zero alongside a
non-significant sign test means the effect is concentrated in a minority of
seeds rather than consistent. That is exactly what happens on `lossy` in the
concurrency-floor result below.

One consequence of the old design is worth stating plainly: **with five seeds
the sign test cannot return p < 0.05 at all.** The most extreme outcome
possible, every seed falling the same way, is p = 0.0625. The previous
evaluation could not have supported a directional claim however clean the data
looked, which is why the repetition count was raised rather than the argument
restated.

## Policies

Each differs from `baseline` in exactly one respect, so a difference is
attributable to that one change.

| Policy | Chunk sizing | Mirror routing | Sub-block digests |
| :--- | :--- | :--- | :--- |
| `baseline` | fixed 4 MB | origin only | no |
| `adaptive-chunks` | BL-DCS + concurrency floor | origin only | no |
| `adaptive-mirrors` | fixed 4 MB | EXP3.S | no |
| `localization` | fixed 4 MB | origin only | yes |
| `routing+localization` | fixed 4 MB | EXP3.S | yes |

There is no "everything at once" policy, because two of them cannot be combined:
sub-block localization needs a manifest, a manifest's digests are computed over
specific chunk boundaries, and adaptive sizing exists to move those boundaries
mid-transfer. When a manifest is supplied the engine adopts its partition and
switches the planner off.

## Results

Raw data in `benchmarks/results/results.jsonl`; tables from
`python -m benchmarks.analyze`. 115 runs, 64 MB payload, 4 workers, 5 seeds per
cell, nothing else running on the machine.

### 1. Integrity verification decides whether the transfer completes at all

Completion rate (runs where the file was delivered and verified, out of 5):

| Scenario | `baseline` | `adaptive-chunks` | `adaptive-mirrors` | `localization` | `routing+localization` |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `corrupting` | **0/30** | 1/30 | — | **30/30** | — |
| `byzantine` | **0/30** | **0/30** | 3/30 | **30/30** | **30/30** |
| `flaky` | 8/30 | 3/30 | 13/30 | **30/30** | **30/30** |

The single `corrupting`/`adaptive-chunks` success is a seed on which no
corruption happened to land, not a policy effect; a five-seed sample reported
0/5 there and slightly overstated the cleanliness of the result.

This is the clearest result in the set, and it is about correctness rather than
speed. Without per-chunk digests from a manifest, silent corruption is caught only
by the whole-file hash after everything has been transferred, and there is no way
to attribute it to a chunk or recover from it. The transfer fails. With sub-block
digests the corrupt chunk is repaired and the transfer completes every time.

### 2. Routing away from a corrupting mirror cuts waste several-fold

`byzantine`: three mirrors, the fastest corrupting 40% of what it serves.

Medians over 30 seeds. Goodput is deliberately omitted: these two policies were
measured in separate cells of the matrix, and the Method section explains why a
cross-cell goodput comparison is not trustworthy. The byte counts are.

| Policy | Wasted bytes | Bytes taken from the corrupting mirror |
| :--- | ---: | ---: |
| `localization` | 28.9% | 100% |
| `routing+localization` | **6.7%** | **27.4%** |

Routing cuts wasted bytes by roughly **4.3x**. The same pattern holds on `flaky`
(7.7% to 3.1% waste; 100% to 45.8% share).

A five-seed sample put this at 44.7% against 4.2%, which would be a tenfold
reduction. The direction was right; the magnitude was not. Waste depends on how
much corruption a given seed happens to deliver, which is exactly the quantity a
small sample estimates badly.

**Which mechanism does the work, corrected.** An earlier version of this document
credited the bandit: EXP3.S scores a failed request as zero reward, a chunk that
fails verification is a failed request, so the router should demote a corrupting
mirror for the same reason it demotes a broken one. That reasoning is wrong, and
an ablation over the shipped implementation shows it. Against three mirrors where
the corrupting one is twice as fast and corrupts 40% of what it serves, averaged
over 40 seeded trials of 400 requests:

| Mechanism | Share of requests to the corrupting mirror |
| :--- | ---: |
| Integrity-aware reward only (EXP3.S) | **45.5%** |
| Reward + consecutive-failure breaker | **5.4%** |

Uniform allocation over three mirrors would be 33%, so the reward alone does not
merely fail to demote the bad mirror -- it actively *prefers* it. The reason is
that expected reward still favours it: a mirror delivering `r_f` but corrupting a
fraction `q` beats an honest mirror delivering `r_h` whenever `q < 1 - r_h/r_f`,
which for a mirror twice as fast means it can corrupt up to half of everything it
serves and remain the best arm.

What displaces it is the circuit breaker, which reacts to the *rate* of
consecutive failures rather than to the average reward. The general lesson is
that an average-reward learner is the wrong instrument for an integrity fault,
because integrity failures are not noise in the reward but a property the reward
deliberately averages away.

### 3. Mirror routing wins when the origin degrades and loses when it is already best

Interleaved, paired, 30 seeds. Mean per-pair difference with a bootstrap 95%
interval and an exact sign test:

| Scenario | `baseline` | `adaptive-mirrors` | Difference | 95% CI | p |
| :--- | ---: | ---: | ---: | :--- | ---: |
| `failover` (origin collapses mid-transfer) | 8.6 MB/s | **16.0 MB/s** | **+86.2%** | [+64.1, +110.8] | <0.0001 |
| `heterogeneous` (origin is the fastest of three) | 50.5 MB/s | 28.4 MB/s | **-42.1%** | [-44.7, -39.4] | <0.0001 |

Reproduce with
`python -m benchmarks.ablations.interleaved_policy --a baseline --b adaptive-mirrors`.

Both directions are real and both are expected. Exploration is what lets the
router find a better mirror when the current one degrades, and it is pure cost
when the current one is already the best available. The `heterogeneous` scenario
is biased in the baseline's favour by construction -- the origin is the fastest
of its three mirrors, which is the best case for not routing at all -- and it is
kept precisely because the honest statement is that routing is not free.

### 4. The concurrency floor is necessary; adaptive sizing helps only on a degrading path

Controlled A/B, the two arms interleaved within one process so machine drift
falls on both equally:

| Scenario | Floor off | Floor on | Difference | 95% CI | sign test |
| :--- | ---: | ---: | ---: | :--- | ---: |
| `clean` | 29.0 MB/s | 52.0 MB/s | **+79.4%** | [+73.0, +88.3] | <0.0001 |
| `heterogeneous` | 29.7 MB/s | 51.0 MB/s | **+73.9%** | [+71.3, +76.4] | <0.0001 |
| `failover` | 3.6 MB/s | 9.0 MB/s | **+146.9%** | [+143.5, +150.6] | <0.0001 |
| `lossy` | 45.8 MB/s | 49.4 MB/s | +15.6% | [+5.7, +26.6] | 0.20 |

**On `lossy` the two statistics disagree, and the disagreement is the result.**
The interval on the mean excludes zero while the sign test does not, which means
the gain is concentrated in a minority of seeds -- large when it occurs, absent
otherwise -- rather than a consistent shift. Quoting the interval alone would
overstate it. On the other three scenarios both agree and the effect is large.

Reproduce with `python -m benchmarks.ablations.concurrency_floor --repetitions 30`.
The matrix has no floor-disabled arm, so this A/B lives in its own script, which
switches the floor off inside the scheduler for one arm and runs the two arms
back to back on each seed.

Unconstrained, the BL-DCS controller grows the chunk until fewer chunks remain
than there are workers and the pool starves; on `failover` a single 32 MB chunk
landing on the degraded mirror stalls everything behind it. See
[ALGORITHMS.md](ALGORITHMS.md) for the derivation.

**Adaptive sizing against a well-chosen fixed chunk size.** Interleaved, paired,
30 seeds:

| Scenario | `baseline` (fixed 4 MB) | `adaptive-chunks` | Difference | 95% CI | p |
| :--- | ---: | ---: | ---: | :--- | ---: |
| `clean` | 50.8 MB/s | 51.8 MB/s | +1.3% | [-1.6, +4.4] | 0.59 |
| `lossy` | 49.3 MB/s | 48.5 MB/s | -1.2% | [-3.7, +1.4] | 0.36 |
| `heterogeneous` | 51.9 MB/s | 52.0 MB/s | +1.3% | [-2.4, +5.3] | 0.59 |
| `failover` | 8.7 MB/s | **9.0 MB/s** | **+3.0%** | **[+2.0, +3.7]** | **<0.0001** |

On the three static paths the controller is indistinguishable from a fixed 4 MB
chunk: every interval spans zero and no sign test comes close to significance.
On `failover`, where the path degrades mid-transfer, it is reliably better --
a small effect, but a tight interval over thirty pairs and a distribution-free
test.

That is the claim the design predicts. A fixed chunk size is a good choice when a
good size is knowable in advance, and `failover` is the one scenario here where
it is not, because the right size changes while the transfer is running. The
controller earns its place there and nowhere else in this set.

Two superseded results, both from smaller or confounded samples:

- An earlier five-seed run put adaptive sizing 9-10% *worse* on `lossy` and
  `heterogeneous`. That was measured against a controller whose loss term carried
  a stray bits-to-bytes factor of 8, making every loss-driven chunk `sqrt(8)` ~
  2.83x too small. See [ALGORITHMS.md](ALGORITHMS.md) section 1.
- A later five-seed run put it **+5.8% on `heterogeneous`**. That figure came
  from the non-interleaved matrix and does not survive a paired interleaved run,
  where the same comparison gives +1.3% with an interval spanning zero. It was
  machine drift, not the controller.

**Read that as "no longer worse", not as "better".** Four of the eight intervals
above overlap substantially, five seeds is few, and the largest gap
(`heterogeneous`, +5.8%) rests on intervals that barely separate. What the data
now supports is that the corrected controller costs nothing against a fixed 4 MB
chunk on this workload; it does not establish that it beats one. The case the
controller exists for -- a path where a good fixed size is not known in advance --
is still untested, because one file size with one worker count cannot show it.

## Limitations

These bound what the numbers mean, and they are not minor.

1. **Loopback only.** There is no real propagation delay, so per-request overhead
   is understated relative to a wide-area transfer, and bandwidth limits are
   simulated by pacing writes rather than by contention for a link. The chunk
   sizing results in particular would look different over a real path with a
   meaningful bandwidth-delay product.
2. **One machine, and absolute goodput is not reproducible on it.** Client and
   mirrors share a CPU, so a heavily parallel configuration competes with the
   servers it is measuring. This is not a small effect: the median for one fixed
   configuration (`heterogeneous`/`baseline`) measured 46.7 MB/s in one matrix
   run and 28.2 MB/s in another, a 66% spread, while staying flat within each
   run. **No absolute goodput number here should be read as a property of the
   system rather than of the machine on the day.** Only the paired interleaved
   differences are defensible, because they cancel whatever the machine was
   doing. Byte counts are unaffected throughout.
3. **Emulated impairments, not validated yet.** The mirror server models the
   network rather than using one. `benchmarks/validate_against_netem.py` compares
   it against real kernel shaping, but it requires Linux and root and **has not
   been run**. Until it has, the goodput figures rest on an unchecked model. The
   waste and corruption-share figures do not: those are byte counts, and they
   hold whatever the timing model gets wrong.
4. **Bandwidth is modelled per connection, not as a shared link.** A mirror
   profiled at 40 MB/s gives each connection 40 MB/s, so four workers can see
   roughly 160 MB/s. This is the common case for real origins -- CDNs and object
   stores throttle per connection, which is why parallel downloaders exist -- but
   it is also the condition most favourable to parallelism. On a path limited by
   a shared bottleneck, extra connections divide one capacity and buy only
   latency hiding. Every speedup here is a speedup against a per-connection
   limit and must be reported as such.
5. **Baselines are partly run.** `benchmarks/run_baselines.py` compares ReliaDL
   against aria2c, axel, curl and wget on identical conditions, and reports
   missing tools as missing rather than omitting them. aria2c and curl are
   installed; axel and wget are not, and their rows say so.

   `--split` and `--min-split-size` are set explicitly in
   `benchmarks/baselines.py`: aria2's `-x` alone does not open N connections, and
   `--min-split-size` defaults to 20 MB, which silently caps the split on a 64 MB
   file. Left at their defaults they would make aria2 look far worse than it is.

   **On a clean path, aria2c is substantially faster than ReliaDL** (64 MB,
   4 workers, 5 seeds):

   | Tool | Median | Goodput | |
   | :--- | ---: | ---: | :--- |
   | aria2c | 0.80 s | **79.8 MB/s** | parallel |
   | ReliaDL | 1.26 s | 50.7 MB/s | parallel |
   | curl | 2.68 s | 23.9 MB/s | single stream |
   | axel, wget | — | — | not installed |

   aria2c is roughly 1.6x faster. That is the expected shape — aria2 is C++ and
   ReliaDL is Python asyncio — and it is reported because the comparison was run,
   not because it is flattering. **No claim in this project is that ReliaDL is
   the fastest parallel downloader, and this result says it is not.**

   **On a corrupting mirror the ordering reverses, and that is the point.** Same
   payload and seeds, against a mirror silently corrupting one byte in 20% of
   responses:

   | Tool | Exited 0 | Delivered a file matching its SHA-256 |
   | :--- | ---: | ---: |
   | aria2c | 5/5 | **3/5** |
   | ReliaDL (`baseline`, no manifest) | 1/5 | 1/5 |
   | ReliaDL (`localization`, manifest) | 5/5 | 5/5 |

   aria2c reported success on every run and handed back a full-size, silently
   wrong 64 MB file on two of them. Nothing in its output distinguishes those two
   runs from the other three. ReliaDL without a manifest detects the corruption
   and refuses to deliver, which is why its exit status is 1 on four runs; with a
   manifest it repairs and completes. This is the comparison the project exists
   to make, and it is the one place where being 1.6x slower is the correct trade.
6. **Thirty repetitions, and the limits of what they support.** Paired
   comparisons report a bootstrap interval and an exact sign test, so a result
   whose interval excludes zero and whose sign test is significant does support a
   directional claim. What they do not support is extrapolation beyond this
   testbed: a tight interval around a 3% effect on loopback says nothing about
   3% on a wide-area path. The earlier five-seed design could not support a
   directional claim at all -- the sign test floors at p = 0.0625 there.
7. **One file size, one worker count.** Everything here is 64 MB with 4 workers.
   Chunk sizing in particular is sensitive to both, and neither has been swept.

## What would make this publishable

In rough order of how much each would add:

1. Run the netem validation, and report the gap honestly whatever it is. It needs
   Linux and root, and the development machine has no WSL distribution installed.
   This is now the largest single gap, and limitation 2 makes it sharper: a
   testbed whose absolute goodput moves 66% between runs needs an external
   reference point before any timing number means much.
2. Run on a real network between two hosts, even a LAN, to check the loopback
   conclusions survive a path with a non-trivial bandwidth-delay product. This
   would also settle whether the `failover` result -- the one place adaptive
   sizing wins -- survives a real bandwidth-delay product, since that is exactly
   where the controller's bandwidth-delay term should matter most.
3. Sweep file size and worker count; the chunk-sizing result is a claim about a
   tradeoff and a tradeoff needs a curve, not a point. The question is now
   sharply posed: adaptive sizing is indistinguishable from a fixed 4 MB chunk on
   static paths and reliably better on a degrading one, so the sweep should ask
   how wide that degrading-path advantage gets.
4. Install `axel` and `wget` to complete the baseline table.
5. Investigate why absolute goodput varies 66% between matrix runs. Paired
   interleaving makes the comparisons sound without explaining the underlying
   instability, and the explanation might matter.

**Done since this list was written.**

- Repetitions raised from 5 to 30; the matrix is 690 runs. Confidence intervals
  and an exact sign test are reported for every paired comparison.
- The confound that made cross-cell goodput comparison invalid was found and
  fixed: goodput now comes from interleaved paired runs
  (`benchmarks/ablations/interleaved_policy.py`), not from the matrix.
- aria2c installed and benchmarked, on a clean path and on a corrupting mirror.
- The concurrency-floor A/B, previously hard-coded into the figure script, is now
  a committed script that reproduces it, at 30 seeds with intervals.
