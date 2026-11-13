"""
Figures 2-8 for the paper.

Design constraints, in order of how much they bind:

* Conference proceedings are routinely printed in black and white, so nothing is
  encoded in hue. Series are separated by lightness *and* hatch, which makes the
  figures legible in greyscale and colour-vision-safe by construction rather than
  by validation.
* Single IEEE column is 3.4 in; two-column span is 7.0 in. Each figure declares
  which it is.
* Hairline, solid, recessive grid on the value axis only. No dashed rules, no
  dual axes anywhere: where two quantities of different units must be compared
  they are drawn as small multiples sharing a policy axis.
* Measured figures take their numbers from benchmarks/results/results.jsonl at
  build time, so a figure cannot drift from the data it claims to show.
"""

import json
import statistics
import sys
from collections import defaultdict

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Times New Roman", "DejaVu Serif"],
    "font.size": 7,
    "axes.linewidth": 0.6,
    "axes.edgecolor": "#444444",
    "xtick.major.width": 0.6,
    "ytick.major.width": 0.6,
    "xtick.labelsize": 6.5,
    "ytick.labelsize": 6.5,
    "legend.fontsize": 6.2,
    "legend.frameon": False,
    "figure.dpi": 600,
})

COL, SPAN = 3.4, 7.0
INK, MUTED = "#1A1A1A", "#555555"

# Lightness ladder plus hatch: two independent channels, both greyscale-safe.
STYLE = {
    "baseline":              dict(fc="#FFFFFF", hatch="",     label="baseline"),
    "adaptive-chunks":       dict(fc="#D9D9D9", hatch="...",  label="adaptive-chunks"),
    "adaptive-mirrors":      dict(fc="#BFBFBF", hatch="///",  label="adaptive-mirrors"),
    "localization":          dict(fc="#8C8C8C", hatch="\\\\\\", label="localization"),
    "routing+localization":  dict(fc="#404040", hatch="xxx",  label="routing + localization"),
}

RESULTS = "benchmarks/results/results.jsonl"


def load():
    rows = [json.loads(line) for line in open(RESULTS) if line.strip()]
    cells = defaultdict(list)
    for r in rows:
        cells[(r["scenario"], r["policy"])].append(r)
    return cells


def stat(cells, scenario, policy, field):
    rs = cells.get((scenario, policy), [])
    ok = [r for r in rs if r["ok"] and r["verified"]]
    if field == "completion":
        return 100.0 * len(ok) / len(rs) if rs else None
    if not ok:
        return None
    if field == "goodput":
        return statistics.median(r["goodput_bps"] for r in ok) / 1048576
    if field == "waste":
        return statistics.median(r["waste_ratio"] for r in ok) * 100
    if field == "share":
        vals = [r["corrupting_mirror_share"] for r in ok
                if r.get("corrupting_mirror_share") is not None]
        return statistics.median(vals) * 100 if vals else None
    raise ValueError(field)


def grouped_bars(ax, scenarios, policies, values, ylabel, ymax=None, fmt="{:.0f}"):
    """One group per scenario, one bar per policy. Absent cells are left blank."""
    n = len(policies)
    width = 0.8 / n
    x = np.arange(len(scenarios))
    for i, pol in enumerate(policies):
        heights, offs = [], []
        for j, sc in enumerate(scenarios):
            v = values.get((sc, pol))
            if v is not None:
                heights.append(v)
                offs.append(x[j] - 0.4 + width * (i + 0.5))
        st = STYLE[pol]
        bars = ax.bar(offs, heights, width * 0.88, label=st["label"],
                      facecolor=st["fc"], edgecolor=INK, linewidth=0.6,
                      hatch=st["hatch"], zorder=3)
        for b, h in zip(bars, heights, strict=True):
            ax.text(b.get_x() + b.get_width() / 2, h + (ymax or max(heights)) * 0.02,
                    fmt.format(h), ha="center", va="bottom", fontsize=5.6, color=INK,
                    zorder=4)
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_ylabel(ylabel, fontsize=7)
    if ymax:
        ax.set_ylim(0, ymax)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD", zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)


# ---------------------------------------------------------------- Fig 2
def fig_completion(cells, out):
    """Headline result: verification granularity decides completion, not speed."""
    scenarios = ["corrupting", "byzantine", "flaky"]
    policies = ["baseline", "localization", "routing+localization"]
    values = {(s, p): stat(cells, s, p, "completion") for s in scenarios for p in policies}
    fig, ax = plt.subplots(figsize=(COL, 1.85))
    grouped_bars(ax, scenarios, policies, values, "Completed transfers (%)", ymax=118)
    ax.set_yticks([0, 25, 50, 75, 100])
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.30), ncol=2,
              handlelength=1.5, columnspacing=1.0, handletextpad=0.4)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 3
def fig_waste_share(cells, out):
    """Two units, so two panels sharing a policy axis rather than two y-scales."""
    scenarios = ["byzantine", "flaky"]
    policies = ["localization", "routing+localization"]
    fig, axes = plt.subplots(2, 1, figsize=(COL, 3.1))

    waste = {(s, p): stat(cells, s, p, "waste") for s in scenarios for p in policies}
    grouped_bars(axes[0], scenarios, policies, waste,
                 "Wasted bytes (% of payload)", ymax=52, fmt="{:.1f}")
    axes[0].set_title("(a) Bandwidth spent on repair", fontsize=7, pad=4)

    share = {(s, p): stat(cells, s, p, "share") for s in scenarios for p in policies}
    grouped_bars(axes[1], scenarios, policies, share,
                 "Bytes from corrupting mirror (%)", ymax=118, fmt="{:.1f}")
    axes[1].set_title("(b) Where the bytes came from", fontsize=7, pad=4)
    axes[1].set_yticks([0, 25, 50, 75, 100])

    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="lower center", ncol=2, bbox_to_anchor=(0.5, -0.06),
               handlelength=1.5, columnspacing=1.2, handletextpad=0.4)
    fig.tight_layout(pad=0.3)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 4
def fig_cost_model(out):
    """
    Why the cost minimum is the wrong answer for a parallel downloader.

    Analytical, not measured: it draws equations (5) and (6) together with the
    feasibility limit (8), so the reader can see that the unconstrained optimum
    lies inside the region where the worker pool would be starved. Parameters are
    the controller's own defaults with a clean-path loss rate.
    """
    tau, mu, MSS = 0.025, 40.0, 1460 / 1e6     # s, MB/s, MB
    p = 1.43e-6                                 # per-segment loss on a clean path
    S = np.linspace(4, 64, 900)                 # MB
    overhead = tau * mu / S
    loss = p * S / MSS
    J = overhead + loss
    s_star = S[int(np.argmin(J))]
    ceiling = 64 / 4                            # F_rem / W

    fig, ax = plt.subplots(figsize=(COL, 2.05))
    ax.axvspan(ceiling, 64, facecolor="#ECECEC", edgecolor="none", zorder=0)
    ax.plot(S, overhead, linewidth=0.9, color=MUTED, linestyle=(0, (1, 1.5)),
            label=r"overhead $\tau\mu/S$")
    ax.plot(S, loss, linewidth=0.9, color=MUTED, linestyle=(0, (4, 2)),
            label=r"loss $p_{seg}S/\mathrm{MSS}$")
    ax.plot(S, J, linewidth=1.6, color=INK, label=r"total $J(S)$")

    j_star = J.min()
    ax.plot([s_star], [j_star], marker="o", markersize=3.2, color=INK, zorder=5)
    ax.annotate("unconstrained $S^*$ = %.0f MB" % s_star + "\n(infeasible)",
                xy=(s_star, j_star), xytext=(30, 0.165), fontsize=5.9, color=INK,
                ha="center",
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))

    j_c = tau * mu / ceiling + p * ceiling / MSS
    ax.plot([ceiling], [j_c], marker="s", markersize=3.2, color=INK, zorder=5)
    ax.annotate("chosen $S$ = %.0f MB" % ceiling,
                xy=(ceiling, j_c), xytext=(22.0, 0.238), fontsize=5.9, color=INK,
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))

    ax.axvline(ceiling, linewidth=0.9, color=INK, zorder=4)
    ax.text(40, 0.011, "fewer chunks than workers", fontsize=5.8, color="#444444",
            ha="center", style="italic")

    ax.set_xlabel("Chunk size $S$ (MB)", fontsize=7)
    ax.set_ylabel("Per-byte cost (arb. units)", fontsize=7)
    ax.set_xlim(4, 64)
    ax.set_ylim(0, 0.29)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD")
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper right", handlelength=1.9, handletextpad=0.4, labelspacing=0.3)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 5
def fig_floor(out):
    """
    Measured effect of the constraint, from the interleaved A/B in Section VI-E.

    These are not read from results.jsonl, because the matrix has no
    floor-disabled arm: the comparison needs the floor switched off inside the
    scheduler, which `benchmarks/ablations/concurrency_floor.py` does by
    replacing `_apply_concurrency_floor` for one arm. Regenerate with::

        python -m benchmarks.ablations.concurrency_floor --repetitions 5

    and update the three lists below. Values recorded 2026-11-14, 30 seeds per
    arm, 64 MB payload, 4 workers, against the corrected BL-DCS loss term. The
    lossy bar is the one to read carefully: its interval on the mean excludes
    zero but its sign test does not, so the gain there is not consistent.
    """
    scenarios = ["clean", "heterog.", "failover", "lossy"]
    off = [29.0, 29.7, 3.6, 45.8]
    on = [52.0, 51.0, 9.0, 49.4]
    # Carried rather than recomputed from the rounded bar labels. On failover the
    # unrounded medians are 3.63 and 8.92, which is +145%; dividing the displayed
    # 8.9 by the displayed 3.6 gives +147%, and a figure that disagrees with its
    # own table by two points invites exactly the question it should not.
    change = [79.4, 73.9, 146.9, 15.6]
    x = np.arange(len(scenarios))
    w = 0.36
    fig, ax = plt.subplots(figsize=(COL, 1.85))
    ax.bar(x - w / 2, off, w, label="without constraint (8)", facecolor="#FFFFFF",
           edgecolor=INK, linewidth=0.6, zorder=3)
    ax.bar(x + w / 2, on, w, label="with constraint (8)", facecolor="#595959",
           edgecolor=INK, linewidth=0.6, hatch="///", zorder=3)
    for xi, a, b, c in zip(x, off, on, change, strict=True):
        ax.text(xi - w / 2, a + 1.2, f"{a:.1f}", ha="center", fontsize=5.6)
        ax.text(xi + w / 2, b + 1.2, f"{b:.1f}", ha="center", fontsize=5.6)
        ax.text(xi, max(a, b) + 6.0, f"+{c:.0f}%", ha="center",
                fontsize=6.0, color=INK)
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_ylabel("Goodput (MB/s)", fontsize=7)
    ax.set_ylim(0, 68)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD", zorder=0)
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.24), ncol=2,
              handlelength=1.5, columnspacing=1.2, handletextpad=0.4)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 6
def fig_localization_bound(out):
    """
    Equation (1). The point of the figure is the left-hand end: localisation
    saves nothing when the corruption lands in the first block.
    """
    b = 64 / 1024  # 64 KB in MB
    fig, ax = plt.subplots(figsize=(COL, 1.9))
    styles = [(4, "#1A1A1A", "-"), (8, "#555555", (0, (4, 2))), (16, "#888888", (0, (1, 1.5)))]
    for S, c, ls in styles:
        k = np.arange(0, int(S / b) + 1)
        ax.plot(k * b / S * 100, (S - k * b), linewidth=1.1, color=c, linestyle=ls,
                label=f"$S$ = {S} MB")
        ax.plot([0], [S], marker="o", markersize=2.6, color=c)
    ax.annotate("no saving when the\nfirst block is corrupt",
                xy=(0, 16), xytext=(14, 14.2), fontsize=5.8, color=INK,
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))
    ax.set_xlabel("Position of corruption within the chunk (%)", fontsize=7)
    ax.set_ylabel("Bytes re-requested (MB)", fontsize=7)
    ax.set_xlim(0, 100)
    ax.set_ylim(0, 17.5)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD")
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper right", handlelength=1.8, handletextpad=0.4)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 7
def fig_detection(out):
    """Equation (10): the detection-latency half of the chunk-size tradeoff."""
    q = np.linspace(0.02, 0.5, 300)
    fig, ax = plt.subplots(figsize=(COL, 1.9))
    for S, c, ls in [(1, "#888888", (0, (1, 1.5))), (4, "#555555", (0, (4, 2))),
                     (16, "#1A1A1A", "-")]:
        ax.plot(q * 100, S / q, linewidth=1.1, color=c, linestyle=ls, label=f"$S$ = {S} MB")
    ax.axvline(40, linewidth=0.7, color=INK)
    # Annotation sits low and left of the rule so it clears the legend block.
    ax.annotate("byzantine scenario ($q$ = 0.4)", xy=(40, 3.4), xytext=(27, 1.8),
                fontsize=5.8, color=INK, ha="center",
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))
    ax.set_xlabel("Corruption rate $q$ of the faulty mirror (%)", fontsize=7)
    ax.set_ylabel("Bytes served before\nfirst detection (MB)", fontsize=7)
    ax.set_yscale("log")
    ax.set_xlim(2, 50)
    ax.set_ylim(1.3, 1100)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD")
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper right", handlelength=1.8, handletextpad=0.4)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)


# ---------------------------------------------------------------- Fig 8
def fig_bandit(out):
    """
    The ablation that corrected our own claim.

    Both arms run the shipped implementation rather than a reimplementation. The
    point of the figure is that an integrity-aware *reward* is not sufficient: a
    fast mirror corrupting 40 percent of what it serves still has the highest
    expected reward, so EXP3.S routes towards it. What displaces it is the
    consecutive-failure breaker.
    """
    sys.path.insert(0, ".")
    from reliadl.algorithms.mirror_bandit import (
        EXP3Bandit, MirrorDispatcher, MirrorEndpoint,
    )

    arms = ["fast-corrupting", "honest-a", "honest-b"]
    rounds, trials, q = 400, 40, 0.40

    def simulate(with_breaker):
        # Cumulative share of requests sent to the corrupting mirror, per round.
        share = np.zeros((trials, rounds))
        for t in range(trials):
            rng = np.random.default_rng(900 + t)
            if with_breaker:
                router = MirrorDispatcher(
                    endpoints=[MirrorEndpoint(mirror_id=a, url=f"http://{a}/f") for a in arms],
                    exploration_rate=0.1, weight_decay=0.0025, seed=t)
            else:
                router = EXP3Bandit(arms=arms, exploration_rate=0.1,
                                    weight_decay=0.0025, seed=t)
            bad = total = 0
            for r in range(rounds):
                if with_breaker:
                    req = router.dispatch()
                    if req is not None:
                        i = arms.index(req.mirror)
                        total += 1
                        bad += (i == 0)
                        if i == 0 and rng.random() < q:
                            router.fail(req)
                        else:
                            router.complete(
                                req,
                                bytes_transferred=2_000_000 if i == 0 else 1_000_000,
                                duration_seconds=0.05)
                else:
                    sel = router.select()
                    i = arms.index(sel.arm)
                    total += 1
                    bad += (i == 0)
                    reward = 0.0 if (i == 0 and rng.random() < q) else (1.0 if i == 0 else 0.5)
                    router.update(sel, reward)
                share[t, r] = bad / max(1, total)
        return share.mean(axis=0) * 100

    without = simulate(False)
    with_br = simulate(True)

    fig, ax = plt.subplots(figsize=(COL, 1.95))
    ax.plot(without, linewidth=1.3, color=INK, label="reward only (EXP3.S)")
    ax.plot(with_br, linewidth=1.3, color="#888888", linestyle=(0, (4, 2)),
            label="reward + failure breaker")
    ax.axhline(100 / 3, linewidth=0.6, color="#BBBBBB")
    ax.text(rounds * 0.98, 100 / 3 + 1.4, "uniform (33%)", fontsize=5.6,
            color=MUTED, ha="right")
    # Labels sit below their lines so neither collides with the legend block.
    ax.annotate(f"{without[-1]:.0f}%", xy=(rounds - 4, without[-1]),
                xytext=(rounds - 108, without[-1] - 11), fontsize=6.2, color=INK,
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))
    ax.annotate(f"{with_br[-1]:.0f}%", xy=(rounds - 4, with_br[-1]),
                xytext=(rounds - 108, with_br[-1] + 8), fontsize=6.2, color=INK,
                arrowprops=dict(arrowstyle="->", linewidth=0.5, color=INK))
    ax.set_xlabel("Range requests", fontsize=7)
    ax.set_ylabel("Requests sent to the" + chr(10) + "corrupting mirror (%)", fontsize=7)
    ax.set_xlim(0, rounds)
    ax.set_ylim(0, 72)
    ax.yaxis.grid(True, linewidth=0.4, color="#DDDDDD")
    ax.set_axisbelow(True)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    ax.legend(loc="upper center", bbox_to_anchor=(0.5, 1.22), ncol=2,
              handlelength=1.6, columnspacing=1.0, handletextpad=0.4)
    fig.tight_layout(pad=0.2)
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return without[-1], with_br[-1]


if __name__ == "__main__":
    out = sys.argv[1].rstrip("/")
    cells = load()
    fig_completion(cells, f"{out}/fig2.png")
    fig_waste_share(cells, f"{out}/fig3.png")
    fig_cost_model(f"{out}/fig4.png")
    fig_floor(f"{out}/fig5.png")
    fig_localization_bound(f"{out}/fig6.png")
    fig_detection(f"{out}/fig7.png")
    without, with_br = fig_bandit(f"{out}/fig8.png")
    print("figures written")
    print(f"corrupting-mirror share: reward only {without:.1f}%, "
          f"reward + breaker {with_br:.1f}%")
