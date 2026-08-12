#!/usr/bin/env python3
"""
Per-city deviation statistics log.

Pairs each resolved POINT market (from _resolved_markets_log.json) with our
MEAN prediction ("strategies.mean.spill", always in °C) from the matching run
in _model_quality_log.json (matched on the exact date), computes the
deviation, and accumulates a persistent per-city statistics database.

For each paired sample we store:
  - spill_c         : our mean bet in °C
  - resolved_value  : the resolved market value in its native unit
  - unit            : "C" or "F" (native market unit)
  - resolved_c      : resolved value normalised to °C
  - error_c         : spill_c - resolved_c  (cross-city aggregation unit)
  - error_native    : native-unit error (spill in native unit - resolved_value)

Threshold markets are skipped entirely. The log is append-only (deduplicated
by (city, date)) and the per-city aggregates (n, mean error, MAE, RMSE, last
error) are recomputed from the full sample history on every run.

This script is pure standard-library Python and writes/reads only the three
JSON files next to it, so it works identically locally and in CI.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
MODEL_QUALITY_LOG = BASE_DIR / "_model_quality_log.json"
RESOLVED_MARKETS_LOG = BASE_DIR / "_resolved_markets_log.json"
CITY_DEVIATION_LOG = BASE_DIR / "_city_deviation_log.json"

# Rounding precision for stored floats (degrees / degree-errors).
ROUND_NDIGITS = 4


def f_to_c(value: float) -> float:
    return (value - 32.0) * 5.0 / 9.0


def c_to_f(value: float) -> float:
    return value * 9.0 / 5.0 + 32.0


def _round(value: float) -> float:
    return round(float(value), ROUND_NDIGITS)


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_runs_by_date() -> dict:
    """Return {run_date: run} from _model_quality_log.json (last run wins)."""
    if not MODEL_QUALITY_LOG.exists():
        return {}
    data = load_json(MODEL_QUALITY_LOG)
    by_date = {}
    for run in data.get("runs", []):
        run_date = run.get("run_date")
        if run_date:
            by_date[run_date] = run
    return by_date


def get_mean_spill_c(run: dict, city: str):
    """Return strategies.mean.spill for a city in a run, or None if missing."""
    predictions = run.get("predictions", {})
    pred = predictions.get(city)
    if not pred:
        return None
    mean = pred.get("strategies", {}).get("mean")
    if not mean or mean.get("spill") is None:
        return None
    try:
        return float(mean["spill"])
    except (TypeError, ValueError):
        return None


def resolve_market(market: dict):
    """
    Return (unit, resolved_value, resolved_c) for a POINT market.

    Returns None when the market is a threshold market (skipped) or when the
    resolved value cannot be determined.
    """
    market_type = market.get("type")
    if market_type == "threshold":
        return None

    unit = market.get("unit")
    if unit is None:
        # Legacy Celsius entries: resolved value is temp_c, already in °C.
        unit = "C"
        raw = market.get("value", market.get("temp_c"))
    elif unit == "F":
        raw = market.get("value", market.get("temp_f"))
    elif unit == "C":
        raw = market.get("value", market.get("temp_c"))
    else:
        # Unknown unit — do not pair.
        return None

    if raw is None:
        return None
    try:
        resolved_value = float(raw)
    except (TypeError, ValueError):
        return None

    if unit == "F":
        resolved_c = f_to_c(resolved_value)
    else:
        resolved_c = resolved_value

    return unit, resolved_value, resolved_c


def load_existing_samples() -> dict:
    """Return existing (city, date) -> sample from the persistent log."""
    existing = {}
    if CITY_DEVIATION_LOG.exists():
        try:
            prev = load_json(CITY_DEVIATION_LOG)
        except (json.JSONDecodeError, OSError):
            prev = {}
        for sample in prev.get("samples", []):
            city = sample.get("city")
            date = sample.get("date")
            if city and date:
                existing[(city, date)] = sample
    return existing


def compute_city_aggregates(samples: list) -> dict:
    """Recompute per-city aggregates from the full sample history."""
    by_city = defaultdict(list)
    for sample in samples:
        by_city[sample["city"]].append(sample)

    cities = {}
    for city, city_samples in by_city.items():
        ordered = sorted(city_samples, key=lambda s: s["date"])
        n = len(ordered)
        errors = [s["error_c"] for s in ordered]
        mean_error = sum(errors) / n
        mae = sum(abs(e) for e in errors) / n
        rmse = math.sqrt(sum(e * e for e in errors) / n)
        last = ordered[-1]
        cities[city] = {
            "n": n,
            "mean_error_c": _round(mean_error),
            "mae_c": _round(mae),
            "rmse_c": _round(rmse),
            "last_error_c": _round(last["error_c"]),
            "unit": last["unit"],
        }
    return cities


def main() -> None:
    runs_by_date = load_runs_by_date()

    resolved_data = {}
    if RESOLVED_MARKETS_LOG.exists():
        resolved_data = load_json(RESOLVED_MARKETS_LOG)

    markets = resolved_data.get("markets", {})

    # 1. Pair each resolved POINT market with the matching run's mean.spill.
    new_samples = {}
    skipped_threshold = 0
    skipped_unresolvable = 0
    skipped_no_run = 0
    skipped_no_spill = 0

    for key, market in markets.items():
        city = market.get("city")
        date = market.get("date")

        # Fall back to parsing the "City||YYYY-MM-DD" key if fields are absent.
        if (not city or not date) and isinstance(key, str) and "||" in key:
            key_city, _, key_date = key.partition("||")
            city = city or key_city
            date = date or key_date

        if not city or not date:
            continue

        if market.get("type") == "threshold":
            skipped_threshold += 1
            continue

        resolved = resolve_market(market)
        if resolved is None:
            skipped_unresolvable += 1
            continue
        unit, resolved_value, resolved_c = resolved

        run = runs_by_date.get(date)
        if run is None:
            skipped_no_run += 1
            continue

        spill_c = get_mean_spill_c(run, city)
        if spill_c is None:
            skipped_no_spill += 1
            continue

        error_c = spill_c - resolved_c
        spill_native = spill_c if unit == "C" else c_to_f(spill_c)
        error_native = spill_native - resolved_value

        new_samples[(city, date)] = {
            "city": city,
            "date": date,
            "spill_c": _round(spill_c),
            "resolved_value": _round(resolved_value),
            "unit": unit,
            "resolved_c": _round(resolved_c),
            "error_c": _round(error_c),
            "error_native": _round(error_native),
        }

    # 2. Append-only merge (deduplicate by (city, date); never overwrite).
    merged = load_existing_samples()
    added_count = sum(1 for key in new_samples if key not in merged)
    for sample_key, sample in new_samples.items():
        merged.setdefault(sample_key, sample)

    samples = sorted(merged.values(), key=lambda s: (s["date"], s["city"]))

    # 3. Recompute per-city aggregates from the full sample history.
    cities = compute_city_aggregates(samples)
    ordered_cities = dict(
        sorted(
            cities.items(),
            key=lambda item: (-item[1]["n"], item[0].lower()),
        )
    )

    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "samples": samples,
        "cities": ordered_cities,
    }

    with open(CITY_DEVIATION_LOG, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
        fh.write("\n")

    # 4. Compact per-city summary for CI logs.
    print(
        "Per-city deviation stats — "
        f"{len(samples)} samples, {len(ordered_cities)} cities "
        f"(added this run: {added_count}, "
        f"skipped: threshold={skipped_threshold}, unresolvable={skipped_unresolvable}, "
        f"no_run={skipped_no_run}, no_spill={skipped_no_spill})"
    )
    print(f"{'city':<24} {'n':>4} {'mean_error_c':>13}")
    for city, stats in ordered_cities.items():
        print(
            f"{city:<24} {stats['n']:>4} {stats['mean_error_c']:>+13.3f}"
        )


if __name__ == "__main__":
    main()
