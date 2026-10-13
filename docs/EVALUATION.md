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
| `byzantine` | **0/5** | **0/5** | 2/5 | 5/5 | 5/5 |
| `flaky` | 2/5 | 1/5 | 4/5 | 5/5 | 5/5 |

This is the clearest result in the set, and it is about correctness rather than
speed. Without per-chunk digests from a manifest, silent corruption is caught only
by the whole-file hash after everything has been transferred, and there is no way
to attribute it to a chunk or recover from it. The transfer fails. With sub-block
digests the corrupt chunk is repaired and the transfer completes every time.

### 2. Routing away from a corrupting mirror cuts waste several-fold

`byzantine`: three mirrors, the fastest corrupting 40% of what it serves.

| Policy | Goodput | Wasted bytes | Bytes taken from the corrupting mirror |
| :--- | ---: | ---: | ---: |
| `localization` | 42.1 MB/s | 42.2% | 100% |
| `routing+localization` | 33.3 MB/s | **6.1%** | **18.6%** |

Routing costs about 20% of goodput and returns a 7x reduction in wasted bytes.
Nothing was added to make this happen: EXP3.S already scores a failed request as
zero reward, and a chunk that fails verification is a failed request, so the
router demotes a corrupting mirror for the same reason it demotes a broken one.
The same pattern holds on `flaky` (45.5% versus 100%).

### 3. Mirror routing wins when the origin degrades and loses when it is already best

| Scenario | `baseline` | `adaptive-mirrors` | |
| :--- | ---: | ---: | :--- |
| `failover` (origin collapses mid-transfer) | 7.9 MB/s | **11.9 MB/s** | +50% |
| `heterogeneous` (origin is the fastest of three) | 50.9 MB/s | 24.3 MB/s | **-52%** |

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
| `clean` | 28.8 MB/s | 52.1 MB/s | +81% |
| `heterogeneous` | 28.7 MB/s | 53.9 MB/s | +88% |
| `failover` | 3.6 MB/s | 8.9 MB/s | +146% |
| `lossy` | 43.9 MB/s | 46.1 MB/s | +5% |

Unconstrained, the BL-DCS controller grows the chunk until fewer chunks remain
than there are workers and the pool starves; on `failover` a single 32 MB chunk
landing on the degraded mirror stalls everything behind it. See
[ALGORITHMS.md](ALGORITHMS.md) for the derivation.

**But adaptive sizing still does not beat a well-chosen fixed chunk size:**

| Scenario | `baseline` (fixed 4 MB) | `adaptive-chunks` |
| :--- | ---: | ---: |
| `clean` | 53.5 MB/s | 52.8 MB/s |
| `lossy` | 51.9 MB/s | 46.7 MB/s |
| `heterogeneous` | 50.9 MB/s | 46.2 MB/s |
| `failover` | 7.9 MB/s | **9.0 MB/s** |

Parity on `clean`, a win on `failover`, modestly worse on the other two. The
honest conclusion is that the floor repairs a large regression rather than
delivering an improvement, and that 4 MB happens to be a good fixed size for this
workload. The controller's value would have to come from paths where the right
fixed size is not known in advance, and that case is not tested here -- one file
size and one worker count cannot show it.

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
4. **No comparison against other downloaders.** There are no `aria2c`, `wget` or
   `axel` numbers. aria2 is the direct competitor for parallel chunked download
   and its absence is the largest gap in this evaluation. When it is added, state
   the exact invocation: aria2's `--split` governs how many connections it really
   opens, and leaving it at the default makes aria2 look far worse than it is.
5. **Five repetitions is few.** These are descriptive statistics. They report what
   was observed and how much it varied. They do not establish that one policy
   beats another, and must not be written up as though they do.
6. **One file size, one worker count.** Everything here is 64 MB with 4 workers.
   Chunk sizing in particular is sensitive to both, and neither has been swept.

## What would make this publishable

In rough order of how much each would add:

1. Run the netem validation, and report the gap honestly whatever it is.
2. Add aria2c, curl and wget baselines.
3. Sweep file size and worker count; the chunk-sizing result is a claim about a
   tradeoff and a tradeoff needs a curve, not a point.
4. Raise repetitions to 20+ and report confidence intervals.
5. Run on a real network between two hosts, even a LAN, to check the loopback
   conclusions survive a path with a non-trivial bandwidth-delay product.
