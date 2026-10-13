# Ablations and paper figures

`paper_figures.py` regenerates every figure in the paper. Measured figures read
`benchmarks/results/results.jsonl` at build time, so a figure cannot drift from
the data it claims to show; analytical figures plot the equations directly.

```bash
python benchmarks/ablations/paper_figures.py <output-directory>
```

The routing ablation is the one worth re-running, because it corrected a claim we
had already written down. It runs the shipped `EXP3Bandit` and `MirrorDispatcher`
rather than a reimplementation, and reports the share of requests a fast
corrupting mirror attracts under each mechanism:

```
corrupting-mirror share: reward only 45.5%, reward + breaker 5.4%
```

Uniform allocation over three mirrors is 33%, so an integrity-aware reward on its
own does not demote the corrupting mirror — it prefers it. See
`docs/EVALUATION.md` for the threshold condition that explains why.
