#!/usr/bin/env python3
"""Rebuild the peak-vs-resolution history from the FULL quality log.

The live pipeline only writes ``strategies.<x>.actual_peak`` (the observed
Open-Meteo archive daily max, i.e. the "faktisk peak") for city-days that were
confirmed/resolved while the runner was active.  That leaves most historical
(city, date) rows without an ``actual_peak``, so the peak-vs-resolution stats
end up with n=1..8 instead of one sample per resolved city-day.

This script backfills every missing ``actual_peak`` from the Open-Meteo archive
API (one batched request per city over its full target-date range) and then
regenerates the two derived logs from the complete history:

  * ``_peak_verification_log.json``  (via _populate_peak_verify.py)
  * ``_peak_deviation_log.json``     (via _peak_deviation_stats.py)

Idempotent: existing ``actual_peak`` values are never overwritten, so re-running
only fills in what is still missing.  Pure read/write of JSON files next to this
script; only the archive fetch touches the network.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

BASE = Path(__file__).resolve().parent
QUALITY_LOG = BASE / "_model_quality_log.json"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"

try:
    import httpx  # type: ignore
except ImportError:  # pragma: no cover - fallback for minimal environments
    httpx = None


def _fetch_city_daily_max(
    lat: float, lon: float, tz: str, start: str, end: str
) -> dict[str, float]:
    """Fetch ``temperature_2m_max`` for one city over [start, end] (inclusive).

    Returns ``{date_iso: value}`` for every day the archive returned.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start,
        "end_date": end,
        "daily": "temperature_2m_max",
        "timezone": tz or "UTC",
    }
    if httpx is not None:
        resp = httpx.get(ARCHIVE_URL, params=params, timeout=30.0)
        resp.raise_for_status()
        data = resp.json()
    else:
        import urllib.parse
        import urllib.request

        url = ARCHIVE_URL + "?" + urllib.parse.urlencode(params)
        with urllib.request.urlopen(url, timeout=30) as fh:
            data = json.loads(fh.read().decode("utf-8"))

    daily = data.get("daily", {})
    dates = daily.get("time", [])
    temps = daily.get("temperature_2m_max", [])
    out: dict[str, float] = {}
    for day, temp in zip(dates, temps):
        if day and temp is not None:
            out[day] = float(temp)
    return out


def _backfill_actual_peaks(log_data: dict) -> tuple[int, int, int]:
    """Fill missing ``actual_peak`` for every (city, target_date) row.

    Returns ``(cities_fetched, city_days_backfilled, rows_updated)``.
    """
    runs = log_data.get("runs", []) or []

    city_meta: dict[str, tuple] = {}
    city_date_refs: dict[tuple[str, str], list[dict]] = {}

    for run in runs:
        run_date = run.get("run_date", "")
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            target = pdata.get("_target_date") or run_date
            if not target:
                continue
            if city not in city_meta:
                city_meta[city] = (
                    pdata.get("_lat"),
                    pdata.get("_lon"),
                    pdata.get("_tz", "UTC"),
                )
            city_date_refs.setdefault((city, target), []).append(pdata)

    cities_fetched = 0
    city_days_backfilled = 0
    rows_updated = 0

    for city, (lat, lon, tz) in sorted(city_meta.items()):
        if lat is None or lon is None:
            continue
        dates = sorted({d for (c, d) in city_date_refs if c == city})
        if not dates:
            continue

        try:
            day_max = _fetch_city_daily_max(lat, lon, tz, dates[0], dates[-1])
        except Exception as exc:  # pragma: no cover - network is best-effort
            print(f"  ⚠️ archive fetch failed for {city}: {exc}")
            continue
        cities_fetched += 1

        for target in dates:
            value = day_max.get(target)
            if value is None:
                continue
            rounded = round(value, 1)
            refs = city_date_refs.get((city, target), [])
            any_missing = any(
                (pdata.get("strategies") or {}).get("sigma", {}).get("actual_peak") is None
                for pdata in refs
            )
            if not any_missing:
                continue
            for pdata in refs:
                strategies = pdata.setdefault("strategies", {})
                for sn in ("sigma", "p5", "mean"):
                    strat = strategies.setdefault(sn, {})
                    if strat.get("actual_peak") is None:
                        strat["actual_peak"] = rounded
                        rows_updated += 1
            city_days_backfilled += 1

    return cities_fetched, city_days_backfilled, rows_updated


def main() -> int:
    print("╔══════════════════════════════════════════════════╗")
    print("║   PEAK vs RESOLUTION — FULL-HISTORY REBUILD      ║")
    print("╚══════════════════════════════════════════════════╝")

    if not QUALITY_LOG.exists():
        print("  _model_quality_log.json not found — nothing to do.")
        return 1

    log_data = json.loads(QUALITY_LOG.read_text(encoding="utf-8"))
    cities_fetched, city_days, rows = _backfill_actual_peaks(log_data)
    QUALITY_LOG.write_text(
        json.dumps(log_data, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        f"  archive backfill: {cities_fetched} cities fetched, "
        f"{city_days} city-days filled, {rows} strategy rows updated"
    )

    # Regenerate the two derived logs from the now-complete quality log.
    for script in ("_populate_peak_verify.py", "_peak_deviation_stats.py"):
        print(f"  ▶ running {script}")
        result = subprocess.run(
            [sys.executable, str(BASE / script)],
            cwd=str(BASE),
            capture_output=False,
        )
        if result.returncode != 0:
            print(f"  ⚠️ {script} exited with code {result.returncode}")

    print("  Rebuild complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
