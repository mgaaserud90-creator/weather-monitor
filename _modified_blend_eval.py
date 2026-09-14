#!/usr/bin/env python3
"""
Full-series evaluator: does blending the per-provider weighted mean with the
BMA mean beat the current Modifisert base on ALL 35 days?

Candidate bases (all followed by the city's stored correction, rounded, and
resolved with the project's exact Polymarket resolver):

  current  — weighted mean on per-provider days, BMA mean otherwise (as shipped)
  bma      — BMA mean every day
  blend:a  — a·weighted + (1−a)·BMA on per-provider days, BMA otherwise

Usage: python _modified_blend_eval.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

MODIFIED_LOG = SCRIPT_DIR / "_modified_strategy_log.json"
QUALITY_LOG = SCRIPT_DIR / "_model_quality_log.json"


def load_json(p: Path):
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_quality_series():
    data = load_json(QUALITY_LOG) or {}
    series = {}
    for run in data.get("runs", []) or []:
        run_date = str(run.get("run_date", ""))
        run_target = str(run.get("target_date", "")) or run_date
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if isinstance(pdata, dict):
                d = str(pdata.get("_target_date") or run_target or run_date)
                if d:
                    series.setdefault(d, {})[city] = pdata
    return series


def main() -> int:
    import _modified_strategy as ms  # type: ignore

    mod = load_json(MODIFIED_LOG) or {}
    records = mod.get("records", []) or []
    quality = load_quality_series()
    markets = ms.load_market_details()

    provider_stats = ms.load_provider_analysis()
    curvefit = ms.load_curvefit()
    decisions = ms.removal_decisions(provider_stats)
    cfg = ms.build_cities_config(provider_stats, curvefit, decisions)

    def resolve(spill, mi):
        return ms.resolve_spill(spill, mi)

    rules = {"current": [], "bma": []}
    for a in (0.0, 0.25, 0.4, 0.5, 0.6, 0.75):
        rules[f"blend:{a}"] = []

    prov_days = 0
    for rec in records:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        c = cfg.get(city)
        if c is None:
            continue
        base = city.split(",")[0].strip()
        mi = markets.get((city, date_str)) or markets.get((base, date_str))
        if mi is None:
            continue
        bma = (quality.get(date_str, {}) or {}).get(city, {}).get("bma_mean")
        # raw provider weighted mean (records now store the blended base in
        # ``weighted_mean``, so prefer the explicit raw field when present)
        x_now = rec.get("providers_weighted_mean", rec.get("weighted_mean"))
        is_prov = rec.get("base_source") == "blend_bma_weighted" or "providers_weighted_mean" in rec
        if is_prov:
            prov_days += 1

        def spill_of(base_val):
            if base_val is None:
                return None
            corrected = ms.apply_correction(c["correction_method"], c["correction_params"], float(base_val))
            return int(round(corrected))

        ordered = {
            "current": x_now,
            "bma": bma if bma is not None else (x_now if not is_prov else x_now),
        }
        for a in (0.0, 0.25, 0.4, 0.5, 0.6, 0.75):
            if is_prov and bma is not None and x_now is not None:
                ordered[f"blend:{a}"] = a * float(x_now) + (1 - a) * float(bma)
            else:
                ordered[f"blend:{a}"] = bma if bma is not None else x_now

        for name, x in ordered.items():
            sp = spill_of(x)
            if sp is None:
                continue
            res = resolve(sp, mi)
            if res in ("WIN", "LOSS"):
                rules[name].append(1 if res == "WIN" else 0)

    print("=" * 72)
    print(f"FULL-SERIES BASE COMPARISON — {len(records)} records, {prov_days} per-provider days")
    print("=" * 72)
    for name, outcomes in rules.items():
        n = len(outcomes)
        w = sum(outcomes)
        wr = (w / n * 100) if n else float("nan")
        print(f"  {name:<10s} n={n:>4d}  WIN={w:>4d}  WR={wr:5.1f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
