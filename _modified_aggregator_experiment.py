#!/usr/bin/env python3
"""
Aggregator experiment for the Modifisert strategy.

The per-city *offset* optimizer showed that tuning a single correction number
per city does not generalise out-of-sample. This script asks a different,
deeper question: is the **provider aggregation itself** (the weighted mean)
the best estimator, or do robust / bias-corrected aggregators generalise
better?

It evaluates, on the per-provider subset (2026-08-11 → 2026-09-04, the only
days with individual provider values), with a walk-forward 70/30 date split:

  A  weighted mean over remaining providers           (current Modifisert)
  B  equal mean over remaining providers
  C  median over remaining providers
  D  trimmed mean (drop min & max) over remaining
  E  weighted mean of provider-bias-corrected values  (bias learned on TRAIN only)
  F  0.5·A + 0.5·BMA mean (blend with the ensemble mean)

Every predictor is followed by the city's stored correction model, rounded to
a spill and resolved against the real Polymarket market with the project's
resolver — so the comparison is apples-to-apples.

Usage: python _modified_aggregator_experiment.py
"""

from __future__ import annotations

import json
import os
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

WEIGHTED_PREDICTIONS = SCRIPT_DIR / "_weighted_mean_predictions.json"
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


def resolve_win(spill_c: int, mi: dict | None):
    if mi is None:
        return None
    from _model_quality_tracker import (  # type: ignore
        _spill_vs_polymarket_result,
        _spill_vs_threshold_result,
    )
    res = (_spill_vs_threshold_result(spill_c, mi) if mi.get("type") == "threshold"
           else _spill_vs_polymarket_result(spill_c, mi))
    return 1 if res == "WIN" else (0 if res == "LOSS" else None)


def resolve_value_c(mi: dict | None):
    if not mi or mi.get("type") == "threshold":
        return None
    v = mi.get("value")
    if v is None:
        return None
    v = float(v)
    if (mi.get("unit") or "C").upper() == "F":
        return (v - 32.0) * 5.0 / 9.0
    return v


def main() -> int:
    import _modified_strategy as ms  # type: ignore

    payload = load_json(WEIGHTED_PREDICTIONS) or {}
    predictions = payload.get("predictions", []) or []
    quality = load_quality_series()
    markets = ms.load_market_details()

    provider_stats = ms.load_provider_analysis()
    curvefit = ms.load_curvefit()
    decisions = ms.removal_decisions(provider_stats)
    cities_cfg = ms.build_cities_config(provider_stats, curvefit, decisions)

    rows = []
    for rec in predictions:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        providers = rec.get("providers", {}) or {}
        cfg = cities_cfg.get(city)
        if cfg is None or not providers:
            continue
        base = city.split(",")[0].strip()
        mi = markets.get((city, date_str)) or markets.get((base, date_str))
        rows.append({"city": city, "date": date_str, "providers": providers, "cfg": cfg,
                     "mi": mi, "resolved_c": resolve_value_c(mi),
                     "bma": (quality.get(date_str, {}) or {}).get(city, {}).get("bma_mean")})

    # Provider bias per (city, provider) learned ONLY on train dates.
    dates = sorted({r["date"] for r in rows})
    split = int(len(dates) * 0.7)
    train_dates = set(dates[:split])
    test_dates = set(dates[split:])

    bias = defaultdict(list)
    for r in rows:
        if r["date"] not in train_dates or r["resolved_c"] is None:
            continue
        for k, v in r["providers"].items():
            try:
                bias[(r["city"], k)].append(float(v) - float(r["resolved_c"]))
            except (TypeError, ValueError):
                continue
    provider_bias = {k: sum(v) / len(v) for k, v in bias.items() if v}

    def weighted(vals, weights):
        s = sum(weights[k] for k in vals)
        return sum(weights[k] * vals[k] for k in vals) / s if s > 0 else None

    def predict(kind: str, r: dict):
        cfg = r["cfg"]
        w = cfg["remaining_weights"]
        prov = {k: float(v) for k, v in r["providers"].items()
                if k in w and v is not None}
        if not prov:
            return None
        if kind in ("A", "E"):
            vals = prov
            if kind == "E":
                vals = {k: prov[k] - provider_bias.get((r["city"], k), 0.0) for k in prov}
            return weighted(vals, {k: w[k] for k in vals})
        if kind == "B":
            return sum(prov.values()) / len(prov)
        if kind == "C":
            s = sorted(prov.values())
            n = len(s)
            return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])
        if kind == "D":
            s = sorted(prov.values())
            s = s[1:-1] if len(s) > 2 else s
            return sum(s) / len(s)
        if kind.startswith("BLEND:"):
            alpha = float(kind.split(":", 1)[1])
            a = weighted(prov, {k: w[k] for k in prov})
            b = r["bma"]
            if a is None:
                return None
            if b is None:
                return a
            return alpha * a + (1.0 - alpha) * float(b)
        return None

    def apply_corr(cfg, mean_c):
        return ms.apply_correction(cfg["correction_method"], cfg["correction_params"], mean_c)

    labels = {
        "A": "weighted mean (current)",
        "B": "equal mean",
        "C": "median",
        "D": "trimmed mean",
        "E": "provider-bias-corrected weighted",
    }
    blend_kinds = [f"BLEND:{a}" for a in (0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0)]
    for a in (0.0, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0):
        labels[f"BLEND:{a}"] = f"blend alpha={a:g}·weighted + {1-a:g}·BMA"
    out = {}
    for kind in ["A", "B", "C", "D", "E", *blend_kinds]:
        w = n = 0
        for r in rows:
            if r["date"] not in test_dates:
                continue
            x = predict(kind, r)
            if x is None:
                continue
            spill = int(round(apply_corr(r["cfg"], x)))
            res = resolve_win(spill, r["mi"])
            if res is None:
                continue
            n += 1
            w += res
        out[kind] = {"label": labels[kind], "test_n": n, "test_wins": w,
                     "test_wr": round(w / n, 4) if n else None}

    print("=" * 70)
    print(f"AGGREGATOR EXPERIMENT — {len(rows)} per-provider rows, "
          f"train {dates[0]}..{dates[split-1]}, test {dates[split]}..{dates[-1]}")
    print("=" * 70)
    for kind, v in out.items():
        wr = v["test_wr"] * 100 if v["test_wr"] is not None else float("nan")
        print(f"  {kind}  {v['label']:<36s} n={v['test_n']:>4d} WR={wr:5.1f}%")
    print("\nJSON:", json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
