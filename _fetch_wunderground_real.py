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
import html
import json
import re
import sys
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


def _local_yesterday(tz_name: str, now: datetime | None = None) -> str:
    """Local calendar date of yesterday in the city's timezone."""
    now = now or datetime.now(timezone.utc)
    try:
        from zoneinfo import ZoneInfo

        z = ZoneInfo(tz_name)
        local = now.replace(tzinfo=ZoneInfo("UTC")).astimezone(z)
    except Exception:
        local = now
    return (local.date() - timedelta(days=1)).isoformat()


def load_existing() -> dict[str, Any]:
    if OUT_JSON.exists():
        try:
            return json.loads(OUT_JSON.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"last_updated": "", "source": "Wunderground Daily Observations", "cities": {}}


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
    args = parser.parse_args()

    cities = load_cities()
    if args.cities:
        wanted = {c.strip() for c in args.cities}
        cities = [c for c in cities if c["name"] in wanted]

    print(f"Wunderground real-resolution fetcher — {len(cities)} cities")

    state = load_existing()
    cities_db = state.setdefault("cities", {})

    # Determine the dates to fetch.
    if args.dates:
        dates = args.dates
    elif args.date:
        dates = [args.date]
    else:
        dates = sorted({_local_yesterday(c.get("tz", "UTC")) for c in cities})

    total_ok = 0
    total_null = 0
    total_new = 0

    with httpx.Client(
        headers=HEADERS, follow_redirects=True, timeout=args.timeout
    ) as client:
        for loc in cities:
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

            for date_str in dates:
                if date_str in existing and not args.refresh:
                    continue
                row = fetch_one(client, loc, date_str)
                if row["wu_real_max"] is not None:
                    total_ok += 1
                else:
                    total_null += 1
                total_new += 1
                existing[date_str] = row
                flag = "OK" if row["wu_real_max"] is not None else "-- "
                print(
                    f"  {flag} {name:<22} {row['station']:<5} {date_str} "
                    f"max={row['wu_real_max']} n={row['wu_n_readings']}"
                )

            city_db["rows"] = sorted(existing.values(), key=lambda r: r["date"])

    state["last_updated"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    OUT_JSON.write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # Flat CSV.
    with OUT_CSV.open("w", encoding="utf-8", newline="") as fh:
        fh.write(
            "city,station,date,status,wu_real_max,unit,wu_time_of_max,wu_n_readings\n"
        )
        for city_name, cdb in sorted(cities_db.items()):
            for r in cdb.get("rows", []):
                fh.write(
                    f"{city_name},{r.get('station','')},{r['date']},{r.get('status',0)},"
                    f"{r.get('wu_real_max') if r.get('wu_real_max') is not None else ''},"
                    f"{r.get('unit') or ''},{r.get('wu_time_of_max') or ''},"
                    f"{r.get('wu_n_readings',0)}\n"
                )

    print(f"\nFetched {total_new} rows — OK={total_ok} null={total_null}")
    print(f"Wrote {OUT_JSON.name} and {OUT_CSV.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
