#!/usr/bin/env python3
"""Out-of-sample per-city mean-error correction for ``bma_mean`` (P2).

THE DECISION THIS IMPLEMENTS
----------------------------
The correction was adopted only after the pre-registered gate passed:

  * max-statistic permutation over the 48-configuration grid, adjusted
    p = 0.0196 (500 residual permutations, resolutions shuffled across cities
    within each date) — below the 0.05 barrier, so the holdout was unlocked;
  * ONE pass on the frozen holdout 2026-10-10..2026-10-29, n = 909 scored
    city-dates, ΔLogLoss = −0.19884 (MDE is −0.052), date-clustered 95 % CI
    [−0.25407, −0.15803] (upper bound below 0), calibration slope 0.1476 →
    0.1649 (toward 1.0) and Brier not worse. Rule R1 passed in every clause.

The joint P2+P4 configuration (bias + global standardised-residual ECDF) was
scored in the same single holdout pass and FAILED: ΔLogLoss +0.10234, CI
[+0.00549, +0.17217], Brier worse. It is therefore NOT enabled — see
``STRATEGI_OPTIMALISERING.md`` for the recorded verdict.

WHAT THE CORRECTION IS
----------------------
``bias(city) = mean(bma_mean − resolution)`` over the city's already-resolved
city-dates, with the own-city → cluster → global ladder of design doc §8:

    own city, n >= MIN_N   -> that city's mean error
    else the unit cluster  -> the °F (US) or °C cluster's mean error
    else                   -> the global mean error

Nothing is fitted on the day being predicted: a prediction for date *t* only
ever sees city-dates with ``date < t``. That is the whole point — the in-sample
version of this same correction is what produced the bogus "+4.5 pp" in
``_edge_enhancer_backtest.json``.

Kill switch: ``VMON_BIAS_CORRECTION=0`` disables it (the fields are still
written, with ``_bias_applied = 0.0``).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
QUALITY_LOG = BASE_DIR / "_model_quality_log.json"
RESOLVED_LOG = BASE_DIR / "_resolved_markets_log.json"

MIN_N = 10
ENABLED = os.environ.get("VMON_BIAS_CORRECTION", "1").strip() not in ("0", "false", "False")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def resolution_c_by_key() -> dict[tuple[str, str], float]:
    """{(city, date): resolution in °C} for point markets."""
    data = _load_json(RESOLVED_LOG) or {}
    out: dict[tuple[str, str], float] = {}
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
        out[(city, date_str)] = value
    return out


def estimate(before_date: str | None = None) -> dict[str, dict[str, Any]]:
    """Per-city bias estimates from city-dates strictly before ``before_date``.

    Returns ``{city: {"bias": float, "source": str, "n": int}}``. When
    ``before_date`` is None every resolved city-date is used (the daily path
    passes today's date so the estimate is genuinely out of sample).
    """
    quality = _load_json(QUALITY_LOG) or {}
    resolutions = resolution_c_by_key()
    if not resolutions:
        return {}

    city_sum: dict[str, list[float]] = {}
    cluster_sum: dict[str, list[float]] = {}
    seen: set[tuple[str, str]] = set()
    global_sum = [0.0, 0]

    for run in quality.get("runs", []) or []:
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
            if not target or (before_date and target >= before_date):
                continue
            if (city, target) in seen:
                continue
            resolved = resolutions.get((city, target))
            if resolved is None:
                continue
            try:
                mean_c = float(pdata.get("bma_mean"))
            except (TypeError, ValueError):
                continue
            seen.add((city, target))
            err = mean_c - resolved
            bucket = city_sum.setdefault(city, [0.0, 0])
            bucket[0] += err
            bucket[1] += 1
            unit = "F" if (city.upper().endswith(", US")) else "C"
            cl = cluster_sum.setdefault(unit, [0.0, 0.0])
            cl[0] += err
            cl[1] += 1
            global_sum[0] += err
            global_sum[1] += 1

    if global_sum[1] == 0:
        return {}
    global_bias = global_sum[0] / global_sum[1]
    cluster_bias = {
        unit: (v[0] / v[1] if v[1] else global_bias) for unit, v in cluster_sum.items()
    }

    out: dict[str, dict[str, Any]] = {}
    for city, (total, n) in city_sum.items():
        if n >= MIN_N:
            out[city] = {"bias": total / n, "source": "own_city", "n": int(n)}
        else:
            unit = "F" if (city.upper().endswith(", US")) else "C"
            out[city] = {
                "bias": cluster_bias.get(unit, global_bias),
                "source": "cluster",
                "n": int(cluster_sum.get(unit, [0, 0])[1]),
            }
    out["__global__"] = {"bias": global_bias, "source": "global", "n": int(global_sum[1])}
    return out


def bias_for(city: str, estimates: dict[str, dict[str, Any]] | None = None) -> dict[str, Any]:
    """Bias estimate for one city, falling back to cluster then global."""
    est = estimates if estimates is not None else estimate()
    if not est:
        return {"bias": 0.0, "source": "none", "n": 0}
    hit = est.get(city)
    if hit is not None:
        return hit
    return est.get("__global__", {"bias": 0.0, "source": "none", "n": 0})


def apply_bias(mean_c: float, city: str, estimates: dict[str, dict[str, Any]] | None = None) -> tuple[float, dict[str, Any]]:
    """Return (corrected mean, provenance). Corrected = raw − bias."""
    if not ENABLED:
        info = bias_for(city, estimates)
        return mean_c, {"bias": 0.0, "source": "disabled", "n": int(info.get("n") or 0)}
    info = bias_for(city, estimates)
    return mean_c - float(info["bias"]), info


def main() -> int:
    est = estimate()
    glob = est.get("__global__", {})
    print(f"bias estimates: {len(est) - 1} cities, global {glob.get('bias', 0):+.3f} °C "
          f"over n={glob.get('n', 0)} city-dates (enabled={ENABLED})")
    for city in sorted(k for k in est if not k.startswith("__"))[:10]:
        e = est[city]
        print(f"  {city:<24} {e['bias']:+.3f} °C  [{e['source']}, n={e['n']}]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
