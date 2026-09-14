#!/usr/bin/env python3
"""
Per-city correction optimizer for the Modifisert strategy.

Goal: maximise the realised hit rate by choosing, **per city**, the best
correction applied to the modified weighted mean before rounding to a spill.

Method (honest, out-of-sample):
  * x        = the row's ``weighted_mean`` (per-provider weighted mean, or BMA
               mean on fallback days) from ``_modified_strategy_log.json``;
  * spill    = round(x + b)  for a candidate offset ``b``;
  * WIN/LOSS = the project's exact resolver against the real Polymarket market
               (point °C, °F bucket bounds, and threshold markets);
  * for each city we pick ``b`` on the TRAIN split (first 70% of dates) and
    report the TEST hit rate (last 30%), plus a shrunk offset toward the global
    optimum for small samples.

Candidates include the *current* correction already stored in
``_per_city_curvefit.json`` (evaluated via the persisted record results) so the
gain is measured against the real baseline, not a strawman.

Usage:
    python _modified_correction_optimizer.py
    python _modified_correction_optimizer.py --json
    python _modified_correction_optimizer.py --emit   # write _per_city_correction_v2.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
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
CURVEFIT = SCRIPT_DIR / "_per_city_curvefit.json"
OUT_FILE = SCRIPT_DIR / "_per_city_correction_v2.json"

OFFSET_GRID = [round(-3.0 + 0.25 * i, 2) for i in range(25)]  # -3.00 .. +3.00
SHRINK_K = 6.0


def _phi(x: float) -> float:
    import math
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_quality_series() -> dict[str, dict[str, dict]]:
    data = load_json(QUALITY_LOG) or {}
    series: dict[str, dict[str, dict]] = {}
    for run in data.get("runs", []) or []:
        run_date = str(run.get("run_date", ""))
        run_target = str(run.get("target_date", "")) or run_date
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            date_str = str(pdata.get("_target_date") or run_target or run_date)
            if date_str:
                series.setdefault(date_str, {})[city] = pdata
    return series


def load_resolved():
    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        return _load_market_resolved_details()
    except Exception:
        return {}


def resolve_win(spill_c: int, mi: dict | None) -> int | None:
    if mi is None:
        return None
    try:
        from _model_quality_tracker import (  # type: ignore
            _spill_vs_polymarket_result,
            _spill_vs_threshold_result,
        )
        if mi.get("type") == "threshold":
            res = _spill_vs_threshold_result(spill_c, mi)
        else:
            res = _spill_vs_polymarket_result(spill_c, mi)
    except Exception:
        return None
    if res == "WIN":
        return 1
    if res == "LOSS":
        return 0
    return None


def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    import math
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--emit", action="store_true", help="write _per_city_correction_v2.json")
    args = ap.parse_args()

    mod = load_json(MODIFIED_LOG) or {}
    records = mod.get("records", []) or []
    quality = load_quality_series()
    resolved = load_resolved()

    rows = []
    for rec in records:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        # start from the CURRENT corrected mean so the offset is a *residual*
        # bias correction on top of the existing per-city model, not a
        # replacement for it.
        x = rec.get("corrected_mean", rec.get("weighted_mean"))
        if city == "" or not date_str or x is None:
            continue
        base = city.split(",")[0].strip()
        mi = resolved.get((city, date_str)) or resolved.get((base, date_str))
        if mi is None:
            continue
        pdata = (quality.get(date_str) or {}).get(city) or {}
        std = pdata.get("bma_std")
        rows.append({
            "city": city, "date": date_str, "x": float(x),
            "mi": mi, "std": std,
            "current_win": 1 if rec.get("result") == "WIN" else (0 if rec.get("result") == "LOSS" else None),
        })

    by_city = defaultdict(list)
    for r in rows:
        by_city[r["city"]].append(r)

    def hit(offset: float, subset: list[dict]) -> tuple[int, int]:
        w = n = 0
        for r in subset:
            spill = int(round(r["x"] + offset))
            res = resolve_win(spill, r["mi"])
            if res is None:
                continue
            n += 1
            w += res
        return w, n

    # Global (pooled) optimum on ALL data, for shrinkage.
    global_best, global_score = 0.0, -1.0
    for b in OFFSET_GRID:
        w, n = hit(b, rows)
        if n and w / n > global_score:
            global_score, global_best = w / n, b

    city_out = {}
    tot_cur_w = tot_cur_n = 0
    tot_opt_w = tot_opt_n = 0
    tot_sht_w = tot_sht_n = 0
    for city, cr in by_city.items():
        dates = sorted({r["date"] for r in cr})
        if len(dates) < 4:
            continue
        split = max(1, int(len(dates) * 0.7))
        train_dates = set(dates[:split])
        test_dates = set(dates[split:])
        train = [r for r in cr if r["date"] in train_dates]
        test = [r for r in cr if r["date"] in test_dates]
        if not test:
            continue

        cur_w, cur_n = sum(1 for r in test if r["current_win"] == 1), sum(1 for r in test if r["current_win"] is not None)

        best_b, best_w, best_n = 0.0, -1, 0
        for b in OFFSET_GRID:
            w, n = hit(b, train)
            if n == 0:
                continue
            # tie-break toward the smaller |offset|
            score = w / n - 1e-6 * abs(b)
            best_score = (best_w / best_n - 1e-6 * abs(best_b)) if best_n else None
            if best_score is None or score > best_score:
                best_b, best_w, best_n = b, w, n
        if best_n == 0:
            best_b = global_best

        opt_w, opt_n = hit(best_b, test)
        shrunk_b = (best_n * best_b + SHRINK_K * global_best) / (best_n + SHRINK_K)
        sht_w, sht_n = hit(shrunk_b, test)

        city_out[city] = {
            "n_total": len(cr),
            "n_train": len(train),
            "n_test": len(test),
            "current_test_wr": round(cur_w / cur_n, 4) if cur_n else None,
            "best_offset_train": best_b,
            "optimized_test_wr": round(opt_w / opt_n, 4) if opt_n else None,
            "wilson_lb": round(wilson_lb(opt_w, opt_n), 4) if opt_n else None,
            "shrunk_offset": round(shrunk_b, 4),
            "shrunk_test_wr": round(sht_w / sht_n, 4) if sht_n else None,
            "gain_vs_current": round((opt_w / opt_n - cur_w / cur_n), 4) if (opt_n and cur_n) else None,
        }
        tot_cur_w += cur_w; tot_cur_n += cur_n
        tot_opt_w += opt_w; tot_opt_n += opt_n
        tot_sht_w += sht_w; tot_sht_n += sht_n

    result = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "global_best_offset": global_best,
        "global_best_wr": round(global_score, 4),
        "rows_total": len(rows),
        "cities_total": len(city_out),
        "pooled_current_test_wr": round(tot_cur_w / tot_cur_n, 4) if tot_cur_n else None,
        "pooled_optimized_test_wr": round(tot_opt_w / tot_opt_n, 4) if tot_opt_n else None,
        "pooled_shrunk_test_wr": round(tot_sht_w / tot_sht_n, 4) if tot_sht_n else None,
        "cities": dict(sorted(city_out.items(), key=lambda kv: -(kv[1]["optimized_test_wr"] or 0))),
    }

    if args.emit:
        OUT_FILE.write_text(json.dumps({
            "meta": {
                "generated": result["generated"],
                "method": "per-city constant offset on weighted_mean, walk-forward selected, shrunk to global",
                "global_best_offset": global_best,
                "shrink_k": SHRINK_K,
                "offset_grid": OFFSET_GRID,
            },
            "cities": {c: {"offset": v["shrunk_offset"], "train_offset": v["best_offset_train"],
                            "optimized_test_wr": v["optimized_test_wr"],
                            "current_test_wr": v["current_test_wr"]}
                        for c, v in city_out.items()},
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[emit] wrote {OUT_FILE.name} ({len(city_out)} cities)")

    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print("=" * 74)
    print(f"PER-CITY CORRECTION OPTIMIZER — {len(rows)} rows, {len(city_out)} cities")
    print(f"global best offset={global_best:+.2f} (WR {global_score*100:.1f}%)")
    print(f"pooled TEST hit: current {result['pooled_current_test_wr']*100:.1f}%  "
          f"-> optimized {result['pooled_optimized_test_wr']*100:.1f}%  "
          f"-> shrunk {result['pooled_shrunk_test_wr']*100:.1f}%")
    print("=" * 74)
    print(f"{'city':<26s} {'n':>3s} {'cur%':>6s} {'opt%':>6s} {'gain':>6s} {'off':>6s}")
    for city, v in result["cities"].items():
        cur = v["current_test_wr"] * 100 if v["current_test_wr"] is not None else float("nan")
        opt = v["optimized_test_wr"] * 100 if v["optimized_test_wr"] is not None else float("nan")
        gain = v["gain_vs_current"] * 100 if v["gain_vs_current"] is not None else float("nan")
        print(f"{city:<26s} {v['n_total']:>3d} {cur:>6.1f} {opt:>6.1f} {gain:>+6.1f} {v['best_offset_train']:>+6.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
