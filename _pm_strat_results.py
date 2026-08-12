#!/usr/bin/env python3
"""Generate PM Strategy comparison HTML section."""
import json
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
QLOG = SCRIPT_DIR / "_model_quality_log.json"
RLOG = SCRIPT_DIR / "_resolved_markets_log.json"

log = json.loads(QLOG.read_text(encoding="utf-8"))
pm = {}
if RLOG.exists():
    rl = json.loads(RLOG.read_text(encoding="utf-8"))
    for k, v in rl.get("markets", {}).items():
        if "||" in k:
            city, date_str = k.split("||", 1)
            pm[(city, date_str)] = float(v["temp_c"])

runs = log.get("runs", [])
if not runs:
    print("No data")
    exit(0)

# Find most recent run with resolved predictions
latest = None
for r in reversed(runs):
    if any(p.get("strategies", {}).get("sigma", {}).get("actual_peak") is not None
           for p in r.get("predictions", {}).values()):
        latest = r
        break
if latest is None:
    latest = runs[-1]
target_date = latest["run_date"]
preds = latest.get("predictions", {})

# Determine best strategy per city (simple heuristic: mean > sigma > p5)
def best_spill(pdata):
    s = pdata.get("strategies", {})
    m = s.get("mean", {}).get("spill")
    if m is not None:
        return m, "mean"
    sig = s.get("sigma", {}).get("spill")
    return sig, "sigma"

rows = ""
wins = losses = 0
for city in sorted(preds):
    pdata = preds[city]
    cb = city.split(",")[0].strip()
    pt = pm.get((city, target_date)) or pm.get((cb, target_date))
    if pt is None:
        continue
    sigma = pdata.get("strategies", {}).get("sigma", {})
    our_peak = sigma.get("actual_peak")
    if our_peak is None:
        continue
    our_round = round(our_peak)
    pm_round = int(round(pt))
    gap = round(our_peak - pt, 1)
    spill, strat = best_spill(pdata)
    is_win = int(spill) == pm_round if spill else False
    if is_win:
        wins += 1
    else:
        losses += 1
    rows += f"<tr><td>{city}</td><td>{our_round}C</td><td>{pm_round}C</td><td>{gap:+.1f}C</td><td>{spill}C ({strat})</td><td>{'WIN' if is_win else 'TAP'}</td></tr>\n"

total = wins + losses
rate = round(wins/max(1,total)*100,1)
print(f"PM Strategy Results ({total} cities): {wins}W/{losses}L = {rate}%")
print(rows)

# Write HTML section
html = f"""<div class="section" style="border-color: rgba(210,153,29,0.4);">
  <h2>🎯 ANBEFALT SPILL vs POLYMARKET ({total} byer, {target_date})</h2>
  <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
    Anbefalt strategispill (mean) sjekket mot Polymarkets faktiske resolusjon.
  </p>
  <div class="card-grid" style="margin-bottom: 12px;">
    <div class="card" style="border: 1px solid var(--green);">
      <div class="value" style="color: var(--green);">{rate}%</div>
      <div class="label">Win Rate vs PM ({wins}W/{losses}L)</div>
    </div>
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
