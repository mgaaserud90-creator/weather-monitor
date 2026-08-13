#!/usr/bin/env python3
"""Generate PM Strategy comparison HTML section.

Compares each city's recommended spill against Polymarket's resolved market
using the unit-aware loader from _model_quality_tracker. Threshold markets are
never treated as point temperatures, US °F bucket markets are compared against
their inclusive native °F range (not a rounded midpoint), and cities that were
predicted but have no resolvable market are surfaced explicitly as NA.
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _model_quality_tracker import _load_market_resolved_details  # noqa: E402

QLOG = SCRIPT_DIR / "_model_quality_log.json"

log = json.loads(QLOG.read_text(encoding="utf-8"))

# Unit-aware resolved markets: {(city, date): info}. Threshold markets are
# already excluded by the loader.
pm = _load_market_resolved_details()

runs = log.get("runs", [])
if not runs:
    print("No data")
    sys.exit(0)

# Find run with the MOST resolved predictions (ground truth comparisons)
latest = None
max_resolved = -1
for r in runs:
    n = sum(1 for p in r.get("predictions", {}).values()
            if p.get("strategies", {}).get("sigma", {}).get("actual_peak") is not None)
    if n > max_resolved:
        max_resolved = n
        latest = r
if latest is None:
    latest = runs[-1]
target_date = latest["run_date"]
preds = latest.get("predictions", {})


def best_spill(pdata):
    s = pdata.get("strategies", {})
    m = s.get("mean", {}).get("spill")
    return m, "mean"


def market_label(info):
    unit = (info.get("unit") or "C").upper()
    bucket = info.get("bucket") or ""
    if bucket:
        return bucket
    value = info.get("value")
    if value is None:
        return "—"
    return f"{value:.0f}°{unit}"


rows = ""
wins = losses = na_count = 0
na_cities = []
winner_cities = []
for city in sorted(preds):
    pdata = preds[city]
    cb = city.split(",")[0].strip()
    # Match each city against its own target date (timezone-shifted cities such
    # as Wellington target the following day), falling back to the run date.
    city_target = pdata.get("_target_date", target_date)
    info = pm.get((city, city_target)) or pm.get((cb, city_target))
    if info is None or info.get("value") is None:
        # City has no resolved POINT market for target_date — surface it.
        na_count += 1
        na_cities.append(city)
        continue

    sigma = pdata.get("strategies", {}).get("sigma", {})
    our_peak = sigma.get("actual_peak")
    spill, strat = best_spill(pdata)

    if spill is None:
        # Mean strategy has no spill for this city — surface it as NA.
        na_count += 1
        na_cities.append(city)
        continue

    if our_peak is None:
        # Market exists but the model produced no resolved peak.
        na_count += 1
        na_cities.append(city)
        continue

    unit = (info.get("unit") or "C").upper()
    value = float(info["value"])
    value_c = value if unit == "C" else (value - 32) * 5 / 9
    lo_c = info.get("lo_c")
    hi_c = info.get("hi_c")
    lo_f = info.get("lo_f")
    hi_f = info.get("hi_f")

    if lo_f is not None and hi_f is not None:
        # Compare in native °F against the bucket's inclusive range.
        is_win = lo_f <= float(spill) * 9.0 / 5.0 + 32.0 <= hi_f
    elif lo_c is not None and hi_c is not None:
        # Fall back to the bucket's inclusive °C range.
        is_win = float(lo_c) <= float(spill) <= float(hi_c)
    else:
        is_win = int(spill) == int(round(value_c))

    if is_win:
        wins += 1
        winner_cities.append(city)
    else:
        losses += 1

    our_round = round(our_peak)
    gap = round(our_peak - value_c, 1)
    label = market_label(info)
    rows += (
        f"<tr><td>{city}</td><td>{our_round}C</td><td>{label}</td>"
        f"<td>{gap:+.1f}C</td><td>{spill}C ({strat})</td>"
        f"<td style='color:{'var(--green)' if is_win else 'var(--red)'};'>"
        f"{'WIN' if is_win else 'TAP'}</td></tr>\n"
    )

total = wins + losses
rate = round(wins / max(1, total) * 100, 1)
print(f"PM Strategy Results ({total} resolved, {na_count} NA): {wins}W/{losses}L = {rate}%")
if na_cities:
    print("  NA (predicted but no resolved point market): " + ", ".join(na_cities))
print(rows)

# Write HTML section
html = f"""<div class="section" style="border-color: rgba(210,153,29,0.4);">
  <h2>🎯 ANBEFALT SPILL vs POLYMARKET ({total} resolved, {na_count} NA, {target_date})</h2>
  <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
    Anbefalt strategispill (mean) sjekket mot Polymarkets faktiske resolusjon.
    US °F-buckets sammenlignes mot sitt inklusive °C-område.
  </p>
  <div class="card-grid" style="margin-bottom: 12px;">
    <div class="card" style="border: 1px solid var(--green);">
      <div class="value" style="color: var(--green);">{rate}%</div>
      <div class="label">Win Rate vs PM ({wins}W/{losses}L)</div>
    </div>
  </div>
  <div style="margin: 12px 0; padding: 12px; border-radius: 8px; background: var(--bg-card); font-size: 0.9rem; line-height: 1.5;">
    <strong>Vinnere vs Polymarket ({len(winner_cities)}):</strong>
    {", ".join(winner_cities) if winner_cities else "—"}
  </div>
  <div style="max-height: 600px; overflow-y: auto;">
  <table>
    <thead><tr><th>By</th><th>Var Peak (round)</th><th>PM Resolved</th><th>Avvik</th><th>Anbefalt Spill</th><th>Vinner?</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  </div>
</div>"""
out_path = SCRIPT_DIR / "_pm_strat_section.html"
out_path.write_text(html, encoding="utf-8")
print(f"HTML written to {out_path}")
