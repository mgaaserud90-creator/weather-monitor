#!/usr/bin/env python3
"""Fetch REAL (finalized) daily max temperatures from Wunderground history pages.

For every city in ``weather_monitor_defaults.json`` that has a known
Wunderground station, this module downloads the server-rendered daily history
page and extracts the finalized "Day High" (whole-degree) temperature plus the
time it occurred and the number of daily observations.

Design goals
------------
* Plain ``httpx`` GET — no API key, no cookies. Wunderground serves the daily
  history as server-rendered HTML (confirmed for LLBG and the full city set).
* Idempotent: already-fetched (city, date) pairs are kept and not re-fetched
  unless ``--refresh`` is passed. Output is merged into the existing files.
* Non-fatal: 404/403, missing stations, "no data recorded" pages and network
  errors are recorded as ``status`` + ``wu_real_max=None`` and never abort the
  run (partial failure is fine).
* Timezone-aware: the "finalized" day for a city is *yesterday in that city's
  local timezone*, because Wunderground pages are keyed by local calendar date.

Outputs
-------
* ``_wunderground_real.json`` — full per-city/per-date record.
* ``_wunderground_real.csv``  — flat table for quick joins.

Usage
-----
    python _fetch_wunderground_real.py                 # yesterday for every city
    python _fetch_wunderground_real.py --days 7        # last 7 local days
    python _fetch_wunderground_real.py --date 2026-09-01
    python _fetch_wunderground_real.py --dates 2026-09-01 2026-09-02
    python _fetch_wunderground_real.py --cities "Tel Aviv, IL" "New York, US"
    python _fetch_wunderground_real.py --refresh
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

BASE = Path(__file__).resolve().parent
DEFAULTS_FILE = BASE / "weather_monitor_defaults.json"
MARKET_PARSER_FILE = BASE / "src" / "strategies" / "weather" / "market_parser.py"
OUT_JSON = BASE / "_wunderground_real.json"
OUT_CSV = BASE / "_wunderground_real.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# ---------------------------------------------------------------------------
# Full city -> Wunderground URL map.
#
# Built from weather_monitor_defaults.json (city name / station / timezone) plus
# the ICAO station values in STATION_METADATA (market_parser.py). For cities
# whose defaults "station" is not a Wunderground station code the ICAO value is
# used instead (see STATION_FIXES below).
#
# Tuple: (region, state, city_slug)
#   * US:      region="us", state=<2-letter state>, slug="new-york-city"
#   * non-US:  region=<2-letter country>, state="", slug="tel-aviv"
# ---------------------------------------------------------------------------
CITY_WU_MAP: dict[str, tuple[str, str, str]] = {
    "Taipei, TW": ("tw", "", "taipei-city"),
    "Hong Kong, HK": ("hk", "", "hong-kong"),
    "Shanghai, CN": ("cn", "", "shanghai"),
    "Seoul (Incheon), KR": ("kr", "", "incheon"),
    "Kuala Lumpur, MY": ("my", "", "kuala-lumpur"),
    "Madrid, ES": ("es", "", "madrid"),
    "Paris, FR": ("fr", "", "paris"),
    "Munich, DE": ("de", "", "munich"),
    "Wellington, NZ": ("nz", "", "wellington"),
    "Shenzhen, CN": ("cn", "", "shenzhen"),
    "Singapore, SG": ("sg", "", "singapore"),
    "Guangzhou, CN": ("cn", "", "guangzhou"),
    "New York, US": ("us", "ny", "new-york-city"),
    "London, UK": ("gb", "", "london"),
    "Milan, IT": ("it", "", "milan"),
    "Los Angeles, US": ("us", "ca", "los-angeles"),
    "Tokyo, JP": ("jp", "", "tokyo"),
    "Helsinki, FI": ("fi", "", "helsinki"),
    "Chongqing, CN": ("cn", "", "chongqing"),
    "Chengdu, CN": ("cn", "", "chengdu"),
    "Wuhan, CN": ("cn", "", "wuhan"),
    "Qingdao, CN": ("cn", "", "qingdao"),
    "Jeddah, SA": ("sa", "", "jeddah"),
    "Istanbul, TR": ("tr", "", "istanbul"),
    "Ankara, TR": ("tr", "", "ankara"),
    "Busan, KR": ("kr", "", "busan"),
    "Dallas, US": ("us", "tx", "dallas"),
    "Houston, US": ("us", "tx", "houston"),
    "Atlanta, US": ("us", "ga", "atlanta"),
    "Lucknow, IN": ("in", "", "lucknow"),
    "Manila, PH": ("ph", "", "manila"),
    "Karachi, PK": ("pk", "", "karachi"),
    "Beijing, CN": ("cn", "", "beijing"),
    "Chicago, US": ("us", "il", "chicago"),
    "Toronto, CA": ("ca", "", "toronto"),
    "Austin, US": ("us", "tx", "austin"),
    "Amsterdam, NL": ("nl", "", "amsterdam"),
    "Warsaw, PL": ("pl", "", "warsaw"),
    "Miami, US": ("us", "fl", "miami"),
    "Cape Town, ZA": ("za", "", "cape-town"),
    "Tel Aviv, IL": ("il", "", "tel-aviv"),
    "Buenos Aires, AR": ("ar", "", "buenos-aires"),
    "Denver, US": ("us", "co", "denver"),
    "San Francisco, US": ("us", "ca", "san-francisco"),
    "Mexico City, MX": ("mx", "", "mexico-city"),
    "Seattle, US": ("us", "wa", "seattle"),
    "Sao Paulo, BR": ("br", "", "sao-paulo"),
    "Zhengzhou, CN": ("cn", "", "zhengzhou"),
    "Moscow, RU": ("ru", "", "moscow"),
    "Panama City, PA": ("pa", "", "panama-city"),
    "Jinan, CN": ("cn", "", "jinan"),
}

# Cities whose defaults "station" field is not a Wunderground station code.
# Hong Kong Observatory's WMO code "HKO" does not exist on Wunderground; the
# ICAO code VHHH (HK International Airport) is used instead.
STATION_FIXES: dict[str, str] = {
    "Hong Kong, HK": "VHHH",
}

# When the primary station has no Wunderground data (or 404s) try these
# alternates in order. Istanbul's new airport (LTFM) has no WU history, but the
# legacy Atatürk airport (LTBA) does.
FALLBACK_STATIONS: dict[str, list[str]] = {
    "Istanbul, TR": ["LTBA"],
}


def _read_station_metadata_icaos() -> dict[str, str]:
    """Extract {city_name_lower: ICAO} from STATION_METADATA without importing
    structlog (so this module stays dependency-light when run standalone)."""
    icaos: dict[str, str] = {}
    if not MARKET_PARSER_FILE.exists():
        return icaos
    try:
        text = MARKET_PARSER_FILE.read_text(encoding="utf-8")
    except OSError:
        return icaos
    # Match entries like: "new york city": {"icao": "KLGA", ...
    pattern = re.compile(r'"([^"]+)":\s*\{[^{}]*?"icao":\s*"([A-Z0-9]{3,4})"')
    for key, icao in pattern.findall(text):
        icaos[key.strip().lower()] = icao
    return icaos


def load_cities() -> list[dict[str, Any]]:
    """Return the default location list with a resolved Wunderground station."""
    raw = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    locations = raw.get("default_locations", [])
    icaos = _read_station_metadata_icaos()
    resolved: list[dict[str, Any]] = []
    for loc in locations:
        name = loc.get("name", "")
        station = loc.get("station", "")
        # Prefer an explicit fix, then the STATION_METADATA ICAO when the
        # defaults value is not a real Wunderground code, then the raw value.
        if name in STATION_FIXES:
            station = STATION_FIXES[name]
        else:
            key = name.split(",")[0].strip().lower().replace(" (incheon)", "")
            meta_icao = icaos.get(key) or icaos.get(key.split()[0])
            if station and re.fullmatch(r"[A-Z]{4}", station) is None and meta_icao:
                # defaults value looks non-ICAO (e.g. "HKO" -> WMO code)
                station = meta_icao
        if not station:
            continue
        loc = dict(loc)
        loc["station"] = station
        loc["fallbacks"] = list(FALLBACK_STATIONS.get(name, []))
        loc["wu"] = CITY_WU_MAP.get(name, (None, "", ""))
        resolved.append(loc)
    return resolved


def build_url(region: str | None, state: str, slug: str, station: str, date_str: str) -> str:
    """Build the full Wunderground daily-history URL.

    US:      https://www.wunderground.com/history/daily/us/ny/new-york-city/KLGA/date/2026-9-4
    non-US:  https://www.wunderground.com/history/daily/il/tel-aviv/LLBG/date/2026-9-4
    """
    y, m, d = date_str.split("-")
    if region == "us" and state:
        return f"https://www.wunderground.com/history/daily/us/{state}/{slug}/{station}/date/{y}-{int(m)}-{int(d)}"
    return f"https://www.wunderground.com/history/daily/{region}/{slug}/{station}/date/{y}-{int(m)}-{int(d)}"


def build_url_pattern(region: str | None, state: str, slug: str, station: str) -> str:
    """Build the URL template (with ``{year}-{month}-{day}`` placeholders)."""
    if region == "us" and state:
        return f"https://www.wunderground.com/history/daily/us/{state}/{slug}/{station}/date/{{year}}-{{month}}-{{day}}"
    return f"https://www.wunderground.com/history/daily/{region}/{slug}/{station}/date/{{year}}-{{month}}-{{day}}"


def station_url(station: str, date_str: str) -> str:
    """Minimal station-only URL — a reliable fallback when the full slug URL 404s."""
    y, m, d = date_str.split("-")
    return f"https://www.wunderground.com/history/daily/{station}/date/{y}-{int(m)}-{int(d)}"


_VALUE_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°|&deg;)?\s*([CF])?")


def _parse_value(text: str) -> tuple[float | None, str | None]:
    """Parse a temperature cell/value like ``32°C``, ``90 °F``, ``--``."""
    t = html.unescape(text or "").replace("\u00a0", " ").strip()
    m = _VALUE_RE.search(t)
    if not m:
        return None, None
    return float(m.group(1)), (m.group(2) or "").upper() or None


def parse_daily_max(page: str) -> dict[str, Any]:
    """Extract Day High + time and observation count from a WU history page."""
    text = html.unescape(page)
    out: dict[str, Any] = {
        "wu_real_max": None,
        "unit": None,
        "wu_time_of_max": None,
        "wu_n_readings": 0,
    }

    # 1) "Day High & Low" summary block.
    high_block = re.search(
        r'high-low-item\s+high.*?<div\s+class="label">High</div>\s*<div\s+class="value">(.*?)</div>'
        r'(?:\s*<div\s+class="meta">(.*?)</div>)?',
        text,
        re.S,
    )
    if high_block:
        value, unit = _parse_value(high_block.group(1))
        out["wu_real_max"] = value
        out["unit"] = unit
        meta = high_block.group(2)
        if meta:
            out["wu_time_of_max"] = html.unescape(re.sub(r"^at\s+", "", meta.strip()))

    # 2) Daily observations table — count readings and derive a fallback max.
    table_max = None
    table_max_time: str | None = None
    table = re.search(r'<table\s+class="observations-table">(.*?)</table>', text, re.S)
    if table:
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S)
        temps: list[float] = []
        times: list[str] = []
        for row in rows:
            if "<th" in row:  # header row
                continue
            cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
            if not cells:
                continue
            time_cell = html.unescape(cells[0]).strip()
            temp_cell = html.unescape(cells[1]).strip() if len(cells) > 1 else ""
            tval, _ = _parse_value(temp_cell)
            if tval is not None:
                temps.append(tval)
                times.append(time_cell)
        out["wu_n_readings"] = len(temps)
        if temps:
            table_max = max(temps)
            table_max_time = times[temps.index(table_max)] if times else None
            # If the summary block was absent, fall back to the table maximum.
            if out["wu_real_max"] is None:
                out["wu_real_max"] = table_max
            # Sanity: never report below the observed table max.
            elif out["wu_real_max"] < table_max:
                out["wu_real_max"] = table_max

    # US pages omit the "at <time>" meta — use the observation table's max time.
    if out["wu_time_of_max"] is None and table_max_time:
        out["wu_time_of_max"] = table_max_time

    return out


def _to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def normalize_unit(value: float | None, raw_unit: str | None, target_unit: str) -> tuple[float | None, str | None]:
    """Convert a parsed value into the city's canonical unit (°F for US, °C otherwise).

    Wunderground serves either °C or °F depending on the requester's region, so
    the reported unit is not reliable — normalize against the station's home unit.
    """
    if value is None:
        return None, None
    ru = (raw_unit or "").upper() or None
    tu = target_unit.upper()
    if ru and ru != tu:
        value = _to_f(value) if tu == "F" else _to_c(value)
    # Wunderground publishes whole degrees; keep the number clean.
    return round(value), tu


def fetch_one(
    client: httpx.Client, loc: dict[str, Any], date_str: str
) -> dict[str, Any]:
    """Fetch a single (city, date) record, trying primary then fallback stations."""
    name = loc["name"]
    region, state, slug = loc["wu"]
    target_unit = "F" if region == "us" else "C"
    stations = [loc["station"]] + list(loc.get("fallbacks", []))
    attempts: list[str] = []

    for station in stations:
        if not station:
            continue
        urls: list[str] = []
        if region and slug:
            urls.append(build_url(region, state, slug, station, date_str))
        urls.append(station_url(station, date_str))

        for url in urls:
            if url in attempts:
                continue
            attempts.append(url)
            try:
                resp = client.get(url)
                status = resp.status_code
                if status in (404, 403):
                    continue
                if status != 200:
                    continue
                parsed = parse_daily_max(resp.text)
                # A 200 with no observations means "no data recorded" for that
                # station/date — try the next candidate station.
                if parsed["wu_n_readings"] == 0 and parsed["wu_real_max"] is None:
                    continue
                real_max, unit = normalize_unit(
                    parsed["wu_real_max"], parsed["unit"], target_unit
                )
                row = {
                    "date": date_str,
                    "status": status,
                    "station": station,
                    "station_primary": loc["station"],
                    "wu_real_max": real_max,
                    "unit": unit,
                    "wu_time_of_max": parsed["wu_time_of_max"],
                    "wu_n_readings": parsed["wu_n_readings"],
                    "url": url,
                }
                return row
            except (httpx.HTTPError, OSError, ValueError):
                continue

    # Unavailable: record null, non-fatal.
    return {
        "date": date_str,
        "status": 0,
        "station": loc["station"],
        "station_primary": loc["station"],
        "wu_real_max": None,
        "unit": None,
        "wu_time_of_max": None,
        "wu_n_readings": 0,
        "url": "",
    }


def _local_day_offset(
    tz_name: str, days_back: int = 1, now: datetime | None = None
) -> str:
    """Local calendar date ``days_back`` days ago in the city's timezone."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        z = ZoneInfo(tz_name)
        local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(z)
    except Exception:
        local = now
    return (local.date() - timedelta(days=max(1, days_back))).isoformat()


def _local_yesterday(tz_name: str, now: datetime | None = None) -> str:
    """Local calendar date of yesterday in the city's timezone."""
    return _local_day_offset(tz_name, 1, now)


def _prev_succeeded(prev: dict[str, Any]) -> bool:
    """True only when a stored row actually carries a real reading.

    The unavailable path in :func:`fetch_one` writes ``status: 0`` with
    ``wu_real_max: None``. Treating such a row as "already fetched" is what
    made a failed day permanent, so skip-on-existing is now gated on this.
    """
    if not prev:
        return False
    if prev.get("wu_real_max") is None:
        return False
    try:
        status = int(prev.get("status") or 0)
    except (TypeError, ValueError):
        return False
    return status == 200


def load_existing() -> dict[str, Any]:
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_updated": "", "source": "Wunderground Daily Observations", "cities": {}}


# ---------------------------------------------------------------------------
# P0.5 — WU validity gate
# ---------------------------------------------------------------------------
# Measured on 2026-10-29: 28 of the 56 WU dates carry a contaminating error
# (daily mean −1.6 … −3.4 °C, sd 3.2–3.7 °C, driven by the US °F rows), while
# the other 28 have mean |bias| ≈ 0.20 °C and sd ≈ 0.47 °C. Consuming the
# contaminated dates as physical truth silently corrupts any station analysis,
# so a date is declared valid only when
#
#     mean|WU − resolution| < 0.6 °C   AND   sd(WU − resolution) < 0.9 °C
#
# and the flag is persisted per (city, date) as ``_wu_valid``. Nothing
# downstream may treat an invalid date as truth.
WU_VALID_MAX_ABS_BIAS_C = 0.6
WU_VALID_MAX_SD_C = 0.9
RESOLVED_LOG = BASE / "_resolved_markets_log.json"


def _resolution_c_by_city_date() -> dict[tuple[str, str], float]:
    """Point-market resolutions in °C, keyed by (city, date)."""
    out: dict[tuple[str, str], float] = {}
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
        if (entry.get("unit") or "C").upper() == "F":
            value = _to_c(value)
        city, date_str = key.split("||", 1)
        out[(city, date_str)] = value
    return out


def _row_value_c(row: dict[str, Any]) -> float | None:
    """A WU row's reading normalised to °C (°F rows are converted)."""
    value = row.get("wu_real_max")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if (row.get("unit") or "C").upper() == "F":
        return _to_c(value)
    return value


def apply_validity_gate(state: dict[str, Any]) -> dict[str, Any]:
    """Flag every WU row with ``_wu_valid`` and return the per-date summary.

    The gate operates on the median-free quantities the design measured:
    ``mean|Δ|`` and ``sd(Δ)`` across all cities that have both a WU reading and
    a Polymarket point resolution on that date.
    """
    resolutions = _resolution_c_by_city_date()
    deltas_by_date: dict[str, list[float]] = {}
    for city, cdb in (state.get("cities") or {}).items():
        for row in cdb.get("rows", []) or []:
            date_str = row.get("date")
            value_c = _row_value_c(row)
            if not date_str or value_c is None:
                continue
            resolved = resolutions.get((city, date_str))
            if resolved is None:
                continue
            deltas_by_date.setdefault(date_str, []).append(value_c - resolved)

    per_date: dict[str, dict[str, Any]] = {}
    for date_str, deltas in deltas_by_date.items():
        n = len(deltas)
        if n == 0:
            continue
        mean_abs = sum(abs(d) for d in deltas) / n
        if n >= 2:
            mean_d = sum(deltas) / n
            sd = (sum((d - mean_d) ** 2 for d in deltas) / (n - 1)) ** 0.5
        else:
            mean_d = deltas[0]
            sd = 0.0
        valid = (mean_abs < WU_VALID_MAX_ABS_BIAS_C) and (sd < WU_VALID_MAX_SD_C)
        per_date[date_str] = {
            "n": n,
            "mean_delta_c": round(mean_d, 4),
            "mean_abs_delta_c": round(mean_abs, 4),
            "sd_delta_c": round(sd, 4),
            "valid": bool(valid),
        }

    flagged_dates = sorted(d for d, v in per_date.items() if not v["valid"])

    # Persist the flag on every row (one row = one city-day = one truth value).
    for cdb in (state.get("cities") or {}).values():
        for row in cdb.get("rows", []) or []:
            date_str = row.get("date")
            info = per_date.get(date_str)
            row["_wu_valid"] = bool(info["valid"]) if info else None

    return {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rule": {
            "mean_abs_delta_c_lt": WU_VALID_MAX_ABS_BIAS_C,
            "sd_delta_c_lt": WU_VALID_MAX_SD_C,
        },
        "n_dates": len(per_date),
        "n_valid_dates": sum(1 for v in per_date.values() if v["valid"]),
        "n_flagged_dates": len(flagged_dates),
        "flagged_dates": flagged_dates,
        "per_date": dict(sorted(per_date.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default=None, help="Single target date (YYYY-MM-DD)")
    parser.add_argument("--dates", nargs="+", default=None, help="Multiple dates (YYYY-MM-DD)")
    parser.add_argument(
        "--days",
        type=int,
        default=1,
        help="Fetch the last N local days per city (default: 1 = yesterday only)",
    )
    parser.add_argument(
        "--cities", nargs="+", default=None, help="Restrict to these city names"
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Re-fetch dates already present"
    )
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-request timeout")
    parser.add_argument(
        "--sleep",
        type=float,
        default=0.0,
        help="Seconds to sleep between cities (politeness throttle for bulk backfills)",
    )
    args = parser.parse_args()

    cities = load_cities()
    if args.cities:
        wanted = {c.strip() for c in args.cities}
        cities = [c for c in cities if c["name"] in wanted]

    print(f"Wunderground real-resolution fetcher — {len(cities)} cities")

    state = load_existing()
    cities_db = state.setdefault("cities", {})

    # Determine the dates to fetch.
    #
    # `--dates` / `--date` give one explicit list shared by every city. Without
    # them we emit "local yesterday" per city (the historic default) plus, when
    # `--days N` was passed, the N-1 local days before it. `--days` used to be
    # declared and never read, so the documented `--days 7` backfill was a
    # silent no-op; it is honoured now.
    days = max(1, int(args.days or 1))
    explicit_dates: list[str] | None = None
    if args.dates:
        explicit_dates = sorted({d.strip() for d in args.dates if d and d.strip()})
    elif args.date:
        explicit_dates = [args.date.strip()]
    if explicit_dates is not None:
        dates = explicit_dates
    else:
        dates = sorted({_local_yesterday(c.get("tz", "UTC")) for c in cities})

    total_ok = 0
    total_null = 0
    total_new = 0
    total_kept = 0
    total_skipped = 0
    per_date_ok: dict[str, int] = {}
    per_date_attempt: dict[str, int] = {}

    with httpx.Client(
        headers=HEADERS, follow_redirects=True, timeout=args.timeout
    ) as client:
        for idx, loc in enumerate(cities):
            name = loc["name"]
            station = loc["station"]
            city_db = cities_db.setdefault(
                name,
                {
                    "station": station,
                    "url_pattern": build_url_pattern(
                        loc["wu"][0], loc["wu"][1], loc["wu"][2], station
                    ),
                    "rows": [],
                },
            )
            existing = {(r.get("date")): r for r in city_db.get("rows", [])}

            if explicit_dates is not None or days == 1:
                city_dates = dates
            else:
                # Last N local days for this city: local yesterday back to
                # local yesterday-(N-1). Fixed dates are a single shared list,
                # but a rolling window is timezone-dependent per city.
                city_dates = sorted(
                    {
                        _local_day_offset(loc.get("tz", "UTC"), i)
                        for i in range(1, days + 1)
                    }
                )

            for date_str in city_dates:
                prev = existing.get(date_str)
                # D2: skip only when the previous attempt actually SUCCEEDED.
                # A date that a past run recorded as null/status 0 (the
                # unavailable path in fetch_one) is retried, otherwise the hole
                # in the ground truth becomes permanent.
                if prev is not None and _prev_succeeded(prev) and not args.refresh:
                    total_skipped += 1
                    continue
                if prev is not None:
                    print(
                        f"  retry {name:<22} {date_str} "
                        f"(previous status={prev.get('status')} "
                        f"wu_real_max={prev.get('wu_real_max')})"
                    )
                row = fetch_one(client, loc, date_str)
                # Additive merge only: never downgrade a good reading to null.
                if (
                    prev is not None
                    and prev.get("wu_real_max") is not None
                    and row.get("wu_real_max") is None
                ):
                    total_kept += 1
                    print(
                        f"  keep  {name:<22} {date_str} "
                        f"(existing max={prev.get('wu_real_max')} retained)"
                    )
                    existing[date_str] = prev
                    continue
                total_new += 1
                per_date_attempt[date_str] = per_date_attempt.get(date_str, 0) + 1
                if row["wu_real_max"] is not None:
                    total_ok += 1
                    per_date_ok[date_str] = per_date_ok.get(date_str, 0) + 1
                else:
                    total_null += 1
                existing[date_str] = row
                flag = "OK" if row["wu_real_max"] is not None else "-- "
                print(
                    f"  {flag} {name:<22} {row['station']:<5} {date_str} "
                    f"max={row['wu_real_max']} n={row['wu_n_readings']}"
                )

            city_db["rows"] = sorted(existing.values(), key=lambda r: r["date"])

            if args.sleep and args.sleep > 0 and idx < len(cities) - 1:
                time.sleep(args.sleep)  # politeness throttle for bulk backfills

    # P0.5 — flag WU dates that cannot be used as physical truth.
    validity = apply_validity_gate(state)
    state["validity"] = validity
    print(
        f"\nWU validity gate: {validity['n_valid_dates']} valid / "
        f"{validity['n_flagged_dates']} flagged of {validity['n_dates']} dates "
        f"(rule: mean|Δ| < {WU_VALID_MAX_ABS_BIAS_C} °C and sd < {WU_VALID_MAX_SD_C} °C)"
    )
    if validity["flagged_dates"]:
        print(f"  flagged: {' '.join(validity['flagged_dates'])}")

    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_JSON.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Flat CSV.
    #
    # D4: written with `csv.writer`, not an f-string. Every city key contains
    # ", " (e.g. "Amsterdam, NL"), so unquoted concatenation emitted nine
    # fields under an eight-field header and every downstream DictReader/Import-Csv
    # shifted its columns. Column order and header names are unchanged; only
    # quoting is added. lineterminator="\n" keeps the historic LF endings.
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, lineterminator="\n")
        writer.writerow(
            [
                "city",
                "station",
                "date",
                "status",
                "wu_real_max",
                "unit",
                "wu_time_of_max",
                "wu_n_readings",
            ]
        )
        for city_name, cdb in sorted(cities_db.items()):
            for r in cdb.get("rows", []):
                writer.writerow(
                    [
                        city_name,
                        r.get("station", ""),
                        r["date"],
                        r.get("status", 0),
                        r.get("wu_real_max")
                        if r.get("wu_real_max") is not None
                        else "",
                        r.get("unit") or "",
                        r.get("wu_time_of_max") or "",
                        r.get("wu_n_readings", 0),
                    ]
                )

    print(
        f"\nFetched {total_new} rows — OK={total_ok} null={total_null} "
        f"kept={total_kept} already_ok={total_skipped}"
    )
    if per_date_attempt:
        print("Per-date success summary:")
        for date_str in sorted(per_date_attempt):
            print(
                f"  {date_str}: {per_date_ok.get(date_str, 0)}"
                f"/{per_date_attempt[date_str]} cities succeeded"
            )
    print(f"Wrote {OUT_JSON.name} and {OUT_CSV.name}")

    # D5: a whole-run total failure must be a red step, not a silent exit 0
    # that still writes an artifact full of nulls.
    if total_new > 0 and total_ok == 0:
        print(
            f"::error::Wunderground fetch produced 0 successes across "
            f"{total_new} attempted (city, date) pairs over "
            f"{len(per_date_attempt)} date(s); {total_null} rows were null."
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
