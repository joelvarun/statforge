"""The before and after figure, drawn from measured numbers only.

Two panels, one axis each.

The top panel is the headline: total measured workload time per configuration,
horizontal bars from a zero baseline, sorted worst to best. Every bar carries
its value as a direct label, which is needed twice over: the range spans a
factor of 217, so the fast bars are slivers, and the aqua slot sits below 3:1
contrast on this surface, which obliges visible labels.

The bottom panel is the per-query comparison. Those times span six orders of
magnitude, so it has to be a log axis, and a log axis rules out bars: a bar
encodes magnitude by length from zero, and a log length means nothing. A
dumbbell encodes by position instead, which is legitimate on a log scale, and
it shows the size and the direction of every change at once. The baseline
marker is drawn larger than the forged one so that a query whose time did not
move still shows both series, as a ring around a core, rather than looking
like a dropped data point.

Colour classes carry kind, not rank: statistics-only configurations, the one
this project produces, and the one that has to rewrite the application's SQL.
"""

import textwrap

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import FuncFormatter

SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK2 = "#52514e"
GRID = "#dedcd6"
BLUE = "#2a78d6"
AQUA = "#1baf7a"
ORANGE = "#eb6834"
KIND_COLOR = {"baseline": BLUE, "statforge": AQUA, "rewrites_sql": ORANGE}
KIND_LABEL = {
    "baseline": "statistics only, existing practice",
    "statforge": "statistics only, searched (this project)",
    "rewrites_sql": "requires rewriting every query",
}


def _seconds(x, _pos=None):
    if x >= 1000:
        return "%g s" % (x / 1000.0)
    return "%g" % x


def _millis(x, _pos=None):
    if x >= 1000:
        return "%g" % x
    if x >= 1:
        return "%g" % x
    return "%g" % x


def _value(ms):
    if ms >= 1000:
        return "%.1f s" % (ms / 1000.0)
    if ms >= 1:
        return "%.0f ms" % ms
    return "%.2f ms" % ms


def _ratio(base_ms, ms):
    r = base_ms / ms
    if 0.98 <= r <= 1.02:
        return "no change"
    if r >= 2.0:
        return "%.0fx faster" % r
    if r > 1.0:
        return "%.2fx faster" % r
    return "%.2fx slower" % (1.0 / r)


def draw(path, totals, per_query, subtitle, baseline_label="ANALYZE"):
    """totals: [(label, ms, kind)]. per_query: [(name, baseline_ms, forged_ms)]."""
    fig = plt.figure(figsize=(12.6, 10.0), facecolor=SURFACE)
    gs = fig.add_gridspec(2, 1, height_ratios=[1.0, 1.3], hspace=0.32,
                          left=0.275, right=0.95, top=0.855, bottom=0.08)

    ranked = sorted(totals, key=lambda t: -t[1])
    base_ms = dict((t[0], t[1]) for t in ranked)[baseline_label]
    ax = fig.add_subplot(gs[0])
    ax.set_facecolor(SURFACE)
    ys = list(range(len(ranked)))[::-1]
    top = max(t[1] for t in ranked)
    for y, (label, ms, kind) in zip(ys, ranked):
        ax.barh([y], [ms], height=0.6, color=KIND_COLOR[kind], zorder=3)
        txt = "(baseline)" if label == baseline_label else _ratio(base_ms, ms)
        ax.text(ms + top * 0.012, y, "%s   %s" % (_value(ms), txt),
                va="center", ha="left", fontsize=10.5, color=INK, zorder=4)
    ax.set_yticks(ys)
    ax.set_yticklabels([t[0] for t in ranked], fontsize=10.5, color=INK)
    ax.set_xlim(0, top * 1.34)
    ax.xaxis.set_major_formatter(FuncFormatter(_seconds))
    ax.set_xlabel("total measured time for the whole workload, seconds"
                  " (lower is better)", fontsize=10.5, color=INK2)
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.spines["bottom"].set_color(GRID)
    ax.tick_params(colors=INK2, length=0)
    ax.set_title("Same SQL, same indexes, same data: only sqlite_stat1 differs",
                 fontsize=13, color=INK, loc="left", pad=11)
    kinds = []
    for _l, _m, k in ranked:
        if k not in kinds:
            kinds.append(k)
    ax.legend(handles=[Line2D([], [], marker="s", linestyle="none",
                              markersize=9, color=KIND_COLOR[k],
                              label=KIND_LABEL[k]) for k in kinds],
              loc="lower right", frameon=False, fontsize=9.5, labelcolor=INK)

    ax2 = fig.add_subplot(gs[1])
    ax2.set_facecolor(SURFACE)
    rows = sorted(per_query, key=lambda r: -r[1])
    idx = list(range(len(rows)))[::-1]
    for y, (name, a, b) in zip(idx, rows):
        ax2.plot([min(a, b), max(a, b)], [y, y], color=GRID, linewidth=2.6,
                 solid_capstyle="round", zorder=2)
        ax2.plot([a], [y], marker="o", markersize=11.5, color=BLUE,
                 markeredgecolor=SURFACE, markeredgewidth=1.6, zorder=3)
        ax2.plot([b], [y], marker="o", markersize=6.5, color=AQUA,
                 markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=4)
        r = a / b if b else 0.0
        if r >= 2.0 or r <= 0.5:
            ax2.text(max(a, b) * 1.45, y, _ratio(a, b), va="center",
                     ha="left", fontsize=9.5, color=INK2, zorder=5)
    lo = min(min(r[1], r[2]) for r in rows)
    hi = max(max(r[1], r[2]) for r in rows)
    ax2.set_xscale("log")
    ax2.set_xlim(lo * 0.45, hi * 14.0)
    ax2.set_yticks(idx)
    ax2.set_yticklabels([r[0] for r in rows], fontsize=9.5, color=INK)
    ax2.xaxis.set_major_formatter(FuncFormatter(_millis))
    ax2.set_xlabel("measured time per query, milliseconds, log scale"
                   " (lower is better)", fontsize=10.5, color=INK2)
    ax2.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax2.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax2.spines[s].set_visible(False)
    ax2.spines["bottom"].set_color(GRID)
    ax2.tick_params(colors=INK2, length=0)
    slower = sum(1 for _n, a, b in rows if b > a * 1.02)
    ax2.set_title("Per query, worst first. %s: one shared estimate cannot"
                  " serve a rare constant and a common one"
                  % ("%d queries get slower" % slower if slower != 1
                     else "one query gets slower"),
                  fontsize=11.5, color=INK, loc="left", pad=10)
    ax2.legend(handles=[
        Line2D([], [], marker="o", linestyle="none", markersize=10.5,
               color=BLUE, label=baseline_label),
        Line2D([], [], marker="o", linestyle="none", markersize=7,
               color=AQUA, label="statforge")],
        loc="lower right", frameon=False, fontsize=10, labelcolor=INK, ncol=2)

    fig.text(0.275, 0.965, "statforge: tuning a SQLite query plan by tuning"
             " the statistics", fontsize=16, color=INK, ha="left", va="center")
    for i, line in enumerate(textwrap.wrap(subtitle, 96)[:2]):
        fig.text(0.275, 0.936 - i * 0.021, line, fontsize=9.8, color=INK2,
                 ha="left", va="center")
    fig.savefig(path, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return path
