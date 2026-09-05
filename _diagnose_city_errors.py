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

Each city is then classified as:

  * STATION_BIAS  — large, consistent offset (|mean error| ≥ 0.75 °C AND
                    std ≤ 0.75 °C). The signature of our station/location
                    differing from Polymarket's resolution source.
  * HIGH_VARIANCE  — noisy errors (std ≥ 1.0 °C or |mean| < threshold but
                    scatter large). Model/noise problem, not a fixed offset.
  * OK             — small mean error and small scatter.

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
DIAG_JSON = BASE_DIR / "_city_error_diagnostics.json"
DIAG_CSV = BASE_DIR / "_city_error_diagnostics.csv"

# Classification thresholds (°C).
STATION_BIAS_MEAN = float(os.environ.get("DIAG_STATION_BIAS_MEAN", "0.75"))
STATION_BIAS_STD = float(os.environ.get("DIAG_STATION_BIAS_STD", "0.75"))
HIGH_VAR_STD = float(os.environ.get("DIAG_HIGH_VAR_STD", "1.0"))


def _load_samples() -> list[dict]:
    if not PEAK_DEVIATION_LOG.exists():
        return []
    try:
        data = json.loads(PEAK_DEVIATION_LOG.read_text(encoding="utf-8"))
        return data.get("samples", []) or []
    except (json.JSONDecodeError, OSError):
        return []


def _classify(mean_error: float, std_error: float) -> str:
    if abs(mean_error) >= STATION_BIAS_MEAN and std_error <= STATION_BIAS_STD:
        return "STATION_BIAS"
    if std_error >= HIGH_VAR_STD or abs(mean_error) >= STATION_BIAS_MEAN:
        return "HIGH_VARIANCE"
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
            "unit": units.get(city, "C"),
            "classification": cls,
            "cause": _cause_text(cls, mean_error, std_error),
        })

    rows.sort(key=lambda r: (-abs(r["mean_error_c"]), -r["std_error_c"], r["city"]))
    return rows


def _cause_text(cls: str, mean_error: float, std_error: float) -> str:
    if cls == "STATION_BIAS":
        direction = "over-predicts" if mean_error > 0 else "under-predicts"
        return (
            f"consistent {direction} offset (mean {mean_error:+.2f}°C, "
            f"std {std_error:.2f}°C) — likely different station/source vs Polymarket"
        )
    if cls == "HIGH_VARIANCE":
        return (
            f"high scatter (std {std_error:.2f}°C) — model/noise error rather "
            f"than a fixed station offset"
        )
    return "within tolerance — no systematic miss cause detected"


def write_outputs(rows: list[dict]) -> None:
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "thresholds": {
            "station_bias_mean_c": STATION_BIAS_MEAN,
            "station_bias_std_c": STATION_BIAS_STD,
            "high_variance_std_c": HIGH_VAR_STD,
        },
        "summary": {
            "n_cities": len(rows),
            "n_station_bias": sum(1 for r in rows if r["classification"] == "STATION_BIAS"),
            "n_high_variance": sum(1 for r in rows if r["classification"] == "HIGH_VARIANCE"),
            "n_ok": sum(1 for r in rows if r["classification"] == "OK"),
        },
        "cities": rows,
    }
    DIAG_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    fieldnames = [
        "city", "n", "mean_error_c", "median_error_c", "mae_c", "rmse_c",
        "std_error_c", "bias_sign", "bias_magnitude_c", "unit",
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

    n_station = sum(1 for r in rows if r["classification"] == "STATION_BIAS")
    n_var = sum(1 for r in rows if r["classification"] == "HIGH_VARIANCE")
    n_ok = sum(1 for r in rows if r["classification"] == "OK")

    print("═" * 92)
    print("  CITY ERROR DIAGNOSTICS — predicted peak vs Polymarket resolution")
    print("═" * 92)
    print(f"  cities={len(rows)}  STATION_BIAS={n_station}  HIGH_VARIANCE={n_var}  OK={n_ok}")
    print(f"  thresholds: station_bias |mean|≥{STATION_BIAS_MEAN}°C & std≤{STATION_BIAS_STD}°C; high_var std≥{HIGH_VAR_STD}°C")
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
