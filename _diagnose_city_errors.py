#!/usr/bin/env python3
"""
City error diagnostics — WHY do so many cities miss?

Reads the persistent peak-deviation sample history
(``_peak_deviation_log.json``) and computes, for every city:

  * n            — number of resolved (city, day) samples
  * mean_error   — mean of (predicted_peak − resolved), normalised to °C
  * median_error — median signed error
  * mae_c        — mean absolute error
  * rmse_c       — root mean square error
  * std_error    — standard deviation of the signed error (ddof=1)
  * bias_sign    — sign of the mean error
  * bias_mag     — absolute mean error

Each city is then classified by **|bias| / within-city σ**, not by σ alone:

  * STATION_OFFSET — |mean error| / std_error > 1 and |mean| ≥ 0.75 °C. The
                     offset is larger than the scatter around it, i.e. a fixed
                     station/provenance offset rather than noise. This is the
                     correct reading for Taipei (bias −1.746, sd 0.977 → 1.79σ)
                     and Munich, which the old σ-only rule mislabelled
                     HIGH_VARIANCE.
  * SIGN_INVERTED  — a large offset in the WRONG direction (mean error
                     ≥ +0.75 °C) in a population where 90 % of cities run cold.
                     Los Angeles (+1.23 °C) is the case: the model is not merely
                     offset, it leans the other way. Checked BEFORE the
                     |bias|/σ test on purpose — the sign is the finding there,
                     and LA's scatter (σ = 1.34) would otherwise hide it.
  * NOISE          — |mean| / std_error ≤ 1: scatter dominates the offset.
  * OK             — small mean error and small scatter.

`bias_over_sigma` is written next to the classification so the rule is auditable.

Outputs ``_city_error_diagnostics.json``, ``_city_error_diagnostics.csv`` and
prints a ranked table of the worst cities with the likely cause.
"""

from __future__ import annotations

import csv
import json
import math
import os
import statistics
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
QUALITY_LOG = BASE_DIR / "_model_quality_log.json"
RESOLVED_LOG = BASE_DIR / "_resolved_markets_log.json"
DIAG_JSON = BASE_DIR / "_city_error_diagnostics.json"
DIAG_CSV = BASE_DIR / "_city_error_diagnostics.csv"

# Classification thresholds (°C) and the |bias|/σ decision boundary.
STATION_BIAS_MEAN = float(os.environ.get("DIAG_STATION_BIAS_MEAN", "0.75"))
STATION_BIAS_STD = float(os.environ.get("DIAG_STATION_BIAS_STD", "0.75"))
HIGH_VAR_STD = float(os.environ.get("DIAG_HIGH_VAR_STD", "1.0"))
BIAS_TO_SIGMA_OFFSET = float(os.environ.get("DIAG_BIAS_TO_SIGMA", "1.0"))
# A positive offset larger than this is treated as a sign-inverted station
# relation, not a tolerable wobble (Los Angeles: +1.241 °C).
SIGN_INVERTED_MEAN = float(os.environ.get("DIAG_SIGN_INVERTED_MEAN", "0.75"))


def _load_peak_samples() -> list[dict]:
    if not PEAK_DEVIATION_LOG.exists():
        return []
    try:
        data = json.loads(PEAK_DEVIATION_LOG.read_text(encoding="utf-8"))
        return data.get("samples", []) or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_resolution_c() -> dict[tuple[str, str], tuple[float, str]]:
    """Point-market resolution in °C plus unit, keyed by (city, date)."""
    out: dict[tuple[str, str], tuple[float, str]] = {}
    if not RESOLVED_LOG.exists():
        return out
    try:
        data = json.loads(RESOLVED_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    for key, entry in (data.get("markets") or {}).items():
        if "||" not in key:
            continue
        if entry.get("type", "point") != "point" or entry.get("value") is None:
            continue
        try:
            value = float(entry["value"])
        except (TypeError, ValueError):
            continue
        unit = (entry.get("unit") or "C").upper()
        if unit == "F":
            value = (value - 32.0) * 5.0 / 9.0
        city, date_str = key.split("||", 1)
        out[(city, date_str)] = (value, unit)
    return out


def _load_quality_samples() -> list[dict]:
    """Forecast-vs-resolution error per city-date, the series the design used.

    One sample per (city, target_date): the lead-0 prediction with the latest
    ``last_updated``. ``gap_c`` is ``bma_mean − resolution`` in °C — the same
    convention as §3.2 of the design (negative = the model runs cold), which is
    what makes Taipei's −1.75 bias comparable to its 0.98 within-city sd.
    """
    if not QUALITY_LOG.exists():
        return []
    try:
        data = json.loads(QUALITY_LOG.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    resolutions = _load_resolution_c()
    if not resolutions:
        return []

    best: dict[tuple[str, str], tuple[str, float, str]] = {}
    for run in data.get("runs", []) or []:
        run_date = str(run.get("run_date") or "")
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            try:
                if int(pdata.get("_lead_days", 0) or 0) != 0:
                    continue
            except (TypeError, ValueError):
                continue
            target = str(pdata.get("_target_date") or run_date or "")
            if not target:
                continue
            resolved = resolutions.get((city, target))
            if resolved is None:
                continue
            try:
                mean_c = float(pdata.get("bma_mean"))
            except (TypeError, ValueError):
                continue
            stamp = str(pdata.get("last_updated") or run_date or "")
            key = (city, target)
            previous = best.get(key)
            if previous is None or stamp > previous[0]:
                best[key] = (stamp, mean_c - resolved[0], resolved[1])

    return [
        {"city": city, "gap_c": gap, "unit": unit}
        for (city, _date), (_stamp, gap, unit) in best.items()
    ]


def _load_samples() -> list[dict]:
    """Bias samples: the forecast-vs-resolution series, peak gaps as fallback."""
    samples = _load_quality_samples()
    if samples:
        return samples
    return _load_peak_samples()


def _bias_over_sigma(mean_error: float, std_error: float) -> float:
    """|mean error| / within-city sd. Infinite when sd is 0 and bias is not."""
    if std_error > 0:
        return abs(mean_error) / std_error
    return float("inf") if mean_error else 0.0


def _classify(mean_error: float, std_error: float) -> str:
    ratio = _bias_over_sigma(mean_error, std_error)
    large_bias = abs(mean_error) >= STATION_BIAS_MEAN
    if mean_error >= SIGN_INVERTED_MEAN:
        return "SIGN_INVERTED"
    if large_bias and ratio > BIAS_TO_SIGMA_OFFSET:
        return "STATION_OFFSET"
    if large_bias or std_error >= HIGH_VAR_STD:
        return "NOISE"
    return "OK"


def diagnose() -> list[dict]:
    samples = _load_samples()
    by_city: dict[str, list[float]] = defaultdict(list)
    units: dict[str, str] = {}
    for s in samples:
        city = s.get("city", "?")
        try:
            by_city[city].append(float(s["gap_c"]))
        except (KeyError, TypeError, ValueError):
            continue
        if city not in units:
            units[city] = s.get("unit", "C")

    rows: list[dict] = []
    for city, gaps in by_city.items():
        n = len(gaps)
        mean_error = sum(gaps) / n
        median_error = statistics.median(gaps)
        mae = sum(abs(g) for g in gaps) / n
        rmse = math.sqrt(sum(g * g for g in gaps) / n)
        std_error = statistics.stdev(gaps) if n >= 2 else 0.0
        cls = _classify(mean_error, std_error)
        ratio = _bias_over_sigma(mean_error, std_error)

        rows.append({
            "city": city,
            "n": n,
            "mean_error_c": round(mean_error, 3),
            "median_error_c": round(median_error, 3),
            "mae_c": round(mae, 3),
            "rmse_c": round(rmse, 3),
            "std_error_c": round(std_error, 3),
            "bias_sign": "positive" if mean_error > 0 else ("negative" if mean_error < 0 else "zero"),
            "bias_magnitude_c": round(abs(mean_error), 3),
            "bias_over_sigma": (round(ratio, 3) if ratio != float("inf") else None),
            "unit": units.get(city, "C"),
            "classification": cls,
            "cause": _cause_text(cls, mean_error, std_error),
        })

    rows.sort(key=lambda r: (-abs(r["mean_error_c"]), -r["std_error_c"], r["city"]))
    return rows


def _cause_text(cls: str, mean_error: float, std_error: float) -> str:
    ratio = _bias_over_sigma(mean_error, std_error)
    if cls == "STATION_OFFSET":
        direction = "over-predicts" if mean_error > 0 else "under-predicts"
        return (
            f"consistent {direction} offset (mean {mean_error:+.2f}°C, "
            f"std {std_error:.2f}°C → {ratio:.2f}σ) — likely different "
            f"station/source vs Polymarket"
        )
    if cls == "SIGN_INVERTED":
        return (
            f"offset in the WRONG direction (mean {mean_error:+.2f}°C, "
            f"std {std_error:.2f}°C → {ratio:.2f}σ) — the point is warmer than "
            f"the market resolves; not a station offset but a sign error"
        )
    if cls == "NOISE":
        return (
            f"scatter dominates the offset (|mean|/σ = {ratio:.2f}, "
            f"std {std_error:.2f}°C) — model/noise error rather than a fixed "
            f"station offset"
        )
    return "within tolerance — no systematic miss cause detected"


def write_outputs(rows: list[dict]) -> None:
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "rule": "|bias| / within-city sigma: > 1 => STATION_OFFSET, positive => SIGN_INVERTED, else NOISE",
        "thresholds": {
            "station_bias_mean_c": STATION_BIAS_MEAN,
            "station_bias_std_c": STATION_BIAS_STD,
            "high_variance_std_c": HIGH_VAR_STD,
            "bias_over_sigma_offset": BIAS_TO_SIGMA_OFFSET,
            "sign_inverted_mean_c": SIGN_INVERTED_MEAN,
        },
        "summary": {
            "n_cities": len(rows),
            "n_station_offset": sum(1 for r in rows if r["classification"] == "STATION_OFFSET"),
            "n_sign_inverted": sum(1 for r in rows if r["classification"] == "SIGN_INVERTED"),
            "n_noise": sum(1 for r in rows if r["classification"] == "NOISE"),
            "n_ok": sum(1 for r in rows if r["classification"] == "OK"),
        },
        "cities": rows,
    }
    DIAG_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = [
        "city", "n", "mean_error_c", "median_error_c", "mae_c", "rmse_c",
        "std_error_c", "bias_sign", "bias_magnitude_c", "bias_over_sigma", "unit",
        "classification", "cause",
    ]
    with open(DIAG_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main() -> int:
    rows = diagnose()
    write_outputs(rows)

    n_station = sum(1 for r in rows if r["classification"] == "STATION_OFFSET")
    n_inv = sum(1 for r in rows if r["classification"] == "SIGN_INVERTED")
    n_noise = sum(1 for r in rows if r["classification"] == "NOISE")
    n_ok = sum(1 for r in rows if r["classification"] == "OK")

    print("═" * 92)
    print("  CITY ERROR DIAGNOSTICS — predicted peak vs Polymarket resolution")
    print("═" * 92)
    print(f"  cities={len(rows)}  STATION_OFFSET={n_station}  SIGN_INVERTED={n_inv}  NOISE={n_noise}  OK={n_ok}")
    print(f"  rule: |bias|/σ > {BIAS_TO_SIGMA_OFFSET} and |mean| ≥ {STATION_BIAS_MEAN}°C ⇒ STATION_OFFSET; "
          f"positive ⇒ SIGN_INVERTED; else NOISE (std ≥ {HIGH_VAR_STD}°C or |mean| ≥ {STATION_BIAS_MEAN}°C)")
    print("")
    print(f"  {'city':<24} {'n':>3} {'mean':>7} {'med':>6} {'MAE':>6} {'RMSE':>6} {'std':>6}  cause")
    print("  " + "-" * 88)
    for r in rows[:25]:
        print(
            f"  {r['city']:<24} {r['n']:>3} {r['mean_error_c']:>+7.2f} {r['median_error_c']:>+6.2f} "
            f"{r['mae_c']:>6.2f} {r['rmse_c']:>6.2f} {r['std_error_c']:>6.2f}  "
            f"[{r['classification']}] {r['cause']}"
        )
    print("")
    print(f"  Wrote {DIAG_JSON.name} and {DIAG_CSV.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
