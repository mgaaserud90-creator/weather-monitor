"""Throwaway consistency analysis for _peak_deviation_log.json.

Tel Aviv plot + per-city consistency table. Does not modify project files
other than saving the PNG.
"""
import json
import statistics
from collections import defaultdict
from pathlib import Path

import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = Path(r"C:/Users/PC/Desktop/vær monitor")
LOG = BASE / "_peak_deviation_log.json"
PNG = BASE / "_telaviv_peak_vs_resolution.png"

data = json.loads(LOG.read_text(encoding="utf-8"))
samples = data["samples"]

# ---------------------------------------------------------------------------
# 1. TEL AVIV
# ---------------------------------------------------------------------------
ta = [s for s in samples if s["city"] == "Tel Aviv, IL"]
ta.sort(key=lambda s: s["date"])
print("=" * 80)
print(f"TEL AVIV  (n = {len(ta)}, unit = {ta[0]['unit'] if ta else '?'})")
print("=" * 80)
print(f"{'date':<12} {'our_peak':>10} {'market_resolved':>16} {'gap':>8}")
for s in ta:
    print(f"{s['date']:<12} {s['our_peak']:>10.2f} {s['market_resolved']:>16.2f} {s['gap']:>8.2f}")

# ---------------------------------------------------------------------------
# 2. TEL AVIV PLOT
# ---------------------------------------------------------------------------
dates = [s["date"] for s in ta]
our = [s["our_peak"] for s in ta]
mr = [s["market_resolved"] for s in ta]
gap = [s["gap"] for s in ta]

x = np.arange(len(dates))
fig, ax1 = plt.subplots(figsize=(13, 7))
fig.suptitle("Tel Aviv, IL — our_peak vs market_resolved (n=26)", fontsize=13, fontweight="bold")

ax1.plot(x, our, "o-", color="#1f77b4", label="our_peak (our mean spill)", markersize=7, linewidth=2)
ax1.plot(x, mr, "s-", color="#d62728", label="market_resolved", markersize=7, linewidth=2)
ax1.set_ylabel("Temperature (native unit)")
ax1.set_xlabel("Date")
ax1.set_xticks(x)
ax1.set_xticklabels(dates, rotation=45, ha="right", fontsize=8)
ax1.grid(True, linestyle="--", alpha=0.4)
ax1.legend(loc="upper left")

# secondary axis: gap (our_peak - market_resolved)
ax2 = ax1.twinx()
bars = ax2.bar(x, gap, alpha=0.35, color="gray", label="gap (our_peak − market_resolved)")
ax2.plot(x, gap, "k.-", linewidth=1, markersize=4)
ax2.set_ylabel("Gap (native unit)")
ax2.axhline(0, color="black", linewidth=0.8)
# annotate gap values
for xi, g in zip(x, gap):
    ax2.annotate(f"{g:+.1f}", (xi, g), textcoords="offset points",
                 xytext=(0, 6 if g >= 0 else -12), ha="center", fontsize=7, color="black")

ax2.legend(loc="upper right")
fig.tight_layout(rect=[0, 0, 1, 0.96])
fig.savefig(PNG, dpi=150, bbox_inches="tight")
plt.close(fig)
print(f"\nSaved plot -> {PNG}")

# ---------------------------------------------------------------------------
# 3. TEL AVIV CONSISTENCY METRICS
# ---------------------------------------------------------------------------
gaps = np.array([s["gap"] for s in ta], dtype=float)
n = len(gaps)
mean_gap = float(np.mean(gaps))
median_gap = float(np.median(gaps))
std_gap = float(np.std(gaps, ddof=1)) if n > 1 else 0.0

if median_gap > 0:
    same_sign = int(np.sum(gaps > 0))
elif median_gap < 0:
    same_sign = int(np.sum(gaps < 0))
else:
    same_sign = int(np.sum(gaps == 0))
same_pct = 100.0 * same_sign / n if n else 0.0
win_count = int(np.sum(np.abs(gaps) <= 0.5))
direction = "over-predicts" if median_gap > 0 else ("under-predicts" if median_gap < 0 else "balanced")

print("\nTEL AVIV CONSISTENCY METRICS")
print("-" * 40)
print(f"n                        = {n}")
print(f"mean gap (native)        = {mean_gap:+.3f}")
print(f"median gap (native)      = {median_gap:+.3f}")
print(f"std of gap (native)      = {std_gap:.3f}")
print(f"same-sign days           = {same_sign}/{n}  ({same_pct:.1f}%)  [{direction}]")
print(f"|gap| <= 0.5 (would win) = {win_count}/{n}  ({100.0*win_count/n:.1f}%)")

# ---------------------------------------------------------------------------
# 4. PER-CITY CONSISTENCY TABLE (n >= 15)
# ---------------------------------------------------------------------------
cities = defaultdict(list)
for s in samples:
    cities[s["city"]].append(s)

rows = []
for city, ss in cities.items():
    if len(ss) < 15:
        continue
    gc = np.array([s["gap_c"] for s in ss], dtype=float)
    cn = len(gc)
    cmean = float(np.mean(gc))
    cstd = float(np.std(gc, ddof=1)) if cn > 1 else 0.0
    cmedian = float(np.median(gc))
    if cmedian > 0:
        agree = int(np.sum(gc > 0))
    elif cmedian < 0:
        agree = int(np.sum(gc < 0))
    else:
        agree = int(np.sum(gc == 0))
    agree_pct = 100.0 * agree / cn
    # STABLE: sign consistent (>= 75%) AND std small relative to the bias
    # (std <= max(0.5 C, |mean|) -> the day-to-day scatter does not dominate
    # the bias). NOISE: sign flips OR scatter larger than the bias.
    cls = "STABLE" if (agree_pct >= 75.0 and cstd <= max(0.5, abs(cmean))) else "NOISE"
    rows.append((city, cn, cmean, cstd, agree_pct, cls))

rows.sort(key=lambda r: abs(r[2]), reverse=True)

print("\n" + "=" * 90)
print("PER-CITY CONSISTENCY TABLE (n >= 15; gaps in degC via gap_c)")
print("Classification: STABLE = sign-agreement >= 75% AND std <= max(0.5, |mean|) C")
print("                 (bias dominates day-to-day scatter); else NOISE (sign flips).")
print("=" * 90)
print(f"{'city':<22} {'n':>4} {'mean gap(C)':>11} {'std gap(C)':>10} {'sign-agree%':>11}  {'class':<7}")
print("-" * 90)
for city, cn, cmean, cstd, agree_pct, cls in rows:
    print(f"{city:<22} {cn:>4} {cmean:>+11.3f} {cstd:>10.3f} {agree_pct:>10.1f}%  {cls:<7}")
print("=" * 90)
