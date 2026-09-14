#!/usr/bin/env python3
"""
Modifisert strategy research / confidence analysis.

Answers, from the locally available data only:
  * how the Modifisert strategy performs overall and per city (with Wilson
    lower bounds, so we can separate signal from small-sample noise);
  * whether model confidence (BMA std / model count / stated confidence)
    predicts a higher hit rate;
  * how well the model's implied bucket probability is calibrated;
  * whether a simple walk-forward selection rule (city + confidence gate)
    survives out-of-sample;
  * how much market-price / ROI data actually exists locally (honesty check).

Usage:
    python _modified_research.py
    python _modified_research.py --json
"""

from __future__ import annotations

import argparse
import json
import math
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
PNL_LOG = SCRIPT_DIR / "_pnl_log.json"


def _phi(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))


def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


def load_json(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_quality_series() -> dict[str, dict[str, dict]]:
    """{date: {city: pdata}} with later runs overriding earlier ones."""
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


def central_bucket_prob(corrected_mean: float, std: float, spill: int) -> float | None:
    """P(outcome rounds to `spill`) under N(corrected_mean, std).

    A ±0.5 native-unit bucket around the spill. For °C point markets this is the
    model's implied win probability for the bucket we actually bet.
    """
    if std is None or std <= 0:
        std = 1.0
    return max(0.0, min(1.0, _phi((spill + 0.5 - corrected_mean) / std)
                              - _phi((spill - 0.5 - corrected_mean) / std)))


def bucketize(value: float, edges: list[float]) -> str:
    for i, e in enumerate(edges):
        if value < e:
            return f"<{e:g}"
    return f">={edges[-1]:g}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    mod = load_json(MODIFIED_LOG) or {}
    records = mod.get("records", []) or []
    quality = load_quality_series()
    pnl = load_json(PNL_LOG) or {}

    # Attach confidence features per row.
    rows = []
    no_conf = 0
    for rec in records:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        result = rec.get("result")
        if result not in ("WIN", "LOSS"):
            continue
        pdata = (quality.get(date_str) or {}).get(city)
        std = conf = models = None
        if isinstance(pdata, dict):
            std = pdata.get("bma_std")
            conf = pdata.get("confidence")
            models = pdata.get("models")
        if std is None:
            no_conf += 1
        corrected = rec.get("corrected_mean")
        spill = rec.get("spill")
        p_central = None
        if corrected is not None and spill is not None:
            p_central = central_bucket_prob(float(corrected), float(std) if std else 1.0, int(spill))
        rows.append({
            "city": city,
            "date": date_str,
            "result": result,
            "win": 1 if result == "WIN" else 0,
            "std": std,
            "confidence": conf,
            "models": models,
            "p_central": p_central,
            "market_unit": rec.get("market_unit"),
            "market_type": rec.get("market_type"),
            "source": rec.get("today_source"),
        })

    n = len(rows)
    wins = sum(r["win"] for r in rows)
    overall_wr = wins / n if n else 0.0

    # ── Per city ────────────────────────────────────────────────────────────
    per_city = defaultdict(lambda: {"n": 0, "wins": 0})
    for r in rows:
        per_city[r["city"]]["n"] += 1
        per_city[r["city"]]["wins"] += r["win"]
    city_rows = []
    for city, s in per_city.items():
        wr = s["wins"] / s["n"]
        city_rows.append({
            "city": city, "n": s["n"], "wins": s["wins"],
            "wr": round(wr, 4), "wilson_lb": round(wilson_lb(s["wins"], s["n"]), 4),
        })
    city_rows.sort(key=lambda x: (-x["wilson_lb"], -x["n"]))

    # ── Confidence buckets ─────────────────────────────────────────────────
    def group(rows_iter, key_fn, edges, label):
        out = {}
        for r in rows_iter:
            v = key_fn(r)
            if v is None:
                continue
            k = bucketize(float(v), edges)
            g = out.setdefault(k, {"n": 0, "wins": 0, "sum_p": 0.0, "n_p": 0})
            g["n"] += 1
            g["wins"] += r["win"]
            if r["p_central"] is not None:
                g["sum_p"] += r["p_central"]
                g["n_p"] += 1
        for k, g in out.items():
            g["wr"] = round(g["wins"] / g["n"], 4) if g["n"] else None
            g["mean_p"] = round(g["sum_p"] / g["n_p"], 4) if g["n_p"] else None
        return {label: out}

    std_buckets = group(rows, lambda r: r["std"], [0.6, 0.8, 1.0, 1.2, 1.5], "bma_std")
    conf_buckets = group(rows, lambda r: r["confidence"], [0.55, 0.65, 0.75, 0.85], "confidence")
    models_buckets = group(rows, lambda r: r["models"], [6, 7, 8], "models")
    p_buckets = group(rows, lambda r: r["p_central"], [0.2, 0.3, 0.4, 0.5, 0.6], "p_central")

    # ── Walk-forward: pick cities on train, evaluate on test ────────────────
    dates = sorted({r["date"] for r in rows})
    split = int(len(dates) * 0.7)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])
    wf = {}
    for min_n, min_wr in [(5, 0.55), (5, 0.60), (8, 0.55), (8, 0.60)]:
        train_city = defaultdict(lambda: {"n": 0, "wins": 0})
        for r in rows:
            if r["date"] in train_dates:
                train_city[r["city"]]["n"] += 1
                train_city[r["city"]]["wins"] += r["win"]
        selected = {
            c for c, s in train_city.items()
            if s["n"] >= min_n and s["wins"] / s["n"] >= min_wr
        }
        tn = tw = 0
        for r in rows:
            if r["date"] in test_dates and r["city"] in selected:
                tn += 1
                tw += r["win"]
        wf[f"city_n{min_n}_wr{int(min_wr*100)}"] = {
            "selected_cities": len(selected),
            "test_n": tn,
            "test_wins": tw,
            "test_wr": round(tw / tn, 4) if tn else None,
        }

    # ── Walk-forward: pooled p_central gate ─────────────────────────────────
    p_gate = {}
    for t in [0.25, 0.30, 0.35, 0.40, 0.45]:
        tn = tw = 0
        for r in rows:
            if r["date"] in test_dates and r["p_central"] is not None and r["p_central"] >= t:
                tn += 1
                tw += r["win"]
        p_gate[f"p>={t:.2f}"] = {"test_n": tn, "test_wins": tw,
                                 "test_wr": round(tw / tn, 4) if tn else None}

    # ── Walk-forward: combined city whitelist + confidence gates ────────────
    train_city60 = {
        c for c, s in (
            (c, {"n": sum(1 for r in rows if r["city"] == c and r["date"] in train_dates),
                 "wins": sum(r["win"] for r in rows if r["city"] == c and r["date"] in train_dates)})
            for c in {r["city"] for r in rows}
        )
        if s["n"] >= 5 and s["wins"] / s["n"] >= 0.60
    }
    combined = {}
    for t in [0.35, 0.40, 0.45]:
        for max_std in [0.9, 1.0, 1.1]:
            tn = tw = 0
            for r in rows:
                if r["date"] not in test_dates or r["city"] not in train_city60:
                    continue
                if r["p_central"] is None or r["p_central"] < t:
                    continue
                if r["std"] is None or r["std"] > max_std:
                    continue
                tn += 1
                tw += r["win"]
            combined[f"city60+p>={t:.2f}+std<={max_std:.1f}"] = {
                "test_n": tn, "test_wins": tw,
                "test_wr": round(tw / tn, 4) if tn else None,
                "wilson_lb": round(wilson_lb(tw, tn), 4) if tn else None,
            }

    # ── Market price / ROI data availability (honesty check) ────────────────
    pr = pnl.get("records", []) or []
    price_present = sum(1 for x in pr if x.get("market_price") is not None)
    edge_present = sum(1 for x in pr if x.get("edge") is not None)
    eligible = sum(1 for x in pr if x.get("eligible"))
    price_cov = {
        "pnl_records": len(pr),
        "with_market_price": price_present,
        "with_edge": edge_present,
        "eligible_bets": eligible,
    }

    summary = {
        "generated": datetime.now(timezone.utc).isoformat(),
        "rows": n,
        "wins": wins,
        "overall_win_rate": round(overall_wr, 4),
        "rows_without_confidence_features": no_conf,
        "train_dates": [dates[0], dates[split - 1]] if dates else None,
        "test_dates": [dates[split], dates[-1]] if dates else None,
        "top_cities_by_wilson_lb": city_rows[:20],
        "bottom_cities_by_wilson_lb": city_rows[-10:],
        "std_buckets": std_buckets["bma_std"],
        "confidence_buckets": conf_buckets["confidence"],
        "models_buckets": models_buckets["models"],
        "p_central_buckets": p_buckets["p_central"],
        "walk_forward_city_rules": wf,
        "walk_forward_p_central_gate": p_gate,
        "walk_forward_combined": combined,
        "market_price_coverage": price_cov,
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    print("=" * 68)
    print(f"MODIFISERT RESEARCH — {n} resolved rows, overall WR {overall_wr*100:.1f}%")
    print(f"train {summary['train_dates']}  test {summary['test_dates']}")
    print(f"rows missing BMA std: {no_conf}")
    print("=" * 68)

    print("\nTOP 20 CITIES by Wilson LB (n, W, WR, LB):")
    for c in city_rows[:20]:
        print(f"  {c['city']:<26s} n={c['n']:>3d} W={c['wins']:>3d} WR={c['wr']*100:5.1f}% LB={c['wilson_lb']*100:5.1f}%")

    print("\nWORST 10 CITIES:")
    for c in city_rows[-10:]:
        print(f"  {c['city']:<26s} n={c['n']:>3d} W={c['wins']:>3d} WR={c['wr']*100:5.1f}% LB={c['wilson_lb']*100:5.1f}%")

    def show(title, d):
        print(f"\n{title}:")
        for k in sorted(d):
            g = d[k]
            print(f"  {k:<8s} n={g['n']:>4d} WR={g['wr']*100:5.1f}%"
                  f"{('  mean_p=' + format(g['mean_p'], '.3f')) if g.get('mean_p') is not None else ''}")

    show("HIT RATE BY BMA STD (°C)", std_buckets["bma_std"])
    show("HIT RATE BY MODEL CONFIDENCE", conf_buckets["confidence"])
    show("HIT RATE BY MODEL COUNT", models_buckets["models"])
    show("HIT RATE BY P(central bucket)", p_buckets["p_central"])

    print("\nWALK-FORWARD CITY SELECTION (train 70% -> test 30%):")
    for k, v in wf.items():
        print(f"  {k:<18s} cities={v['selected_cities']:>2d} test_n={v['test_n']:>4d} "
              f"test_WR={(v['test_wr']*100 if v['test_wr'] is not None else float('nan')):5.1f}%")

    print("\nWALK-FORWARD P(central) GATE on test:")
    for k, v in p_gate.items():
        print(f"  {k:<8s} test_n={v['test_n']:>4d} "
              f"test_WR={(v['test_wr']*100 if v['test_wr'] is not None else float('nan')):5.1f}%")

    print("\nWALK-FORWARD COMBINED (city train WR>=60% + gates) on test:")
    for k, v in combined.items():
        wr = v['test_wr'] * 100 if v['test_wr'] is not None else float('nan')
        lb = v['wilson_lb'] * 100 if v['wilson_lb'] is not None else float('nan')
        print(f"  {k:<34s} n={v['test_n']:>3d} WR={wr:5.1f}% LB={lb:5.1f}%")

    print(f"\nMARKET PRICE / ROI DATA: {price_cov}")
    print("  -> historical market prices are NOT stored, so historical ROI cannot be")
    print("     reconstructed; only hit-rate + calibration can be validated locally.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
