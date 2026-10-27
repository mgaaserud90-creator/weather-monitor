#!/usr/bin/env python3
"""Rebuild the peak-vs-resolution history from the FULL quality log.

The live pipeline only writes ``strategies.<x>.actual_peak`` (the observed
Open-Meteo archive daily max, i.e. the "faktisk peak") for city-days that were
confirmed/resolved while the runner was active.  That leaves most historical
(city, date) rows without an ``actual_peak``, so the peak-vs-resolution stats
end up with n=1..8 instead of one sample per resolved city-day.

This script backfills every missing ``actual_peak`` from the Open-Meteo archive
API (one batched request per city, covering only the still-missing dates) and
then regenerates the two derived logs from the complete history:

  * ``_peak_verification_log.json``  (via _populate_peak_verify.py)
  * ``_peak_deviation_log.json``     (via _peak_deviation_stats.py)

Incremental: only (city, date) pairs that still lack an ``actual_peak`` are
fetched/updated.  A run with nothing new performs no network requests and no
14 MB quality-log rewrite, so the cost is O(new) rather than O(all).  Pass
``--force`` to regenerate the derived logs even when nothing changed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date
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


def _backfill_actual_peaks(log_data: dict) -> tuple[int, int, int, int]:
    """Fill missing ``actual_peak`` for (city, target_date) rows that lack it.

    Only *new* pairs are processed: dates whose ``sigma.actual_peak`` is already
    present are skipped before any network request, so the archive is queried
    for the missing date span only (per city) instead of the full history.

    Returns ``(cities_considered, cities_fetched, city_days_backfilled, rows_updated)``.
    """
    runs = log_data.get("runs", []) or []

    city_meta: dict[str, tuple] = {}
    city_date_refs: dict[tuple[str, str], list[dict]] = {}
    missing_dates: dict[str, set[str]] = {}

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
            sigma = (pdata.get("strategies") or {}).get("sigma") or {}
            if sigma.get("actual_peak") is None:
                missing_dates.setdefault(city, set()).add(target)

    cities_considered = len(city_meta)
    cities_fetched = 0
    city_days_backfilled = 0
    rows_updated = 0

    for city in sorted(missing_dates):
        lat, lon, tz = city_meta.get(city, (None, None, "UTC"))
        if lat is None or lon is None:
            continue
        dates = sorted(missing_dates[city])
        # The archive API rejects ranges that reach into the future, so clamp
        # the end date to today and skip cities that have no finalized day yet.
        end = min(dates[-1], date.today().isoformat())
        if end < dates[0]:
            print(f"  ⏭️ {city}: no finalized archive day yet ({dates[0]}..{dates[-1]})")
            continue
        try:
            day_max = _fetch_city_daily_max(lat, lon, tz, dates[0], end)
        except Exception as exc:  # pragma: no cover - network is best-effort
            print(f"  ⚠️ archive fetch failed for {city}: {exc}")
            continue
        cities_fetched += 1

        for target in dates:
            value = day_max.get(target)
            if value is None:
                continue
            rounded = round(value, 1)
            filled = False
            for pdata in city_date_refs.get((city, target), []):
                strategies = pdata.setdefault("strategies", {})
                for sn in ("sigma", "p5", "mean"):
                    strat = strategies.setdefault(sn, {})
                    if strat.get("actual_peak") is None:
                        strat["actual_peak"] = rounded
                        rows_updated += 1
                        filled = True
            if filled:
                city_days_backfilled += 1

    return cities_considered, cities_fetched, city_days_backfilled, rows_updated


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in args

    started = time.perf_counter()
    print("╔══════════════════════════════════════════════════╗")
    print("║   PEAK vs RESOLUTION — INCREMENTAL REBUILD       ║")
    print("╚══════════════════════════════════════════════════╝")

    if not QUALITY_LOG.exists():
        print("  _model_quality_log.json not found — nothing to do.")
        return 1

    log_data = json.loads(QUALITY_LOG.read_text(encoding="utf-8"))
    cities_considered, cities_fetched, city_days, rows = _backfill_actual_peaks(log_data)

    if rows:
        QUALITY_LOG.write_text(
            json.dumps(log_data, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        print(
            f"  archive backfill: {cities_fetched}/{cities_considered} cities fetched, "
            f"{city_days} city-days filled, {rows} strategy rows updated"
        )
    else:
        print(
            f"  archive backfill: nothing new ({cities_considered} cities, 0 missing "
            "city-days) — quality log left untouched"
        )

    # Regenerate the two derived logs only when the quality log actually
    # changed (or on --force). The pipeline runs both as separate steps right
    # after this one, so re-running them here would only duplicate 14 MB reads.
    if rows or force:
        for script in ("_populate_peak_verify.py", "_peak_deviation_stats.py"):
            print(f"  ▶ running {script}")
            result = subprocess.run(
                [sys.executable, str(BASE / script)],
                cwd=str(BASE),
                capture_output=False,
            )
            if result.returncode != 0:
                print(f"  ⚠️ {script} exited with code {result.returncode}")
    else:
        print("  derived logs unchanged — skipping _populate_peak_verify/_peak_deviation_stats")

    print(f"  Rebuild complete in {time.perf_counter() - started:.1f}s.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
