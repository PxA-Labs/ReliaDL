# ReliaDL: Algorithms and Their Provenance

This document describes the algorithms **actually implemented** in `reliadl/`, states
which are prior art and which are our own composition, and records the evidence behind
every tuned constant.

Nothing here is claimed as a research contribution unless it is marked as one, and no
claim appears without either a citation to prior work or a reproducible measurement in
this repository.

## Status legend

| Status | Meaning |
| :--- | :--- |
| **Wired** | Called on the live download path in `reliadl/download_engine.py` |
| **Library** | Implemented and unit-tested, but not yet reachable from a download |
| **Planned** | Not implemented. Listed only so its absence is unambiguous |

---

## 1. BL-DCS: Bandwidth-Loss Closed-Loop Dynamic Chunk Sizing

**Status: Library** — `reliadl/algorithms/adaptive_chunker.py`

**Provenance:** standard square-root cost tradeoff. Our contribution is the
per-segment loss rescaling, not the formula.

### Problem

A chunk size that is too small pays per-request overhead on every range GET. A chunk
size that is too large wastes more bytes when a chunk must be re-fetched. The optimum
depends on the path, which changes during a transfer.

### Formulation

Minimizing the per-byte cost of a transfer over chunk size `S`:

```
J(S) = tau_overhead * mu / S  +  p_seg * S / MSS
```

where `tau_overhead` is per-request overhead (s), `mu` is throughput (B/s), `p_seg` is
the per-segment drop probability and `MSS` the maximum segment size. Setting
`dJ/dS = 0` gives the familiar square-root form. The controller adds a bandwidth-delay
term so a fast, clean path is not throttled by request overhead:

```
S* = clamp( gamma * sqrt( tau_overhead * mu_k * MSS / (8 * p_k + eps) )
            + lambda * mu_k * tau_k,
            S_min, S_max )
```

The `MSS` factor inside the radical is what makes the radicand a byte-squared
quantity, so the root yields bytes rather than the unit-dependent `sqrt(bytes)` of the
bare form. The result is quantized to a power of two for allocator and page alignment.

This is the classic square-root tradeoff between a fixed per-batch cost and a cost
linear in batch size — the same first-order argument that produces the economic order
quantity. **We do not claim the form as novel.**

### What is ours

The metrics collector observes failures *per chunk*, while the loss model is expressed
*per segment*; at an 8 MB chunk the two differ by roughly four orders of magnitude.
Closing that loop with the transfer size that produced the observation,

```
p_seg = 1 - (1 - p_chunk) ** (MSS / S_observed)
```

converts a measured chunk failure ratio into the per-segment rate the radical expects
(`adaptive_chunker.py:267-272`). Without it the controller is dimensionally incoherent
in practice even though the formula looks right on paper.

### Open issues — do not cite this controller until these are resolved

- `DEFAULT_GAMMA = 1.0` and `DEFAULT_LAMBDA = 1.0` (`:48`, `:51`) are the only free
  parameters of the formula and **both are unjustified placeholders**. Neither has been
  swept or fitted.
- The factor `8.0` in the denominator at `:302` does not appear in the derivation above
  and is unexplained; it may be a bits/bytes slip. It must be derived or removed.
- `DEFAULT_OVERHEAD_SECONDS = 0.025` (`:54`) is described as "nominal" and has no
  measured source.

There is **no convergence proof and no optimality bound** for this controller. It is a
closed-form static optimum recomputed per decision epoch from a smoothed state
estimate, not an online learning algorithm with a regret guarantee.

---

## 2. EXP3.S Mirror Routing

**Status: Library** — `reliadl/algorithms/mirror_bandit.py`

**Provenance:** EXP3 and EXP3.S are Auer, Cesa-Bianchi, Freund and Schapire (2002).
The implementation and the decay measurement are ours; the algorithm is not.

### Why an adversarial bandit

When a file is available from several CDN edges or replicas, an edge's throughput
depends on other tenants' load, routing changes and cache state — none of which are
stationary, and none observable. Only the reward of the mirror actually used is
observed; the counterfactual throughput of the others is unknowable. That makes this a
bandit problem, and it motivates an algorithm that assumes nothing about the reward
distribution: no independence, no stationarity.

### The algorithm

```
p_{m,t}   = (1 - eta) * w_{m,t} / sum_j w_{j,t}  +  eta / M
r_hat     = r / p_{m,t}
w_{m,t+1} = w_{m,t} * exp(eta * r_hat / M)
```

The uniform `eta/M` floor keeps every mirror under occasional test and bounds every
selection probability away from zero, which is what makes the importance-weighted
estimate `r/p` finite. Dividing by the selection probability is what keeps the estimate
unbiased.

Weights are held as logarithms and rescaled so the largest is exactly 1. This is a
requirement, not a refinement: the per-round exponent is bounded by 1, but it
accumulates, so a weight can grow like `e^T` over `T` rounds, and a long download makes
tens of thousands of range requests. Rescaling leaves the selection distribution
untouched, since it depends only on weight ratios.

### Tracking a moving target

Classic EXP3 has vanishing regret against the best *fixed* arm, which is the wrong
target here: over a long transfer the best mirror changes. The EXP3.S decay mixes each
weight back toward the mean every round,

```
w_m <- (1 - alpha) * w_m + (alpha / M) * sum_j w_j
```

capping how much evidence any arm can hoard. With `alpha = 0` the behaviour is exactly
classic EXP3.

### Measured evidence for the decay default

Regret against the best mirror in hindsight; three mirrors, `eta = 0.1`, averaged over
24 seeded trials of 5000 rounds, under a stationary schedule, an abrupt mid-transfer
failover, and a gradual drift. The reward stream is seeded independently of the
router's own sampler — sharing a seed makes the two draw identical values in lockstep
and couples each reward to the arm that draw selected, which quietly favours whichever
mirror sits first in the cumulative distribution.

```
decay     stationary   switching   drifting   worst
0                217        1709        771    1709
0.0002           246         378        222     378
0.0005           279         390        235     390
0.001            333         426        256     426
0.002            420         494        311     494
0.004            578         633        397     633
```

Classic EXP3 wins the stationary column and is unusable everywhere else. Any small
positive decay cuts worst-case regret by more than four times, for about a tenth more
regret in the stationary case. The optimum sat at `alpha = 0.0002`, which is `1/T` for
the 5000-round horizon measured — the EXP3.S guidance `alpha ~ 1/T` — and this was
confirmed at `T = 1000, 2000, 10000`. `decay_for_horizon` applies that relationship.

A reduced version (12 trials) runs in CI: `tests/unit/test_mirror_bandit.py:577-674`
asserts sublinear regret and that tracking beats classic EXP3. **The exact table above
is not yet reproducible from a committed script**; that is tracked as benchmark work.

`DEFAULT_EXPLORATION_RATE = 0.1` is hardcoded and has **not** been tuned.

---

## 3. Straggler Detection and Range Bisection

**Status: Library** — `reliadl/algorithms/work_stealer.py`

A chunk is a straggler when its projected time-to-completion, `TTC = remaining /
throughput`, exceeds a multiple of the minimum TTC across *active* transfers. The
baseline deliberately excludes idle workers, which would otherwise drag the reference
down and mark healthy transfers as stragglers. A detected straggler's outstanding range
is split at the midpoint of its *unfetched* remainder, not of the original range, so
already-received bytes are not re-requested.

`reliadl/algorithms/task_deque.py` implements a Chase-Lev-shaped work-stealing deque.
Its docstring states plainly that it is lock-based, not the lock-free original
(Chase and Lev, SPAA 2005); it approximates the interface, not the memory-ordering
guarantees.

---

## 4. Merkle Tree Integrity and Signed Manifests

**Status: Library (tree); CLI-only (audit command)** — `reliadl/manifest.py:817-970`

A binary Merkle tree over chunk hashes with domain separation — `SHA-256(0x00 || leaf)`
and `SHA-256(0x01 || left || right)` — so a leaf hash can never be confused with an
internal node. Inclusion proofs (`get_proof`, `verify_inclusion`) are implemented and
compare roots in constant time. Manifests are canonicalized per RFC 8785 and signed
with Ed25519.

**Known weakness — must be fixed before the root is treated as a security boundary.**
Odd node counts duplicate the last node (`manifest.py:890`) and the root does not commit
to the leaf count. This is the CVE-2012-2459 shape: two different chunk lists can
produce the same root. The root *is* signed in the manifest, so this is a live issue.
Fix by binding the leaf count into the root, or by adopting the Certificate
Transparency promotion rule.

`SubBlockStreamValidator` (`manifest.py:1001-1160`) aborts a stream at the first bad
64 KB sub-block. It is implemented and tested but **has no production call site**: the
download engine currently verifies whole chunks (`download_engine.py:511`) and discards
an entire chunk on a single bad byte.

---

## 5. Supporting Mechanisms

| Mechanism | Module | Provenance |
| :--- | :--- | :--- |
| EWMA RTT / throughput estimation, sliding failure window | `algorithms/metrics_collector.py` | Smoothing and variance ordering follow RFC 6298 |
| Token bucket rate limiting | `rate_limiter.py` | Standard. Tokens derive from elapsed time on consultation, so there is no timer to drift or cancel |
| Three-state circuit breaker with capped exponential backoff | `algorithms/mirror_bandit.py` | Standard pattern |
| SOCKS5 and HTTP CONNECT tunneling | `adapters/proxy_adapter.py` | RFC 1928, RFC 1929 |
| Streaming SHA-256/384/512, constant-time compare | `hash_verifier.py` | FIPS 180-4; `hmac.compare_digest` |
| Exponential backoff with jitter | `download_engine.py:643-652` | Standard |

---

## 6. Not Implemented

Earlier revisions of this document described five "novel contributions" that were not
present in the source. They are listed here so their absence is unambiguous, together
with the prior art each would have to be built on and cited against.

| Described previously | Reality | Prior art it would rest on |
| :--- | :--- | :--- |
| AdaChunk via Lyapunov drift-plus-penalty, with an O(1/V)–O(V) theorem | **Not implemented.** `BLDCSController` is explicitly stateless (`adaptive_chunker.py:147-150`); drift-plus-penalty requires a virtual queue carried across slots | Neely (2010) |
| LtHash homomorphic aggregation, O(1) whole-file verification | **Not implemented.** Whole-file verification is an O(N) disk re-read (`hash_verifier.py:179-186`) | Bellare and Micciancio (1997); Lewi et al. (2019) |
| Predictive parity injection under a Gilbert-Elliott channel | **Not implemented.** No FEC of any kind exists | Gilbert (1960); Elliott (1963) |
| Whittle index restless-bandit scheduling, with an indexability theorem | **Not implemented.** The router is EXP3.S, a different algorithm class — no subsidy, no indexability argument | Whittle (1988); Weber and Weiss (1990) |
| Sub-chunk Merkle *localization* (binary search to corrupt segments) | **Not implemented.** The tree exists; the localization algorithm does not | Merkle (1987) |

---

## 7. References

Verify each against the primary source before citing it in a paper. Do not cite a paper
you have not read.

1. P. Auer, N. Cesa-Bianchi, Y. Freund, R. E. Schapire. "The Nonstochastic Multiarmed
   Bandit Problem." *SIAM Journal on Computing* 32(1), 2002. — EXP3, EXP3.S
2. R. C. Merkle. "A Digital Signature Based on a Conventional Encryption Function."
   *CRYPTO 1987*, published 1988.
3. D. Chase, Y. Lev. "Dynamic Circular Work-Stealing Deque." *SPAA*, 2005.
4. M. Bellare, D. Micciancio. "A New Paradigm for Collision-Free Hashing:
   Incrementality at Reduced Cost." *EUROCRYPT*, 1997.
5. K. Lewi, W. Kim, I. Maykov, S. Weis. "Securing Update Propagation with Homomorphic
   Hashing." IACR ePrint 2019/227.
6. M. J. Neely. *Stochastic Network Optimization with Application to Communication and
   Queueing Systems.* Morgan and Claypool, 2010.
7. P. Whittle. "Restless Bandits: Activity Allocation in a Changing World."
   *Journal of Applied Probability* 25, 1988.
8. R. R. Weber, G. Weiss. "On an Index Policy for Restless Bandits."
   *Journal of Applied Probability* 27, 1990.
9. E. N. Gilbert. "Capacity of a Burst-Noise Channel." *Bell System Technical Journal*
   39(5), 1960. E. O. Elliott, *BSTJ* 42(5), 1963.
10. B. Kapoor, K. Patel, P. Kar. "Corruption-tolerant bandit learning."
    *Machine Learning* 108, 2019.
11. T. Lykouris, V. Mirrokni, R. Paes Leme. "Stochastic bandits robust to adversarial
    corruptions." *STOC*, 2018.
12. "Fast End-to-End Integrity Verification for High-Speed File Transfers."
    arXiv:1811.01161 — closest prior work on chunk-level verification during transfer.
13. RFC 6298 (TCP retransmission timer), RFC 1928 and RFC 1929 (SOCKS5), RFC 8785 (JSON
    canonicalization), RFC 9110 (HTTP semantics), RFC 7233 (range requests),
    FIPS 180-4 (SHA-2).
14. CVE-2012-2459 — Merkle odd-node duplication.
