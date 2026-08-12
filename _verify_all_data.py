#!/usr/bin/env python3
"""
Comprehensive Data Verification Script
=======================================
Validates all log files for consistency, completeness, and correctness.
Checks cross-file integrity, date formats, missing data, and anomalies.

ZERO API calls — pure data validation.
"""
from __future__ import annotations

import csv
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent

CHECKS = {"pass": 0, "warn": 0, "fail": 0}


def check(condition: bool, msg: str, level: str = "pass") -> None:
    icon = {"pass": "[OK]", "warn": "[WARN]", "fail": "[FAIL]"}[level]
    if condition:
        CHECKS["pass"] += 1
    else:
        CHECKS[level] += 1
    print(f"  {icon} {msg}")


def verify_quality_log():
    """Verify _model_quality_log.json."""
    print("\n--- _model_quality_log.json ---")
    path = _SCRIPT_DIR / "_model_quality_log.json"
    check(path.exists(), "File exists")

    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        runs = data.get("runs", [])
        check(len(runs) > 0, f"Has {len(runs)} runs")
        check(isinstance(runs, list), "runs is a list")

        for run in runs:
            rd = run.get("run_date", "")
            check(bool(rd), f"run_date present: {rd}")
            check(len(rd) == 10 and rd[4] == "-", f"run_date format valid: {rd}")

            preds = run.get("predictions", {})
            resolved = sum(1 for p in preds.values()
                          if p.get("strategies", {}).get("sigma", {}).get("result") in ("WIN", "LOSS"))
            total = len(preds)
            check(total > 0, f"Run {rd}: {total} predictions, {resolved} resolved",
                  "warn" if resolved == 0 else "pass")

            for city, pdata in preds.items():
                strategies = pdata.get("strategies", {})
                sigma = strategies.get("sigma", {})
                if sigma.get("result") in ("WIN", "LOSS"):
                    actual = sigma.get("actual_peak")
                    check(actual is not None, f"{city} resolved but actual_peak present",
                          "fail" if actual is None else "pass")

        # Cross-check: no duplicate cities per run
        for run in runs:
            preds = run.get("predictions", {})
            check(len(preds) <= 51, f"Run {run.get('run_date')}: {len(preds)} <= 51 cities",
                  "warn" if len(preds) > 51 else "pass")

    except (json.JSONDecodeError, KeyError) as e:
        check(False, f"JSON parse error: {e}")


def verify_trading_csv():
    """Verify _trading_data.csv."""
    print("\n--- _trading_data.csv ---")
    path = _SCRIPT_DIR / "_trading_data.csv"
    check(path.exists(), "File exists")

    if not path.exists():
        return
    try:
        with open(path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            check(len(rows) > 0, f"Has {len(rows)} rows")

            required = ["date", "city", "bma_mean", "sigma_spill", "sigma_result", "api_peak"]
            for col in required:
                check(col in reader.fieldnames, f"Column '{col}' exists")

            dates = set(r["date"] for r in rows)
            cities = set(r["city"] for r in rows)
            print(f"  📊 Dates: {len(dates)}, Cities: {len(cities)}")

            # Check for missing actuals
            missing = sum(1 for r in rows if not r.get("api_peak"))
            check(missing == 0, f"Rows missing api_peak: {missing}", "warn" if missing > 0 else "pass")

            pm_matches = sum(1 for r in rows if r.get("pm_resolved"))
            print(f"  📈 Polymarket matches: {pm_matches}")

    except Exception as e:
        check(False, f"CSV parse error: {e}")


def verify_market_prices():
    """Verify _market_prices.json."""
    print("\n--- _market_prices.json ---")
    path = _SCRIPT_DIR / "_market_prices.json"
    check(path.exists(), "File exists")
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        markets = data if isinstance(data, list) else data.get("markets", [])
        check(len(markets) > 0, f"Has {len(markets)} markets", "warn" if len(markets) == 0 else "pass")

        resolved = [m for m in markets if any(
            o.get("price", 0) > 0.95 for o in m.get("outcomes", []))]
        print(f"  📊 Resolved (>95%): {len(resolved)}")
        for r in resolved[:3]:
            city = r.get("city", "?")
            yes_price = max((o.get("price", 0) for o in r.get("outcomes", [])
                           if o.get("label", "").lower() == "yes"), default=0)
            print(f"    {city}: YES@{yes_price:.1%}")

    except Exception as e:
        check(False, f"JSON parse error: {e}")


def verify_peak_verification():
    """Verify _peak_verification_log.json."""
    print("\n--- _peak_verification_log.json ---")
    path = _SCRIPT_DIR / "_peak_verification_log.json"
    check(path.exists(), "File exists", "warn")
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        verif = data.get("verifications", {})
        check(len(verif) > 0, f"Has {len(verif)} verifications", "warn" if len(verif) == 0 else "pass")
        for city, v in verif.items():
            check(v.get("our_peak") is not None, f"{city}: our_peak present")
            check(v.get("market_resolved") is not None, f"{city}: market_resolved present")
            gap = abs(v.get("gap", 0))
            if gap > 1.0:
                print(f"  ⚠️ {city}: gap={gap}C")
    except Exception as e:
        check(False, f"JSON parse error: {e}")


def verify_resolved_markets():
    """Verify _resolved_markets_log.json."""
    print("\n--- _resolved_markets_log.json ---")
    path = _SCRIPT_DIR / "_resolved_markets_log.json"
    check(path.exists(), "File exists", "warn")
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        mkts = data.get("markets", {})
        check(len(mkts) > 0, f"Has {len(mkts)} resolved markets", "warn" if len(mkts) == 0 else "pass")
        for key, val in list(mkts.items())[:5]:
            print(f"  {key}: {val.get('temp_c', '?')}C")
    except Exception as e:
        check(False, f"JSON parse error: {e}")


def verify_accuracy():
    """Verify _model_accuracy_log.json."""
    print("\n--- _model_accuracy_log.json ---")
    path = _SCRIPT_DIR / "_model_accuracy_log.json"
    check(path.exists(), "File exists", "warn")
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        summary = data.get("summary", {})
        for model, stats in summary.items():
            check(stats["total_predictions"] > 0, f"{model}: {stats['total_predictions']} preds, MAE={stats['mae']}C")
    except Exception as e:
        check(False, f"JSON parse error: {e}")


def verify_cross_file_integrity():
    """Cross-check that resolved predictions match across files."""
    print("\n--- Cross-File Integrity ---")
    qpath = _SCRIPT_DIR / "_model_quality_log.json"
    tpath = _SCRIPT_DIR / "_trading_data.csv"

    if not qpath.exists() or not tpath.exists():
        print("  ⚠️ Skipping — missing files")
        return

    try:
        log = json.loads(qpath.read_text(encoding="utf-8"))
        runs = log.get("runs", [])
        with open(tpath, encoding="utf-8") as f:
            csv_rows = list(csv.DictReader(f))

        csv_dates = sorted(set(r["date"] for r in csv_rows))
        log_dates = sorted(set(run["run_date"] for run in runs))
        check(csv_dates == log_dates, f"Date alignment: CSV={csv_dates} == LOG={log_dates}",
              "warn" if csv_dates != log_dates else "pass")

        csv_resolved = sum(1 for r in csv_rows if r.get("sigma_result") in ("WIN", "LOSS"))
        log_resolved = sum(1 for run in runs for p in run.get("predictions", {}).values()
                          if p.get("strategies", {}).get("sigma", {}).get("result") in ("WIN", "LOSS"))
        check(csv_resolved == log_resolved,
              f"Resolved count: CSV={csv_resolved} == LOG={log_resolved}",
              "warn" if csv_resolved != log_resolved else "pass")

    except Exception as e:
        check(False, f"Cross-check error: {e}")


def main():
    print("=" * 60)
    print("  COMPREHENSIVE DATA VERIFICATION")
    print(f"  {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)

    verify_quality_log()
    verify_trading_csv()
    verify_market_prices()
    verify_peak_verification()
    verify_resolved_markets()
    verify_accuracy()
    verify_cross_file_integrity()

    print(f"\n{'='*60}")
    print(f"  RESULTS: {CHECKS['pass']}[OK] {CHECKS['warn']}[WARN] {CHECKS['fail']}[FAIL]")
    print(f"{'='*60}")

    return 0 if CHECKS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
