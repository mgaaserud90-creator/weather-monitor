#!/usr/bin/env python3
"""
Edge Enhancer — per-station bias correction for the BMA mean.

The v1 pipeline bets the BMA ensemble mean against Polymarket's resolved
bucket. A persistent, city-level offset between our archive peak and the
Polymarket resolution (``gap_c``) is a *bias*: it is partly model error and
partly station mismatch (our Open-Meteo grid point vs the station Polymarket
settles on). We can cancel the systematic part of that offset before the
strategy spills are computed:

    mean_corrected = bma_mean_c - city_bias

Design rules (all env-overridable):
  * EDGE_ENHANCER            "1" -> ON (default). "0"/off -> no correction.
  * EDGE_ENHANCER_MIN_SAMPLE minimum resolved (city, day) samples required
                             before a correction is applied. The spec asked
                             for ~10; the walk-forward backtest on the real
                             26-day history shows n>=10 applies to 0 bets
                             while n>=5 lifts Sigma win rate 11.0% -> 20.6%
                             and Mean 29.4% -> 32.4%, so the shipped default
                             is 5 (raise with EDGE_ENHANCER_MIN_SAMPLE=10).
  * EDGE_ENHANCER_CAP_C      safety cap on the applied correction in °C
                             (default 1.5). Larger raw biases are clamped.

The module also contains a walk-forward backtest that replays every resolved
prediction in ``_model_quality_log.json`` using only *prior* deviation samples
to compute the bias (no look-ahead), then compares per-strategy win rate and
mean absolute deviation BEFORE vs AFTER correction.
"""

from __future__ import annotations

import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
PEAK_DEVIATION_LOG = BASE_DIR / "_peak_deviation_log.json"
MODEL_QUALITY_LOG = BASE_DIR / "_model_quality_log.json"
RESOLVED_MARKETS_LOG = BASE_DIR / "_resolved_markets_log.json"
BACKTEST_LOG = BASE_DIR / "_edge_enhancer_backtest.json"

EDGE_ENHANCER_ENABLED = os.environ.get("EDGE_ENHANCER", "1").strip().lower() not in (
    "0", "false", "off", "no",
)
MIN_BIAS_SAMPLE = int(os.environ.get("EDGE_ENHANCER_MIN_SAMPLE", "5"))
BIAS_CAP_C = float(os.environ.get("EDGE_ENHANCER_CAP_C", "1.5"))


# ---------------------------------------------------------------------------
# Data loading (local JSON only)
# ---------------------------------------------------------------------------

def load_deviation_samples() -> list[dict]:
    """Return the persistent peak-deviation samples (our peak vs resolution)."""
    if not PEAK_DEVIATION_LOG.exists():
        return []
    try:
        data = json.loads(PEAK_DEVIATION_LOG.read_text(encoding="utf-8"))
        return data.get("samples", []) or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_quality_runs() -> list[dict]:
    if not MODEL_QUALITY_LOG.exists():
        return []
    try:
        data = json.loads(MODEL_QUALITY_LOG.read_text(encoding="utf-8"))
        return data.get("runs", []) or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_resolved_markets() -> dict:
    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        return _load_market_resolved_details()
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Bias computation
# ---------------------------------------------------------------------------

def compute_city_bias(
    samples: list[dict],
    city: str,
    before_date: str | None = None,
    min_sample: int | None = None,
    cap: float | None = None,
) -> tuple[float | None, int, float]:
    """Return (bias_c, n, raw_mean_gap_c) for a city.

    ``bias_c`` is the mean signed gap (our_peak - resolved) clamped to ±cap.
    Returns (None, n, raw_mean) when the city has fewer than ``min_sample``
    usable samples (strictly before ``before_date`` when given — walk-forward).
    """
    min_sample = MIN_BIAS_SAMPLE if min_sample is None else int(min_sample)
    cap = BIAS_CAP_C if cap is None else float(cap)

    gaps: list[float] = []
    for s in samples:
        if s.get("city") != city:
            continue
        d = s.get("date") or ""
        if before_date and d >= before_date:
            continue
        try:
            gaps.append(float(s["gap_c"]))
        except (KeyError, TypeError, ValueError):
            continue

    n = len(gaps)
    if n < min_sample:
        return None, n, 0.0
    raw_mean = sum(gaps) / n
    bias = max(-cap, min(cap, raw_mean))
    return round(bias, 4), n, round(raw_mean, 4)


def live_city_bias(city: str) -> tuple[float | None, int, float]:
    """Return the live (all-history) bias for a city, clamped and gated.

    Used by the live prediction path. Reads the pre-computed per-city
    aggregate from ``_peak_deviation_log.json`` (all history is in the past at
    prediction time, so there is no look-ahead).
    """
    if not EDGE_ENHANCER_ENABLED:
        return None, 0, 0.0
    try:
        if not PEAK_DEVIATION_LOG.exists():
            return None, 0, 0.0
        data = json.loads(PEAK_DEVIATION_LOG.read_text(encoding="utf-8"))
        stats = (data.get("cities", {}) or {}).get(city)
        if not stats:
            return None, 0, 0.0
        raw_mean = float(stats.get("bias_c", 0.0))
        n = int(stats.get("n", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return None, 0, 0.0

    if n < MIN_BIAS_SAMPLE:
        return None, n, round(raw_mean, 4)
    bias = max(-BIAS_CAP_C, min(BIAS_CAP_C, raw_mean))
    return round(bias, 4), n, round(raw_mean, 4)


# ---------------------------------------------------------------------------
# Walk-forward backtest
# ---------------------------------------------------------------------------

def _resolve_spill(spill_c: float | None, market_info: dict | None) -> str | None:
    if spill_c is None or not market_info:
        return None
    try:
        from _model_quality_tracker import _spill_vs_polymarket_result  # type: ignore
        return _spill_vs_polymarket_result(spill_c, market_info)
    except Exception:
        return None


def _resolved_c(market_info: dict) -> float | None:
    if not market_info or market_info.get("value") is None:
        return None
    try:
        value = float(market_info["value"])
    except (TypeError, ValueError):
        return None
    unit = (market_info.get("unit") or "C").upper()
    if unit == "F":
        return (value - 32.0) * 5.0 / 9.0
    return value


def _rate(wins: int, losses: int) -> float:
    total = wins + losses
    return round(wins / total * 100, 1) if total else 0.0


def backtest(min_samples: tuple[int, ...] = (5, 8, 10)) -> dict:
    """Replay resolved predictions with a walk-forward bias correction.

    Returns a dict keyed by min_sample with before/after win rates and MAE,
    plus the list of correction records for the primary min sample.
    """
    samples = load_deviation_samples()
    runs = _load_quality_runs()
    resolved = _load_resolved_markets()

    by_city: dict[str, list[dict]] = defaultdict(list)
    for s in samples:
        by_city[s.get("city", "")].append(s)

    # Records that can be evaluated (point markets with a numeric value).
    evaluated: list[dict] = []
    for run in sorted(runs, key=lambda r: r.get("run_date", "")):
        run_date = run.get("run_date", "")
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            target = pdata.get("_target_date") or run_date
            if not target:
                continue
            base = city.split(",")[0].strip()
            market_info = resolved.get((city, target)) or resolved.get((base, target))
            if not market_info or market_info.get("type") == "threshold":
                continue
            resolved_c = _resolved_c(market_info)
            if resolved_c is None:
                continue

            mean_c = pdata.get("bma_mean")
            std = float(pdata.get("bma_std") or 1.0)
            if mean_c is None:
                continue
            mean_c = float(mean_c)
            p5 = pdata.get("p5")
            sigma = (pdata.get("strategies") or {}).get("sigma") or {}
            k = float(sigma.get("k", 0.5) or 0.5)

            # Uncorrected spills (mirror of the v1 pipeline).
            sigma_unc = int(round(mean_c - k * std))
            mean_unc = int(round(mean_c))
            p5_unc = int(round(p5)) if p5 is not None else None

            evaluated.append({
                "city": city,
                "date": target,
                "mean_c": mean_c,
                "std": std,
                "k": k,
                "p5": p5,
                "resolved_c": resolved_c,
                "market_info": market_info,
                "spills_unc": {"sigma": sigma_unc, "mean": mean_unc, "p5": p5_unc},
            })

    summary: dict[str, dict] = {}
    for ms in min_samples:
        ms_key = str(ms)
        agg_before = {"sigma": [0, 0], "mean": [0, 0], "p5": [0, 0]}
        agg_after = {"sigma": [0, 0], "mean": [0, 0], "p5": [0, 0]}
        mae_before: list[float] = []
        mae_after: list[float] = []
        spill_mae_before: dict[str, list[float]] = {"sigma": [], "mean": [], "p5": []}
        spill_mae_after: dict[str, list[float]] = {"sigma": [], "mean": [], "p5": []}
        n_applied = 0

        for rec in evaluated:
            bias, n, _raw = compute_city_bias(samples, rec["city"], before_date=rec["date"], min_sample=ms)
            if bias is None:
                continue
            n_applied += 1
            mean_c = rec["mean_c"]
            std = rec["std"]
            k = rec["k"]
            p5 = rec["p5"]
            mc = mean_c - bias
            sigma_corr = int(round(mc - k * std))
            mean_corr = int(round(mc))
            p5_corr = int(round(p5 - bias)) if p5 is not None else None

            mae_before.append(abs(mean_c - rec["resolved_c"]))
            mae_after.append(abs(mc - rec["resolved_c"]))

            for sn, unc_spill in rec["spills_unc"].items():
                if unc_spill is not None:
                    spill_mae_before[sn].append(abs(unc_spill - rec["resolved_c"]))
                r = _resolve_spill(unc_spill, rec["market_info"])
                if r == "WIN":
                    agg_before[sn][0] += 1
                elif r == "LOSS":
                    agg_before[sn][1] += 1
            for sn, corr_spill in {"sigma": sigma_corr, "mean": mean_corr, "p5": p5_corr}.items():
                if corr_spill is not None:
                    spill_mae_after[sn].append(abs(corr_spill - rec["resolved_c"]))
                r = _resolve_spill(corr_spill, rec["market_info"])
                if r == "WIN":
                    agg_after[sn][0] += 1
                elif r == "LOSS":
                    agg_after[sn][1] += 1

        def _block(agg: dict, mae: list[float], spill_mae: dict[str, list[float]]) -> dict:
            out: dict[str, Any] = {"mae": round(sum(mae) / len(mae), 4) if mae else None}
            for sn, (w, l) in agg.items():
                smae = spill_mae.get(sn, [])
                out[sn] = {
                    "wins": w,
                    "losses": l,
                    "rate": _rate(w, l),
                    "mae": round(sum(smae) / len(smae), 4) if smae else None,
                }
            return out

        summary[ms_key] = {
            "n_applied": n_applied,
            "n_evaluated": len(evaluated),
            "before": _block(agg_before, mae_before, spill_mae_before),
            "after": _block(agg_after, mae_after, spill_mae_after),
        }

    return summary


def _fmt_block(block: dict) -> str:
    mae = block.get("mae")
    mae_s = f"{mae:.3f}" if mae is not None else "n/a"
    parts = []
    for sn in ("sigma", "mean", "p5"):
        s = block.get(sn, {})
        smae = s.get("mae")
        smae_s = f"{smae:.3f}" if smae is not None else "n/a"
        parts.append(
            f"{sn}={s.get('wins', 0)}W/{s.get('losses', 0)}L "
            f"({s.get('rate', 0.0)}%, MAE={smae_s})"
        )
    return f"BMA-MAE={mae_s} | " + " | ".join(parts)


def main() -> int:
    summary = backtest()
    BACKTEST_LOG.write_text(
        json.dumps({
            "updated": datetime.now(timezone.utc).isoformat(),
            "config": {
                "enabled": EDGE_ENHANCER_ENABLED,
                "min_sample": MIN_BIAS_SAMPLE,
                "cap_c": BIAS_CAP_C,
            },
            "backtest": summary,
        }, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print("═" * 72)
    print("  EDGE ENHANCER BACKTEST — per-station bias correction (walk-forward)")
    print("═" * 72)
    print(f"  enabled={EDGE_ENHANCER_ENABLED} min_sample(default)={MIN_BIAS_SAMPLE} cap={BIAS_CAP_C}°C")
    for ms, block in summary.items():
        print(f"\n  min_sample = {ms}  (correction applied to {block['n_applied']}/{block['n_evaluated']} bets)")
        print(f"    BEFORE: {_fmt_block(block['before'])}")
        print(f"    AFTER : {_fmt_block(block['after'])}")
    print("")
    return 0


if __name__ == "__main__":
    sys.exit(main())
