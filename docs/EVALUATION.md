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
- **Repetitions.** Five seeds per cell. Tables report the median with the
  interquartile range. Transfer times are right-skewed -- one retry storm
  produces an outlier several times the typical run -- so a mean and standard
  deviation over five points would describe the distribution badly.
- **Waste** is measured at the server: bytes the mirrors wrote, minus the file
  size. It is measured there because a client that abandons a stream early cannot
  see the bytes it avoided.

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
| `corrupting` | **0/5** | **0/5** | — | 5/5 | — |
| `byzantine` | **0/5** | **0/5** | **0/5** | 5/5 | 5/5 |
| `flaky` | 1/5 | 1/5 | 3/5 | 5/5 | 5/5 |

This is the clearest result in the set, and it is about correctness rather than
speed. Without per-chunk digests from a manifest, silent corruption is caught only
by the whole-file hash after everything has been transferred, and there is no way
to attribute it to a chunk or recover from it. The transfer fails. With sub-block
digests the corrupt chunk is repaired and the transfer completes every time.

### 2. Routing away from a corrupting mirror cuts waste several-fold

`byzantine`: three mirrors, the fastest corrupting 40% of what it serves.

| Policy | Goodput | Wasted bytes | Bytes taken from the corrupting mirror |
| :--- | ---: | ---: | ---: |
| `localization` | 38.2 MB/s | 44.7% | 100% |
| `routing+localization` | 29.9 MB/s | **4.2%** | **23.4%** |

Routing costs about 22% of goodput and returns roughly a tenfold reduction in
wasted bytes. The same pattern holds on `flaky` (33.3% versus 100%).

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

| Scenario | `baseline` | `adaptive-mirrors` | |
| :--- | ---: | ---: | :--- |
| `failover` (origin collapses mid-transfer) | 8.6 MB/s | **16.5 MB/s** | +91% |
| `heterogeneous` (origin is the fastest of three) | 46.7 MB/s | 28.4 MB/s | **-39%** |

Both directions are real and both are expected. Exploration is what lets the
router find a better mirror when the current one degrades, and it is pure cost
when the current one is already the best available. The `heterogeneous` scenario
is biased in the baseline's favour by construction -- the origin is the fastest
of its three mirrors, which is the best case for not routing at all -- and it is
kept precisely because the honest statement is that routing is not free.

### 4. The concurrency floor is necessary; adaptive sizing is not yet a win

Controlled A/B, the two arms interleaved within one process so machine drift
falls on both equally:

| Scenario | Floor off | Floor on | |
| :--- | ---: | ---: | :--- |
| `clean` | 28.3 MB/s | 46.6 MB/s | +65% |
| `heterogeneous` | 25.1 MB/s | 45.3 MB/s | +80% |
| `failover` | 3.6 MB/s | 8.9 MB/s | +145% |
| `lossy` | 46.2 MB/s | 47.9 MB/s | +4% |

Reproduce with `python -m benchmarks.ablations.concurrency_floor --repetitions 5`.
The matrix has no floor-disabled arm, so this A/B lives in its own script, which
switches the floor off inside the scheduler for one arm and runs the two arms
back to back on each seed.

Unconstrained, the BL-DCS controller grows the chunk until fewer chunks remain
than there are workers and the pool starves; on `failover` a single 32 MB chunk
landing on the degraded mirror stalls everything behind it. See
[ALGORITHMS.md](ALGORITHMS.md) for the derivation.

**Adaptive sizing against a well-chosen fixed chunk size**, median [IQR]:

| Scenario | `baseline` (fixed 4 MB) | `adaptive-chunks` | |
| :--- | ---: | ---: | :--- |
| `clean` | 48.2 [47.1-49.6] | 47.9 [46.3-49.9] | -0.5% |
| `lossy` | 48.2 [46.1-49.9] | 48.9 [48.6-49.8] | +1.3% |
| `heterogeneous` | 46.7 [46.1-49.1] | 49.4 [48.7-51.7] | +5.8% |
| `failover` | 8.6 [8.6-8.7] | 8.9 [8.6-8.9] | +3.0% |

This **supersedes an earlier result** in which adaptive sizing was 9-10% worse on
`lossy` and `heterogeneous`. That measurement was taken against a controller whose
loss term carried a stray factor of 8 -- a bit-rate conversion applied to a
throughput already in bytes per second -- which made every loss-driven chunk
`sqrt(8)` ~ 2.83x smaller than the model prescribes. With the factor removed the
controller is at parity or slightly ahead on all four scenarios. See
[ALGORITHMS.md](ALGORITHMS.md) section 1.

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
2. **One machine.** Client and mirrors share a CPU. A heavily parallel
   configuration competes with the servers it is measuring, and anything else
   running on the machine contaminates timings. Byte counts are unaffected.
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
6. **Five repetitions is few.** These are descriptive statistics. They report what
   was observed and how much it varied. They do not establish that one policy
   beats another, and must not be written up as though they do.
7. **One file size, one worker count.** Everything here is 64 MB with 4 workers.
   Chunk sizing in particular is sensitive to both, and neither has been swept.

## What would make this publishable

In rough order of how much each would add:

1. Run the netem validation, and report the gap honestly whatever it is. Still
   outstanding: it needs Linux and root, and the development machine has no WSL
   distribution installed. This is now the largest single gap.
2. Sweep file size and worker count; the chunk-sizing result is a claim about a
   tradeoff and a tradeoff needs a curve, not a point. This matters more since
   the controller was corrected, because the question is no longer "why is it
   worse" but "where, if anywhere, does it win".
3. Raise repetitions to 20+ and report confidence intervals. Several of the
   differences above sit inside overlapping interquartile ranges.
4. Run on a real network between two hosts, even a LAN, to check the loopback
   conclusions survive a path with a non-trivial bandwidth-delay product.
5. Install `axel` and `wget` to complete the baseline table.

**Done since this list was written:** aria2c is installed and benchmarked, on a
clean path and on a corrupting mirror (limitation 5 above). The concurrency-floor
A/B, previously hard-coded into the figure script, is now a committed script that
reproduces it.
