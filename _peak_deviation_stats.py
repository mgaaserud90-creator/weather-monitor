#!/usr/bin/env python3
"""
Persistent daily peak-deviation tracker ("our peak vs Polymarket resolution").

Accumulates every peak verification from ``_peak_verification_log.json`` into a
persistent ``_peak_deviation_log.json`` so the per-day gap becomes meaningful
statistics over time. The source file is wiped and rebuilt every 30 minutes by
``_populate_peak_verify.py``, so this module keeps a permanent sample history.

For each (city, date) we store one sample:
  - city           : "City, CC" key from the verification log
  - date           : verification date (YYYY-MM-DD)
  - our_peak       : our archive peak in the market's native unit
  - market_resolved: Polymarket resolved value in the native unit
  - unit           : "C" or "F"
  - gap            : our_peak - market_resolved in the native unit
  - gap_c          : gap normalised to °C (gap * 5/9 when unit == "F")
  - verdict        : OK / MINOR / STATION_MISMATCH

Aggregates are recomputed from the FULL sample history on every run:

  cities : per-city n, mean_gap_c, bias_c (mean signed gap), std_gap_c,
           mae_c, rmse_c, min_gap_c, max_gap_c, last_gap_c and unit.
  global : cross-city n, mean_gap_c, bias_c, std_gap_c, mae_c, rmse_c,
           min_gap_c, max_gap_c.

bias_c is the systematic offset (mean signed gap in °C) that can later be
subtracted from ``our_peak`` for a bias-corrected forecast. The live
verification path intentionally does NOT apply the correction (see
``_model_quality_tracker._log_peak_verification``); the bias is exposed here so
it can be applied at report time or in a later, separately-reviewed change.

Pure standard-library Python; reads/writes only JSON files next to this script,
so it works identically locally and in CI.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
PEAK_VERIFICATION_LOG = BASE_DIR / "_peak_verification_log.json"
PEAK_DEVIATION_LOG = BASE_DIR / "_peak_deviation_log.json"

# Rounding precision for stored floats (°C / °C-deltas).
ROUND_NDIGITS = 4
# °F gap -> °C gap conversion (gap is a delta, so no -32 offset applies).
F_TO_C_DELTA = 5.0 / 9.0


def _round(value: float) -> float:
    return round(float(value), ROUND_NDIGITS)


def _round1(value: float) -> float:
    """Round native-unit values to 1 decimal (matches the verification log)."""
    return round(float(value), 1)


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def gap_to_c(gap: float, unit: str) -> float:
    """Normalise a native-unit gap to °C."""
    return gap * F_TO_C_DELTA if (unit or "C").upper() == "F" else gap


def build_sample(
    city: str,
    date_str: str,
    our_peak: float | None,
    market_resolved: float | None,
    unit: str = "C",
    gap: float | None = None,
    verdict: str | None = None,
) -> dict | None:
    """Canonical sample builder shared by the merge path and live upsert.

    Returns None when the required (city, date) key or numeric values are
    missing, so callers can simply skip unusable entries.
    """
    if not city or not date_str:
        return None
    if our_peak is None or market_resolved is None:
        return None
    try:
        our_peak_f = float(our_peak)
        market_f = float(market_resolved)
    except (TypeError, ValueError):
        return None

    unit = (unit or "C").upper()
    if unit not in ("C", "F"):
        unit = "C"

    if gap is None:
        gap_f = our_peak_f - market_f
    else:
        try:
            gap_f = float(gap)
        except (TypeError, ValueError):
            gap_f = our_peak_f - market_f
    gap_c = gap_to_c(gap_f, unit)

    return {
        "city": city,
        "date": date_str,
        "our_peak": _round1(our_peak_f),
        "market_resolved": _round1(market_f),
        "unit": unit,
        "gap": _round1(gap_f),
        "gap_c": _round(gap_c),
        "verdict": verdict or "?",
    }


def sample_from_verification_entry(city: str, entry: dict[str, Any]) -> dict | None:
    """Build a sample from a ``_peak_verification_log.json`` verification entry."""
    date_str = entry.get("date")
    if not city or not date_str:
        return None

    our_peak = entry.get("our_peak")
    market_resolved = entry.get("market_resolved")
    gap = entry.get("gap")
    unit = (entry.get("unit") or "C").upper()
    verdict = entry.get("verdict")

    # build_sample derives the gap from our_peak/market_resolved when the
    # entry predates the explicit gap field.
    return build_sample(
        city=city,
        date_str=date_str,
        our_peak=our_peak,
        market_resolved=market_resolved,
        unit=unit,
        gap=gap,
        verdict=verdict,
    )


def load_existing_samples() -> dict:
    """Return {(city, date): sample} from the persistent deviation log."""
    existing: dict = {}
    if PEAK_DEVIATION_LOG.exists():
        try:
            prev = load_json(PEAK_DEVIATION_LOG)
        except (json.JSONDecodeError, OSError):
            prev = {}
        for sample in prev.get("samples", []):
            city = sample.get("city")
            date_str = sample.get("date")
            if city and date_str:
                existing[(city, date_str)] = sample
    return existing


def _sample_std(values: list[float], mean: float | None = None) -> float:
    """Sample standard deviation (ddof=1); 0.0 when n < 2."""
    n = len(values)
    if n < 2:
        return 0.0
    if mean is None:
        mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return math.sqrt(var)


def compute_city_aggregates(samples: list[dict]) -> dict:
    """Recompute per-city aggregates from the full sample history."""
    by_city: dict[str, list[dict]] = defaultdict(list)
    for sample in samples:
        by_city[sample["city"]].append(sample)

    cities: dict = {}
    for city, city_samples in by_city.items():
        ordered = sorted(city_samples, key=lambda s: (s["date"], s["city"]))
        n = len(ordered)
        gaps = [s["gap_c"] for s in ordered]
        mean = sum(gaps) / n
        last = ordered[-1]
        cities[city] = {
            "n": n,
            "mean_gap_c": _round(mean),
            "bias_c": _round(mean),
            "std_gap_c": _round(_sample_std(gaps, mean)),
            "mae_c": _round(sum(abs(g) for g in gaps) / n),
            "rmse_c": _round(math.sqrt(sum(g * g for g in gaps) / n)),
            "min_gap_c": _round(min(gaps)),
            "max_gap_c": _round(max(gaps)),
            "last_gap_c": _round(last["gap_c"]),
            "unit": last["unit"],
        }
    return cities


def compute_global_aggregates(samples: list[dict]) -> dict:
    """Recompute cross-city aggregates from the full sample history."""
    gaps = [s["gap_c"] for s in samples]
    n = len(gaps)
    if n == 0:
        return {
            "n": 0,
            "mean_gap_c": 0.0,
            "bias_c": 0.0,
            "std_gap_c": 0.0,
            "mae_c": 0.0,
            "rmse_c": 0.0,
            "min_gap_c": 0.0,
            "max_gap_c": 0.0,
        }
    mean = sum(gaps) / n
    return {
        "n": n,
        "mean_gap_c": _round(mean),
        "bias_c": _round(mean),
        "std_gap_c": _round(_sample_std(gaps, mean)),
        "mae_c": _round(sum(abs(g) for g in gaps) / n),
        "rmse_c": _round(math.sqrt(sum(g * g for g in gaps) / n)),
        "min_gap_c": _round(min(gaps)),
        "max_gap_c": _round(max(gaps)),
    }


def _recompute_and_write(samples_by_key: dict) -> dict:
    """Sort the samples and write the log with recomputed aggregates."""
    samples = sorted(
        samples_by_key.values(), key=lambda s: (s["date"], s["city"])
    )
    cities = compute_city_aggregates(samples)
    ordered_cities = dict(
        sorted(cities.items(), key=lambda item: (-item[1]["n"], item[0].lower()))
    )
    global_stats = compute_global_aggregates(samples)

    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "samples": samples,
        "cities": ordered_cities,
        "global": global_stats,
    }

    with open(PEAK_DEVIATION_LOG, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    return output


def upsert_peak_sample(
    city: str,
    date_str: str,
    our_peak: float,
    market_resolved: float,
    unit: str = "C",
    gap: float | None = None,
    verdict: str | None = None,
) -> bool:
    """Upsert one (city, date) sample and recompute aggregates.

    Lightweight, exception-safe helper used by the daily-close verification
    path. Never raises — returns False on any failure so peak verification is
    not affected by tracker issues.
    """
    try:
        sample = build_sample(
            city=city,
            date_str=date_str,
            our_peak=our_peak,
            market_resolved=market_resolved,
            unit=unit,
            gap=gap,
            verdict=verdict,
        )
        if sample is None:
            return False
        samples = load_existing_samples()
        samples[(sample["city"], sample["date"])] = sample
        _recompute_and_write(samples)
        return True
    except Exception:
        return False


def get_city_bias(city: str) -> tuple[float | None, int]:
    """Return (bias_c, n) for a city, or (None, 0) when unavailable.

    Provided so a future correction hook can gate on a minimum sample size
    (e.g. n >= 20) without duplicating load logic. Not applied live yet.
    """
    try:
        if not PEAK_DEVIATION_LOG.exists():
            return None, 0
        data = load_json(PEAK_DEVIATION_LOG)
        city_stats = data.get("cities", {}).get(city)
        if not city_stats:
            return None, 0
        return float(city_stats.get("bias_c")), int(city_stats.get("n", 0))
    except Exception:
        return None, 0


def main() -> None:
    """Merge the latest verification entries into the persistent history."""
    samples_by_key = load_existing_samples()

    added_count = 0
    skipped_count = 0
    merged_count = 0
    if PEAK_VERIFICATION_LOG.exists():
        try:
            pv_data = load_json(PEAK_VERIFICATION_LOG)
        except (json.JSONDecodeError, OSError):
            pv_data = {}
        verifications = pv_data.get("verifications", {})
        for city_key, entry in verifications.items():
            sample = sample_from_verification_entry(city_key, entry)
            if sample is None:
                skipped_count += 1
                continue
            key = (sample["city"], sample["date"])
            if key not in samples_by_key:
                added_count += 1
            samples_by_key[key] = sample
            merged_count += 1

    output = _recompute_and_write(samples_by_key)
    global_stats = output["global"]
    cities = output["cities"]

    print(
        "Peak deviation stats — "
        f"{len(output['samples'])} samples, {len(cities)} cities "
        f"(added this run: {added_count}, merged: {merged_count}, "
        f"skipped: {skipped_count})"
    )
    print(
        f"global: n={global_stats['n']} "
        f"mean/bias={global_stats['bias_c']:+.3f}°C "
        f"std={global_stats['std_gap_c']:.3f}°C "
        f"mae={global_stats['mae_c']:.3f}°C "
        f"rmse={global_stats['rmse_c']:.3f}°C"
    )
    print(f"{'city':<24} {'n':>4} {'bias_c':>10} {'std':>8} {'mae':>8} {'rmse':>8}")
    for city, stats in cities.items():
        print(
            f"{city:<24} {stats['n']:>4} {stats['bias_c']:>+10.3f} "
            f"{stats['std_gap_c']:>8.3f} {stats['mae_c']:>8.3f} "
            f"{stats['rmse_c']:>8.3f}"
        )


if __name__ == "__main__":
    main()
