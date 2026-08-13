#!/usr/bin/env python3
"""
Trading Data Consolidator
=========================
Reads all existing logs and produces clean, analysis-ready CSV files.
ZERO API calls — purely reorganizing existing data.

Outputs:
  _trading_data.csv         One row per city-day: predictions, actuals, deltas
  _strategy_summary.csv     Aggregated win rates per strategy
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
QUALITY_LOG = _SCRIPT_DIR / "_model_quality_log.json"
RESOLVED_LOG = _SCRIPT_DIR / "_resolved_markets_log.json"
TRADING_CSV = _SCRIPT_DIR / "_trading_data.csv"
STRATEGY_CSV = _SCRIPT_DIR / "_strategy_summary.csv"


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def load_resolved_markets() -> dict[tuple[str, str], float]:
    """Load resolved market temps: {(city, date): temp_c} from combined sources."""
    resolved: dict[tuple[str, str], float] = {}

    # From resolved markets collector
    rl = load_json(RESOLVED_LOG)
    for key_str, data in rl.get("markets", {}).items():
        if "||" in key_str:
            city, date_str = key_str.split("||", 1)
            temp = data.get("temp_c")
            if temp is not None:
                resolved[(city, date_str)] = float(temp)

    # From market prices (active markets with >95% YES)
    mp = load_json(_SCRIPT_DIR / "_market_prices.json")
    markets = mp if isinstance(mp, list) else mp.get("markets", [])
    import re
    for m in markets:
        city = m.get("city", "")
        if not city:
            continue
        question = m.get("question", "")
        # Extract date
        dm = re.search(r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?', question, re.IGNORECASE)
        if not dm:
            continue
        months = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12}
        month = months.get(dm.group(1).lower(), 1)
        day = int(dm.group(2))
        year = int(dm.group(3)) if dm.group(3) else datetime.now(timezone.utc).year
        date_str = f"{year:04d}-{month:02d}-{day:02d}"
        for o in m.get("outcomes", []):
            price = o.get("price")
            if price is None:
                continue
            if price > 0.95 and (o.get("label") or "").lower() == "yes":
                match = re.search(r'(\d+)°C', question)
                if match:
                    key = (city, date_str)
                    if key not in resolved:
                        resolved[key] = float(match.group(1))
                break

    return resolved


def main():
    print("=" * 50)
    print("  TRADING DATA CONSOLIDATOR")
    print("=" * 50)

    log = load_json(QUALITY_LOG)
    runs = log.get("runs", [])
    if not runs:
        print("No runs in quality log. Nothing to consolidate.")
        return 1

    resolved = load_resolved_markets()
    print(f"Quality log: {len(runs)} runs")
    print(f"Resolved markets: {len(resolved)}")

    # ---- Build trading_data.csv ----
    rows = []
    for run in runs:
        run_date = run.get("run_date", "")
        predictions = run.get("predictions", {})
        for city, pdata in sorted(predictions.items()):
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            p5 = strategies.get("p5", {})
            mean_s = strategies.get("mean", {})

            bma_mean = pdata.get("bma_mean")
            bma_std = pdata.get("bma_std")
            confidence = pdata.get("confidence", 0)
            models = pdata.get("models", 0)

            sigma_spill = sigma.get("spill")
            sigma_result = sigma.get("result")
            sigma_actual = sigma.get("actual_peak")
            sigma_win_prob = sigma.get("win_prob")

            p5_spill = p5.get("spill")
            p5_result = p5.get("result")
            p5_actual = p5.get("actual_peak")
            p5_win_prob = p5.get("win_prob")

            mean_spill = mean_s.get("spill")
            mean_result = mean_s.get("result")
            mean_actual = mean_s.get("actual_peak")
            mean_win_prob = mean_s.get("win_prob")

            # API peak (use sigma actual as primary, fall back to any)
            api_peak = sigma_actual or p5_actual or mean_actual

            # Polymarket resolved
            city_base = city.split(",")[0].strip()
            pm_temp = resolved.get((city, run_date)) or resolved.get((city_base, run_date))

            # Deltas
            sigma_delta = round(api_peak - sigma_spill, 1) if api_peak is not None and sigma_spill is not None else None
            pm_delta = round(api_peak - pm_temp, 1) if api_peak is not None and pm_temp is not None else None
            spill_vs_pm = round(sigma_spill - pm_temp, 1) if sigma_spill is not None and pm_temp is not None else None

            rows.append({
                "date": run_date,
                "city": city,
                "bma_mean": bma_mean,
                "bma_std": bma_std,
                "confidence": confidence,
                "models": models,
                "sigma_spill": sigma_spill,
                "sigma_result": sigma_result,
                "sigma_actual": sigma_actual,
                "sigma_win_prob": sigma_win_prob,
                "p5_spill": p5_spill,
                "p5_result": p5_result,
                "p5_actual": p5_actual,
                "p5_win_prob": p5_win_prob,
                "mean_spill": mean_spill,
                "mean_result": mean_result,
                "mean_actual": mean_actual,
                "mean_win_prob": mean_win_prob,
                "api_peak": api_peak,
                "pm_resolved": pm_temp,
                "sigma_vs_api_delta": sigma_delta,
                "api_vs_pm_delta": pm_delta,
                "spill_vs_pm_delta": spill_vs_pm,
            })

    # Write trading_data.csv
    fieldnames = [
        "date", "city", "bma_mean", "bma_std", "confidence", "models",
        "sigma_spill", "sigma_result", "sigma_actual", "sigma_win_prob",
        "p5_spill", "p5_result", "p5_actual", "p5_win_prob",
        "mean_spill", "mean_result", "mean_actual", "mean_win_prob",
        "api_peak", "pm_resolved", "sigma_vs_api_delta", "api_vs_pm_delta", "spill_vs_pm_delta",
    ]
    with open(TRADING_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)
    print(f"Trading data: {len(rows)} rows -> {TRADING_CSV}")

    # ---- Build strategy_summary.csv ----
    strat_rows = []
    strategies = ["sigma", "p5", "mean"]
    for sn in strategies:
        wins = sum(1 for r in rows if r[f"{sn}_result"] == "WIN")
        losses = sum(1 for r in rows if r[f"{sn}_result"] == "LOSS")
        total = wins + losses
        rate = round(wins / max(1, total) * 100, 1)

        # Avg delta
        deltas = [r[f"{sn}_spill"] for r in rows if r[f"{sn}_spill"] is not None and r.get("api_peak") is not None]
        avg_spill = round(sum(deltas) / max(1, len(deltas)), 1) if deltas else None
        actuals = [r["api_peak"] for r in rows if r["api_peak"] is not None]
        avg_actual = round(sum(actuals) / max(1, len(actuals)), 1) if actuals else None

        strat_rows.append({
            "strategy": sn,
            "wins": wins, "losses": losses, "total": total,
            "win_rate_pct": rate,
            "avg_spill": avg_spill, "avg_actual": avg_actual,
        })

    with open(STRATEGY_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["strategy", "wins", "losses", "total", "win_rate_pct", "avg_spill", "avg_actual"])
        w.writeheader()
        w.writerows(strat_rows)
    print(f"Strategy summary -> {STRATEGY_CSV}")
    for sr in strat_rows:
        print(f"  {sr['strategy']}: {sr['wins']}W/{sr['losses']}L = {sr['win_rate_pct']}%")

    # Quick stats
    total_pm_matches = sum(1 for r in rows if r["pm_resolved"] is not None)
    pm_deltas = [r["api_vs_pm_delta"] for r in rows if r["api_vs_pm_delta"] is not None]
    if pm_deltas:
        avg_pm_gap = round(sum(abs(d) for d in pm_deltas) / len(pm_deltas), 2)
        print(f"\nPolymarket matches: {total_pm_matches}, avg |gap|: {avg_pm_gap}C")

    return 0


if __name__ == "__main__":
    sys.exit(main())
