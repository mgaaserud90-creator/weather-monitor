#!/usr/bin/env python3
"""
Per-city blend-weight experiment.

The global blend (0.25·provider + 0.75·BMA) lifted the full-series hit rate
from 41.9% to 45.9%. This script asks whether a **per-city** blend weight,
chosen on a train split and evaluated on a held-out test split, generalises
better than the single global weight.

Usage: python _modified_percity_alpha.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
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
ALPHAS = [0.0, 0.25, 0.5, 0.75, 1.0]


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

    rows = []
    for rec in records:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        prov = rec.get("providers_weighted_mean")
        bma = (quality.get(date_str, {}) or {}).get(city, {}).get("bma_mean")
        c = cfg.get(city)
        if prov is None or bma is None or c is None:
            continue
        base = city.split(",")[0].strip()
        mi = markets.get((city, date_str)) or markets.get((base, date_str))
        if mi is None:
            continue
        rows.append({"city": city, "date": date_str, "prov": float(prov), "bma": float(bma),
                     "cfg": c, "mi": mi, "cur": 1 if rec.get("result") == "WIN" else 0})

    def hit(city_cfg, alpha, subset):
        w = n = 0
        for r in subset:
            x = alpha * r["prov"] + (1 - alpha) * r["bma"]
            spill = int(round(ms.apply_correction(city_cfg["correction_method"],
                                                  city_cfg["correction_params"], x)))
            res = ms.resolve_spill(spill, r["mi"])
            if res in ("WIN", "LOSS"):
                n += 1
                w += 1 if res == "WIN" else 0
        return w, n

    dates = sorted({r["date"] for r in rows})
    split = int(len(dates) * 0.7)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])
    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    per_city_alpha = {}
    tw = tn = 0
    cw = cn = 0
    gw = gn = 0
    for city, cr in by_city.items():
        train = [r for r in cr if r["date"] in train_dates]
        test = [r for r in cr if r["date"] in test_dates]
        if not train or not test:
            continue
        best_a, best_w, best_n = 0.25, -1, 0
        for a in ALPHAS:
            w, n = hit(cr[0]["cfg"], a, train)
            if n and (best_n == 0 or w / n > best_w / best_n):
                best_a, best_w, best_n = a, w, n
        per_city_alpha[city] = best_a
        w, n = hit(cr[0]["cfg"], best_a, test)
        tw += w; tn += n
        w2, n2 = hit(cr[0]["cfg"], 0.25, test)
        gw += w2; gn += n2
        cw += sum(r["cur"] for r in test); cn += len(test)

    print("=" * 68)
    print(f"PER-CITY BLEND ALPHA — {len(rows)} rows, test {dates[split]}..{dates[-1]}")
    print(f"  current strategy            test WR = {cw/cn*100:5.1f}%  (n={cn})")
    print(f"  global alpha=0.25           test WR = {gw/gn*100:5.1f}%  (n={gn})")
    print(f"  per-city alpha (train-picked) test WR = {tw/tn*100:5.1f}%  (n={tn})")
    print("  chosen alphas:", json.dumps(per_city_alpha, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
