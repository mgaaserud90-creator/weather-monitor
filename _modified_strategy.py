#!/usr/bin/env python3
"""
Modifisert (Modified) strategy — v1 Polymarket Weather.

A 4th strategy whose spill is a per-city, individually WEIGHTED and
individually CORRECTED mean, computed after removing per-city providers that
are imprecise or inconsistent.

Pipeline (fully deterministic, zero API calls):

  1. REMOVE per-city providers using documented thresholds derived from
     ``_provider_analysis.md``:
         * consistent misser : |bias| >= 1.0 °C  AND  sign-agreement >= 70%
         * oscillator        : std   >= 1.0 °C  AND  sign-agreement in 45-55%
     Every removal is recorded with the exact bias / std / sign-agreement
     values that triggered it.

  2. RECOMPUTE per-city weights over the REMAINING providers:
         inverse-MSE  w_raw_i = 1 / (bias_i^2 + std_i^2 + 0.25)
         normalized to sum 1, then floored at 0.02 and renormalized.
     Weights are stored as percentages (e.g. p1 15%, p2 10%, ...).

  3. MODIFIED MEAN per (city, date):
         modified_mean = sum(w_i * provider_i) over remaining providers
                         present on that date (weights renormalized over the
                         available subset).

  4. CORRECT the modified mean with the city's best correction model from
     ``_per_city_curvefit.json`` (baseline / additive / median / linear /
     multiplicative). Out-of-sample parameters (``oos_params``) are used so
     the backtest does not leak the fit; in-sample parameters are the
     fallback. All correction parameters are in °C (the curve-fit series is
     built on °C means and °C resolutions, see ``_weighted_mean_curvefit.md``).

  5. SPILL = round(corrected_mean) in °C (the project's native storage unit;
     the Polymarket resolver converts °C -> °F internally for °F markets).

  6. RESOLVE against Polymarket using the exact same win rule as the project:
     WIN iff the spill matches the resolved bucket. Point markets:
     round(spill) == round(resolved °C) (i.e. |spill - resolved| <= 0.5 native
     unit); °F bucket markets: native inclusive bounds; threshold markets:
     binary bound comparison. See ``_model_quality_tracker``.

Outputs:
    _modified_strategy_log.json     — per (city,date) rows + per-city aggregates
    _modified_strategy_report.html  — full self-contained HTML documentation

Usage:
    python _modified_strategy.py                 # backfill (default)
    python _modified_strategy.py --mode backfill
    python _modified_strategy.py --mode report   # regenerate HTML only
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

WEIGHTED_PREDICTIONS_FILE = SCRIPT_DIR / "_weighted_mean_predictions.json"
PER_CITY_CURVEFIT_FILE = SCRIPT_DIR / "_per_city_curvefit.json"
PROVIDER_ANALYSIS_FILE = SCRIPT_DIR / "_provider_analysis.md"
MODIFIED_LOG_FILE = SCRIPT_DIR / "_modified_strategy_log.json"
MODIFIED_HTML_FILE = SCRIPT_DIR / "_modified_strategy_report.html"
QUALITY_LOG_FILE = SCRIPT_DIR / "_model_quality_log.json"
DAILY_CITY_LOG_FILE = SCRIPT_DIR / "_daily_city_log.json"

# ── Removal thresholds (documented, derived from _provider_analysis.md) ──
MISSER_BIAS_THRESHOLD = 1.0       # |bias| >= 1.0 °C
MISSER_SIGNAGREE_THRESHOLD = 0.70  # sign-agreement >= 70%
OSCILLATOR_STD_THRESHOLD = 1.0     # std >= 1.0 °C
OSCILLATOR_SIGNAGREE_LO = 0.45     # sign-agreement in [0.45, 0.55]
OSCILLATOR_SIGNAGREE_HI = 0.55

# ── Weighting ──
WEIGHT_FLOOR = 0.02                 # no provider is fully zeroed
MSE_REGULARIZATION = 0.25           # matches inverse-MSE scheme (bias^2 + var + 0.25)

PROVIDER_DISPLAY_TO_KEY: dict[str, str] = {
    "ECMWF IFS": "ecmwf_ifs",
    "GFS": "gfs",
    "ICON": "icon",
    "GEM": "gem",
    "UKMO": "ukmo",
    "JMA": "jma",
    "HRRR": "hrrr",
    "AIFS": "aifs",
}
PROVIDER_KEY_TO_DISPLAY: dict[str, str] = {v: k for k, v in PROVIDER_DISPLAY_TO_KEY.items()}

CORRECTION_METHOD_LABELS: dict[str, str] = {
    "baseline": "baseline (no correction)",
    "additive": "additive offset (mean + c)",
    "median": "median offset (mean + c)",
    "linear": "linear (a + b·mean)",
    "multiplicative": "multiplicative (k·mean)",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# =============================================================================
# Data loading
# =============================================================================

def load_provider_analysis(path: Path = PROVIDER_ANALYSIS_FILE) -> dict[str, dict[str, dict]]:
    """Parse ``_provider_analysis.md`` into {city: {provider_key: stats}}.

    stats keys: bias (°C), std (°C), sign_agree (0-1), n.
    Providers with no data (n == 0) keep None values for the numeric fields.
    """
    text = path.read_text(encoding="utf-8")
    sections: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        m = re.match(r"^## (.+)$", line)
        if m:
            city_name = m.group(1).strip()
            current = city_name
            sections[city_name] = []
        elif current is not None:
            sections[current].append(line)

    def _num(raw: str) -> float | None:
        s = raw.strip().replace("%", "").replace("—", "").replace("+", "")
        if s in ("", "no data"):
            return None
        try:
            return float(s)
        except ValueError:
            return None

    out: dict[str, dict[str, dict]] = {}
    for city, body in sections.items():
        stats: dict[str, dict] = {}
        for line in body:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 10:
                continue
            name = cells[0]
            key = PROVIDER_DISPLAY_TO_KEY.get(name)
            if key is None:
                continue
            bias = _num(cells[3])
            std = _num(cells[6])
            sign_agree_raw = _num(cells[7])
            n_raw = _num(cells[1])
            stats[key] = {
                "bias": bias,
                "std": std,
                "sign_agree": (sign_agree_raw / 100.0) if sign_agree_raw is not None else None,
                "n": int(n_raw) if n_raw else 0,
            }
        out[city] = stats
    return out


def load_curvefit(path: Path = PER_CITY_CURVEFIT_FILE) -> dict[str, dict]:
    """Return {city: curvefit entry} from ``_per_city_curvefit.json``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return {c["city"]: c for c in data.get("cities", [])}


def load_predictions(path: Path = WEIGHTED_PREDICTIONS_FILE) -> dict:
    """Return the full ``_weighted_mean_predictions.json`` payload."""
    return json.loads(path.read_text(encoding="utf-8"))


def load_latest_bma_predictions(path: Path = QUALITY_LOG_FILE) -> dict[str, dict]:
    """Return {city: prediction} from the latest run in ``_model_quality_log.json``.

    This is the fallback forecast source for today when today's per-provider
    values are not present in ``_weighted_mean_predictions.json``. The BMA mean
    is stored in °C (the project's internal unit, see ``_model_quality_tracker``).
    """
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    runs = data.get("runs", []) or []
    if not runs:
        return {}
    return runs[-1].get("predictions", {}) or {}


def resolve_today_str() -> str:
    """Return the pipeline's "today" target date.

    Prefers the latest date present in ``_daily_city_log.json`` — the exact
    date ``_recommended_bets.py`` uses for its Sigma/P5/Mean rows — so the
    modified strategy's today bucket always lines up with the other strategies.
    Falls back to the UTC calendar date.
    """
    if DAILY_CITY_LOG_FILE.exists():
        try:
            data = json.loads(DAILY_CITY_LOG_FILE.read_text(encoding="utf-8"))
            dates = sorted({str(r.get("date", "")) for r in data.get("rows", []) if r.get("date")})
            if dates:
                return dates[-1]
        except (json.JSONDecodeError, OSError):
            pass
    return datetime.now(timezone.utc).date().isoformat()


def _match_city_key(city: str, mapping: dict[str, dict]) -> dict | None:
    """Match a city name against a mapping keyed by city, with fuzzy fallback."""
    if city in mapping:
        return mapping[city]
    base = city.split(",")[0].strip().lower()
    if not base:
        return None
    for key, value in mapping.items():
        if key.split(",")[0].strip().lower() == base:
            return value
    for key, value in mapping.items():
        kbase = key.split(",")[0].strip().lower()
        if kbase and (base in kbase or kbase in base):
            return value
    return None


def _to_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_market_details() -> dict[tuple[str, str], dict]:
    """Unit-aware resolved-market lookup {(city, date): market_info}."""
    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        return _load_market_resolved_details()
    except Exception:
        return {}


def load_modified_log() -> dict:
    """Load the modified strategy log (empty structure when absent/corrupt)."""
    if MODIFIED_LOG_FILE.exists():
        try:
            data = json.loads(MODIFIED_LOG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and "overall" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"meta": {}, "overall": {}, "cities": {}, "records": []}


# =============================================================================
# Removal rules
# =============================================================================

def removal_decisions(provider_stats: dict[str, dict[str, dict]]) -> dict[str, list[dict]]:
    """Apply the documented removal thresholds per city.

    Returns {city: [removal dict, ...]} where each removal dict carries the
    provider key, display name, reason and the exact triggering values.
    """
    decisions: dict[str, list[dict]] = {}
    for city, providers in provider_stats.items():
        removed: list[dict] = []
        for key, s in providers.items():
            if s.get("n", 0) <= 0:
                continue  # no data -> never a "removal", simply unavailable
            bias = s.get("bias")
            std = s.get("std")
            sign = s.get("sign_agree")
            if bias is None or std is None or sign is None:
                continue

            if abs(bias) >= MISSER_BIAS_THRESHOLD and sign >= MISSER_SIGNAGREE_THRESHOLD:
                removed.append({
                    "provider_key": key,
                    "provider": PROVIDER_KEY_TO_DISPLAY.get(key, key),
                    "reason": "consistent misser",
                    "rule": (
                        f"|bias| {abs(bias):.2f}°C >= {MISSER_BIAS_THRESHOLD:.1f}°C "
                        f"AND sign-agreement {sign * 100:.0f}% >= {MISSER_SIGNAGREE_THRESHOLD * 100:.0f}%"
                    ),
                    "bias": round(bias, 3),
                    "std": round(std, 3),
                    "sign_agree": round(sign, 4),
                })
            elif (
                std >= OSCILLATOR_STD_THRESHOLD
                and OSCILLATOR_SIGNAGREE_LO <= sign <= OSCILLATOR_SIGNAGREE_HI
            ):
                removed.append({
                    "provider_key": key,
                    "provider": PROVIDER_KEY_TO_DISPLAY.get(key, key),
                    "reason": "oscillator",
                    "rule": (
                        f"std {std:.2f}°C >= {OSCILLATOR_STD_THRESHOLD:.1f}°C "
                        f"AND sign-agreement {sign * 100:.0f}% in "
                        f"{OSCILLATOR_SIGNAGREE_LO * 100:.0f}-{OSCILLATOR_SIGNAGREE_HI * 100:.0f}%"
                    ),
                    "bias": round(bias, 3),
                    "std": round(std, 3),
                    "sign_agree": round(sign, 4),
                })
        decisions[city] = removed
    return decisions


# =============================================================================
# Weighting
# =============================================================================

def recompute_weights(
    provider_stats: dict[str, dict],
    removed_keys: set[str],
) -> dict[str, float]:
    """Inverse-MSE weights over remaining (n>0, not removed) providers.

    Returns {provider_key: percentage} summing to ~100.
    """
    raw: dict[str, float] = {}
    for key, s in provider_stats.items():
        if key in removed_keys or s.get("n", 0) <= 0:
            continue
        bias = s.get("bias")
        std = s.get("std")
        if bias is None or std is None:
            continue
        mse = bias * bias + std * std
        raw[key] = 1.0 / (mse + MSE_REGULARIZATION)

    if not raw:
        return {}

    total = sum(raw.values())
    weights = {k: v / total for k, v in raw.items()}

    # Floor so no provider is zeroed, then renormalize.
    weights = {k: max(v, WEIGHT_FLOOR) for k, v in weights.items()}
    total = sum(weights.values())
    weights = {k: v / total for k, v in weights.items()}

    # Percentage weights summing to ~100.
    return {k: round(v * 100.0, 2) for k, v in weights.items()}


def build_cities_config(
    provider_stats: dict[str, dict[str, dict]],
    curvefit: dict[str, dict],
    decisions: dict[str, list[dict]],
) -> dict[str, dict]:
    """Assemble the per-city static strategy configuration.

    For every city this stores the removed providers, the recomputed remaining
    provider weights (percentages) and the city's best correction model +
    parameters. This is the self-contained recipe used for both historical
    backfill rows and today's modified spill.
    """
    cities_cfg: dict[str, dict] = {}
    for city, stats in provider_stats.items():
        cf = curvefit.get(city, {})
        removed = decisions.get(city, [])
        removed_keys = {r["provider_key"] for r in removed}
        weights_pct = recompute_weights(stats, removed_keys)
        method, params, params_source = correction_params(cf)
        cities_cfg[city] = {
            "unit": cf.get("unit", "C"),
            "verdict": cf.get("verdict", ""),
            "removed": removed,
            "remaining_weights": weights_pct,
            "correction_method": method,
            "correction_params": params,
            "correction_params_source": params_source,
            "wins": 0,
            "losses": 0,
        }
    return cities_cfg


# =============================================================================
# Correction
# =============================================================================

def correction_params(curvefit_entry: dict) -> tuple[str, dict[str, float], str]:
    """Return (method, params, params_source) for a city.

    Prefers out-of-sample parameters so the backtest does not leak the fit.
    """
    method = curvefit_entry.get("best_method", "baseline")
    oos = curvefit_entry.get("oos_params", {})
    ins = curvefit_entry.get("in_sample_params", {})
    if method in oos and oos[method]:
        return method, {k: float(v) for k, v in oos[method].items()}, "oos"
    if method in ins and ins[method]:
        return method, {k: float(v) for k, v in ins[method].items()}, "in_sample"
    return method, {}, "none"


def apply_correction(method: str, params: dict[str, float], mean_c: float) -> float:
    """Apply a correction model to a mean value (°C). All params are °C-scaled."""
    if method == "additive" or method == "median":
        return mean_c + params.get("c", 0.0)
    if method == "multiplicative":
        return mean_c * params.get("k", 1.0)
    if method == "linear":
        return params.get("a", 0.0) + params.get("b", 1.0) * mean_c
    return mean_c  # baseline


def compute_today_records(
    cities_cfg: dict[str, dict],
    today_str: str,
    predictions: list[dict],
    bma_preds: dict[str, dict],
) -> tuple[list[dict], dict]:
    """Compute TODAY's modified spill for every city in the config.

    Primary path  — per-provider daily-max values for ``today_str`` taken from
    ``_weighted_mean_predictions.json`` (the Open-Meteo per-model archive the
    project's analysis uses). The city's remaining providers are weighted with
    the city's own ``remaining_weights`` (renormalized over the available
    subset) and the city's correction model is applied.

    Fallback path — today's BMA mean from the latest ``_model_quality_log.json``
    run, corrected with the city's correction model. Used when today's
    per-provider values are unavailable (the current situation for all cities
    except Wellington, NZ).

    The chosen path is recorded per row as ``today_source`` so the computation
    is fully auditable. Spill is ``round(corrected_mean)`` in °C (native unit).
    """
    today_provider_rows: dict[str, dict] = {}
    for rec in predictions:
        if str(rec.get("date", "")) == today_str:
            city = str(rec.get("city", "")).strip()
            providers = rec.get("providers", {}) or {}
            if city and providers:
                today_provider_rows[city] = providers

    records: list[dict] = []
    summary: dict = {}

    for city, cfg in sorted(cities_cfg.items()):
        weights_pct = cfg.get("remaining_weights", {})
        method = cfg.get("correction_method", "baseline")
        params = cfg.get("correction_params", {})

        weighted_mean: float | None = None
        providers_used: list[str] = []
        source = ""

        # 1) Prefer today's per-provider values (Open-Meteo per-model source).
        providers = today_provider_rows.get(city, {})
        available = []
        for key, raw in providers.items():
            if key in weights_pct:
                val = _to_float(raw)
                if val is not None:
                    available.append((key, val))
        if available:
            wsum = sum(weights_pct[key] for key, _ in available)
            if wsum > 0:
                weighted_mean = sum(weights_pct[key] * val for key, val in available) / wsum
                providers_used = [key for key, _ in available]
                source = "open_meteo_per_model"

        # 2) Fallback to the latest-run BMA mean and apply the correction.
        if weighted_mean is None:
            bma_mean = None
            entry = _match_city_key(city, bma_preds)
            if isinstance(entry, dict):
                bma_mean = _to_float(entry.get("bma_mean"))
            if bma_mean is not None:
                weighted_mean = bma_mean
                source = "bma_mean_fallback"

        if weighted_mean is None:
            summary[city] = {
                "date": today_str,
                "spill": None,
                "today_source": "unavailable",
                "reason": "no per-provider values and no BMA mean for today",
                "remaining_weights": weights_pct,
                "correction_method": method,
                "correction_params": params,
            }
            continue

        corrected_mean = apply_correction(method, params, weighted_mean)
        spill = int(round(corrected_mean))

        record = {
            "city": city,
            "date": today_str,
            "weighted_mean": round(weighted_mean, 4),
            "correction_method": method,
            "correction": round(corrected_mean, 4),
            "corrected_mean": round(corrected_mean, 4),
            "spill": spill,
            "resolved": None,
            "resolved_bucket": None,
            "market_type": None,
            "market_unit": cfg.get("unit"),
            "result": None,
            "plain_mean": None,
            "plain_mean_spill": None,
            "plain_mean_result": None,
            "today_source": source,
            "providers_used": providers_used,
        }
        records.append(record)
        summary[city] = {
            "date": today_str,
            "spill": spill,
            "today_source": source,
            "providers_used": providers_used,
            "weighted_mean": round(weighted_mean, 4),
            "corrected_mean": round(corrected_mean, 4),
            "correction_method": method,
            "correction_params": params,
            "remaining_weights": weights_pct,
        }

    return records, summary


# =============================================================================
# Resolution (project win rule)
# =============================================================================

def resolve_spill(spill_c: int, market_info: dict | None) -> str | None:
    """Resolve a strategy spill (°C integer) against a Polymarket outcome.

    Reuses the project's exact win rule from ``_model_quality_tracker``.
    Returns "WIN", "LOSS" or None (unresolvable).
    """
    if spill_c is None or not market_info:
        return None
    try:
        if market_info.get("type") == "threshold":
            from _model_quality_tracker import _spill_vs_threshold_result  # type: ignore
            return _spill_vs_threshold_result(spill_c, market_info)
        from _model_quality_tracker import _spill_vs_polymarket_result  # type: ignore
        return _spill_vs_polymarket_result(spill_c, market_info)
    except Exception:
        return None


def market_info_for(city: str, date_str: str, markets: dict) -> dict | None:
    base = city.split(",")[0].strip()
    return markets.get((city, date_str)) or markets.get((base, date_str))


def resolved_value_c(market_info: dict | None) -> float | None:
    """Resolved point temperature in °C (None for threshold/unresolvable)."""
    if not market_info or market_info.get("type") == "threshold":
        return None
    value = market_info.get("value")
    if value is None:
        return None
    value = float(value)
    if (market_info.get("unit") or "C").upper() == "F":
        return (value - 32.0) * 5.0 / 9.0
    return value


# =============================================================================
# Core computation
# =============================================================================

def build_log() -> dict:
    """Compute the full modified-strategy backtest and assemble the log."""
    provider_stats = load_provider_analysis()
    curvefit = load_curvefit()
    payload = load_predictions()
    predictions: list[dict] = payload.get("predictions", [])
    markets = load_market_details()

    decisions = removal_decisions(provider_stats)

    # Per-city static configuration (removed providers + weights + correction).
    cities_cfg = build_cities_config(provider_stats, curvefit, decisions)

    records: list[dict] = []
    for rec in predictions:
        city = rec.get("city", "")
        date_str = rec.get("date", "")
        cfg = cities_cfg.get(city)
        providers = rec.get("providers", {}) or {}

        # Plain equal-mean baseline (unweighted average of all provided values).
        plain_vals = [float(v) for v in providers.values() if v is not None]
        plain_mean = sum(plain_vals) / len(plain_vals) if plain_vals else None

        # Modified weighted mean over remaining ∩ available providers.
        if cfg is None:
            continue
        weights_pct = cfg["remaining_weights"]
        available = [k for k in providers.keys() if k in weights_pct]
        if not available:
            # Every remaining provider missing on this date -> cannot compute.
            continue
        wsum = sum(weights_pct[k] for k in available)
        modified_mean = sum(weights_pct[k] * float(providers[k]) for k in available) / wsum

        method = cfg["correction_method"]
        params = cfg["correction_params"]
        corrected_mean = apply_correction(method, params, modified_mean)
        spill = int(round(corrected_mean))

        market_info = market_info_for(city, date_str, markets)
        result = resolve_spill(spill, market_info)
        resolved_c = resolved_value_c(market_info)

        plain_spill = int(round(plain_mean)) if plain_mean is not None else None
        plain_result = resolve_spill(plain_spill, market_info) if plain_spill is not None else None

        if result == "WIN":
            cfg["wins"] += 1
        elif result == "LOSS":
            cfg["losses"] += 1

        records.append({
            "city": city,
            "date": date_str,
            "weighted_mean": round(modified_mean, 4),
            "correction_method": method,
            "correction": round(corrected_mean, 4),
            "corrected_mean": round(corrected_mean, 4),
            "spill": spill,
            "resolved": round(resolved_c, 2) if resolved_c is not None else None,
            "resolved_bucket": (market_info or {}).get("bucket"),
            "market_type": (market_info or {}).get("type"),
            "market_unit": (market_info or {}).get("unit"),
            "result": result,
            "plain_mean": round(plain_mean, 4) if plain_mean is not None else None,
            "plain_mean_spill": plain_spill,
            "plain_mean_result": plain_result,
        })

    # ── Today's modified spill (per city, self-contained) ──
    today_str = resolve_today_str()
    today_records, today_summary = compute_today_records(
        cities_cfg, today_str, predictions, load_latest_bma_predictions()
    )
    existing_pairs = {(r["city"], r["date"]) for r in records}
    for tr in today_records:
        if (tr["city"], tr["date"]) not in existing_pairs:
            records.append(tr)

    # Per-city aggregates.
    cities_out: dict[str, dict] = {}
    for city, cfg in sorted(cities_cfg.items()):
        bets = cfg["wins"] + cfg["losses"]
        cities_out[city] = {
            "unit": cfg["unit"],
            "verdict": cfg["verdict"],
            "removed": cfg["removed"],
            "remaining_weights": cfg["remaining_weights"],
            "correction": {
                "method": cfg["correction_method"],
                "method_label": CORRECTION_METHOD_LABELS.get(cfg["correction_method"], cfg["correction_method"]),
                "params": cfg["correction_params"],
                "params_source": cfg["correction_params_source"],
            },
            "wins": cfg["wins"],
            "losses": cfg["losses"],
            "bets": bets,
            "win_rate": round(cfg["wins"] / bets * 100, 1) if bets else None,
        }

    wins = sum(c["wins"] for c in cities_out.values())
    losses = sum(c["losses"] for c in cities_out.values())
    bets = wins + losses

    plain_wins = sum(1 for r in records if r["plain_mean_result"] == "WIN")
    plain_losses = sum(1 for r in records if r["plain_mean_result"] == "LOSS")
    plain_bets = plain_wins + plain_losses

    removed_counts = [len(c["removed"]) for c in cities_out.values()]
    removed_total = sum(removed_counts)

    return {
        "meta": {
            "generated": _now_iso(),
            "description": "Modifisert strategy: per-city provider removal + inverse-MSE reweighting + per-city correction.",
            "removal_rule": {
                "consistent_misser": (
                    f"|bias| >= {MISSER_BIAS_THRESHOLD}°C AND sign-agreement >= "
                    f"{MISSER_SIGNAGREE_THRESHOLD * 100:.0f}%"
                ),
                "oscillator": (
                    f"std >= {OSCILLATOR_STD_THRESHOLD}°C AND sign-agreement in "
                    f"{OSCILLATOR_SIGNAGREE_LO * 100:.0f}-{OSCILLATOR_SIGNAGREE_HI * 100:.0f}%"
                ),
            },
            "weighting": "inverse-MSE (1/(bias^2 + std^2 + 0.25)), normalized, floor 0.02, renormalized",
            "correction": "per-city best method from _per_city_curvefit.json, out-of-sample params (in-sample fallback), °C scale",
            "win_rule": "WIN iff spill matches the resolved bucket (project _spill_vs_polymarket_result / _spill_vs_threshold_result)",
            "n_cities_with_config": len(cities_out),
            "n_cities_with_bets": sum(1 for c in cities_out.values() if c["bets"] > 0),
            "n_cities_with_today_spill": sum(1 for v in today_summary.values() if v.get("spill") is not None),
            "avg_providers_removed_per_city": round(removed_total / len(cities_out), 2) if cities_out else 0.0,
            "total_providers_removed": removed_total,
        },
        "overall": {
            "wins": wins,
            "losses": losses,
            "bets": bets,
            "win_rate": round(wins / bets * 100, 1) if bets else None,
            "plain_mean_wins": plain_wins,
            "plain_mean_losses": plain_losses,
            "plain_mean_bets": plain_bets,
            "plain_mean_win_rate": round(plain_wins / plain_bets * 100, 1) if plain_bets else None,
        },
        "cities": cities_out,
        "today": {
            "date": today_str,
            "computed_at": _now_iso(),
            "source_priority": (
                "1) today's per-provider values from _weighted_mean_predictions.json "
                "(Open-Meteo per-model source); 2) latest-run BMA mean from "
                "_model_quality_log.json + city correction (fallback). "
                "Each city's chosen path is recorded in rows[].today_source."
            ),
            "rows": today_summary,
        },
        "records": records,
    }


def save_log(log: dict) -> None:
    MODIFIED_LOG_FILE.write_text(
        json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8"
    )


# =============================================================================
# HTML report
# =============================================================================

def _esc(text: Any) -> str:
    return (
        str(text)
        .replace("&", "&")
        .replace("<", "<")
        .replace(">", ">")
    )


def _rate_color(rate: float | None) -> str:
    if rate is None:
        return "#8b949e"
    if rate >= 60:
        return "#3fb950"
    if rate >= 50:
        return "#d2991d"
    return "#f85149"


def build_html_report(log: dict) -> str:
    """Render a full, self-contained HTML report (inline CSS, no assets)."""
    meta = log.get("meta", {})
    overall = log.get("overall", {})
    cities = log.get("cities", {})

    ow = overall.get("win_rate")
    pw = overall.get("plain_mean_win_rate")

    def _pct(v) -> str:
        return f"{v:.1f}%" if v is not None else "n/a"

    city_blocks: list[str] = []
    for city, c in cities.items():
        # (a) removed providers + reason
        removed_html = ""
        if c.get("removed"):
            rows = []
            for r in c["removed"]:
                rows.append(
                    f"<tr><td>{_esc(r['provider'])}</td><td>{_esc(r['reason'])}</td>"
                    f"<td>{r['bias']:+.2f}°C</td><td>{r['std']:.2f}°C</td>"
                    f"<td>{r['sign_agree'] * 100:.0f}%</td>"
                    f"<td style='font-size:0.75rem;color:#8b949e;'>{_esc(r['rule'])}</td></tr>"
                )
            removed_html = (
                "<h4 style='margin:14px 0 6px;color:#f85149;'>🗑️ Fjernede providere</h4>"
                "<table><thead><tr><th>Provider</th><th>Årsak</th><th>Bias</th><th>Std</th>"
                "<th>Sign-agree</th><th>Regel</th></tr></thead><tbody>"
                + "".join(rows) + "</tbody></table>"
            )
        else:
            removed_html = (
                "<p style='color:#8b949e;margin:8px 0;'>Ingen providere fjernet — "
                "alle beholdes.</p>"
            )

        # (b) remaining providers + percentage weights
        weights = c.get("remaining_weights", {})
        weight_items = "".join(
            f"<span class='chip'>{_esc(PROVIDER_KEY_TO_DISPLAY.get(k, k))} "
            f"<b>{v:.1f}%</b></span>"
            for k, v in sorted(weights.items(), key=lambda kv: -kv[1])
        ) or "<span style='color:#8b949e;'>Ingen</span>"

        # (c) correction model + parameters
        corr = c.get("correction", {})
        params = corr.get("params", {})
        params_str = ", ".join(f"{k} = {v:.4f}" for k, v in params.items()) if params else "—"

        # (d) W/L
        wr = c.get("win_rate")
        wr_color = _rate_color(wr)
        bets = c.get("bets", 0)

        city_blocks.append(f"""
      <div class="city">
        <div class="city-head">
          <span class="city-name">{_esc(city)}</span>
          <span class="city-unit">unit {_esc(c.get('unit', 'C'))} · {_esc(c.get('verdict', ''))}</span>
          <span class="city-wl" style="color:{wr_color};">{c['wins']}W / {c['losses']}L ({_pct(wr)}) n={bets}</span>
        </div>
        {removed_html}
        <h4 style="margin:14px 0 6px;">⚖️ Gjenværende providere og vekter</h4>
        <div class="chips">{weight_items}</div>
        <h4 style="margin:14px 0 6px;">🔧 Korreksjonsmodell</h4>
        <p style="margin:0 0 4px;">
          <b>{_esc(corr.get('method_label', corr.get('method', '')))}</b>
          <span style="color:#8b949e;"> · params: {_esc(params_str)} · kilde: {_esc(corr.get('params_source', ''))}</span>
        </p>
      </div>""")

    city_blocks_html = "\n".join(city_blocks)

    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Modifisert strategi — rapport</title>
<style>
  :root {{
    --bg: #0d1117; --card: #161b22; --border: #30363d;
    --text: #c9d1d9; --dim: #8b949e; --green: #3fb950;
    --red: #f85149; --orange: #d2991d; --blue: #58a6ff; --purple: #bc8cff;
  }}
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,'Segoe UI',Roboto,sans-serif; line-height:1.55; padding:24px; }}
  .wrap {{ max-width:1200px; margin:0 auto; }}
  header {{ text-align:center; padding:24px 16px; border-bottom:1px solid var(--border); margin-bottom:24px; }}
  header h1 {{ color:var(--blue); font-size:1.6rem; }}
  header p {{ color:var(--dim); font-size:0.9rem; margin-top:6px; }}
  .cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); gap:14px; margin-bottom:24px; }}
  .card {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:18px; text-align:center; }}
  .card .value {{ font-size:1.9rem; font-weight:700; }}
  .card .label {{ color:var(--dim); font-size:0.8rem; margin-top:4px; }}
  .section {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:20px; margin-bottom:18px; }}
  .section h2 {{ color:var(--purple); font-size:1.15rem; margin-bottom:12px; border-bottom:1px solid var(--border); padding-bottom:8px; }}
  table {{ width:100%; border-collapse:collapse; font-size:0.82rem; }}
  th, td {{ text-align:left; padding:7px 9px; border-bottom:1px solid var(--border); vertical-align:top; }}
  th {{ color:var(--dim); text-transform:uppercase; font-size:0.68rem; letter-spacing:0.4px; }}
  tr:hover {{ background:rgba(255,255,255,0.02); }}
  .city {{ background:var(--card); border:1px solid var(--border); border-radius:10px; padding:16px; margin-bottom:14px; }}
  .city-head {{ display:flex; flex-wrap:wrap; gap:10px; align-items:baseline; border-bottom:1px solid var(--border); padding-bottom:8px; margin-bottom:6px; }}
  .city-name {{ font-size:1.05rem; font-weight:700; color:var(--blue); }}
  .city-unit {{ color:var(--dim); font-size:0.75rem; }}
  .city-wl {{ margin-left:auto; font-weight:700; }}
  .chips {{ display:flex; flex-wrap:wrap; gap:8px; }}
  .chip {{ background:rgba(88,166,255,0.12); border:1px solid rgba(88,166,255,0.4); color:var(--blue); border-radius:999px; padding:4px 11px; font-size:0.8rem; }}
  code {{ background:rgba(255,255,255,0.06); padding:1px 6px; border-radius:5px; font-size:0.8em; }}
  .muted {{ color:var(--dim); font-size:0.85rem; }}
  .note {{ background:rgba(88,166,255,0.08); border-left:3px solid var(--blue); padding:12px 14px; border-radius:6px; margin-bottom:18px; font-size:0.85rem; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>🧪 Modifisert strategi — vektet + korrigert per by</h1>
    <p>Generert {_esc(meta.get('generated', ''))} · Selvstendig HTML (inline CSS, ingen eksterne ressurser)</p>
  </header>

  <div class="cards">
    <div class="card"><div class="value" style="color:{_rate_color(ow)};">{_pct(ow)}</div><div class="label">Modifisert — total win rate</div></div>
    <div class="card"><div class="value">{overall.get('wins', 0)}W / {overall.get('losses', 0)}L</div><div class="label">Modifisert — kumulativ W/L</div></div>
    <div class="card"><div class="value" style="color:{_rate_color(pw)};">{_pct(pw)}</div><div class="label">Vanlig mean — win rate</div></div>
    <div class="card"><div class="value">{overall.get('plain_mean_wins', 0)}W / {overall.get('plain_mean_losses', 0)}L</div><div class="label">Vanlig mean — kumulativ W/L</div></div>
    <div class="card"><div class="value" style="color:var(--orange);">{meta.get('avg_providers_removed_per_city', 0)}</div><div class="label">Snitt fjernede providere / by</div></div>
    <div class="card"><div class="value" style="color:var(--red);">{meta.get('total_providers_removed', 0)}</div><div class="label">Totalt fjernede providere</div></div>
  </div>

  <div class="note">
    <b>Fjerningsregel:</b> fjern en provider fra en by hvis
    <code>|bias| ≥ {MISSER_BIAS_THRESHOLD}°C OG sign-agreement ≥ {MISSER_SIGNAGREE_THRESHOLD * 100:.0f}%</code> (konsistent misser),
    ELLER <code>std ≥ {OSCILLATOR_STD_THRESHOLD}°C OG sign-agreement {OSCILLATOR_SIGNAGREE_LO * 100:.0f}–{OSCILLATOR_SIGNAGREE_HI * 100:.0f}%</code> (oscillator).
    Vekting: invers-MSE (<code>1/(bias² + std² + 0.25)</code>) over gjenværende providere, normalisert, gulv 0.02.
    Korreksjon: byens beste modell fra <code>_per_city_curvefit.json</code> (out-of-sample parametre, °C-skala).
    Spill = <code>round(korrigert_mean)</code> °C. Win-regel: prosjektets — WIN hvis spillet treffer resolved bucket.
  </div>

  <div class="section">
    <h2>🏙️ Per by — fjerning, vekter, korreksjon og resultat</h2>
    {city_blocks_html}
  </div>

  <footer style="text-align:center;color:var(--dim);font-size:0.8rem;padding:16px;">
    Modifisert strategi · v1 Polymarket Weather · {meta.get('n_cities_with_bets', 0)} byer med resolvede spill
  </footer>
</div>
</body>
</html>"""
    return html


# =============================================================================
# Entry points
# =============================================================================

def backfill() -> int:
    """Recompute the modified strategy over ALL historical (city, date) rows."""
    print("╔══════════════════════════════════════════════════╗")
    print("║   MODIFISERT STRATEGI — BACKFILL                ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_iso()}")

    log = build_log()
    save_log(log)
    overall = log["overall"]
    meta = log["meta"]

    print(f"   Byer med konfig:        {meta.get('n_cities_with_config')}")
    print(f"   Byer med resolvede spill: {meta.get('n_cities_with_bets')}")
    print(f"   Fjernede providere:     {meta.get('total_providers_removed')} "
          f"(snitt {meta.get('avg_providers_removed_per_city')}/by)")
    print(f"   Modifisert: {overall.get('wins')}W/{overall.get('losses')}L "
          f"({overall.get('win_rate')}%)")
    print(f"   Vanlig mean: {overall.get('plain_mean_wins')}W/{overall.get('plain_mean_losses')}L "
          f"({overall.get('plain_mean_win_rate')}%)")

    html = build_html_report(log)
    MODIFIED_HTML_FILE.write_text(html, encoding="utf-8")
    print(f"   Logg:    {MODIFIED_LOG_FILE.name}")
    print(f"   Rapport: {MODIFIED_HTML_FILE.name}")
    print("   ✅ Backfill fullført.\n")
    return 0


def report_only() -> int:
    """Regenerate the HTML report from the existing log."""
    log = load_modified_log()
    if not log.get("records") and not log.get("cities"):
        print("⚠️ Ingen modifisert logg funnet — kjører backfill først.")
        return backfill()
    MODIFIED_HTML_FILE.write_text(build_html_report(log), encoding="utf-8")
    print(f"Rapport regenerert: {MODIFIED_HTML_FILE}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Modifisert (modified) strategy backfill + report")
    parser.add_argument(
        "--mode",
        choices=["backfill", "report"],
        default="backfill",
        help="backfill (default) or report (regenerate HTML only)",
    )
    args = parser.parse_args()
    if args.mode == "report":
        sys.exit(report_only())
    sys.exit(backfill())


if __name__ == "__main__":
    main()
