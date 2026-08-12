#!/usr/bin/env python3
"""
Polymarket Strategy Comparison
===============================
Computes win/loss per strategy checked against Polymarket's resolved
temperature (NOT our archive peak). This is the ground truth comparison.
"""
import json, sys
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
QLOG = _SCRIPT_DIR / "_model_quality_log.json"
RLOG = _SCRIPT_DIR / "_resolved_markets_log.json"
OUTFILE = _SCRIPT_DIR / "_pm_strategy_results.json"

log = json.loads(QLOG.read_text(encoding="utf-8"))
runs = log.get("runs", [])

pm_temps = {}
if RLOG.exists():
    rl = json.loads(RLOG.read_text(encoding="utf-8"))
    for key_str, data in rl.get("markets", {}).items():
        if "||" in key_str:
            city, date_str = key_str.split("||", 1)
            pm_temps[(city, date_str)] = float(data["temp_c"])

results = []
for run in runs:
    rd = run.get("run_date", "")
    preds = run.get("predictions", {})
    sw = sl = pw = pl = mw = ml = 0
    matched = 0

    for city, pdata in sorted(preds.items()):
        cb = city.split(",")[0].strip()
        pt = pm_temps.get((city, rd)) or pm_temps.get((cb, rd))
        if pt is None:
            continue
        matched += 1
        actual = int(round(pt))

        s = pdata.get("strategies", {}).get("sigma", {})
        p = pdata.get("strategies", {}).get("p5", {})
        m = pdata.get("strategies", {}).get("mean", {})

        if s.get("spill") is not None:
            if int(s["spill"]) == actual: sw += 1
            else: sl += 1
        if p.get("spill") is not None:
            if int(p["spill"]) == actual: pw += 1
            else: pl += 1
        if m.get("spill{
