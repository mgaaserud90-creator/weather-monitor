#!/usr/bin/env python3
"""
Polymarket Resolved Market Fetcher — Tag-Slug Discovery
========================================================
Fetches resolved temperature markets from the Polymarket Gamma API using the
``daily-temperature`` tag-slug pagination path instead of one slug query per
hard-coded city.

API Pattern (primary):
  GET /events?tag_slug=daily-temperature&limit=100&offset=...&order=endDate&ascending=false
  -> all daily-temperature events (active + closed), most recent first
  -> filter to "Highest temperature" events whose slug matches a target date
  -> each event has nested ``markets[]``
  -> find the winning bucket: outcomePrices=["1","0"] with outcome "Yes"
  -> groupItemTitle = winning temperature bracket

This captures ALL of a day's markets (including NYC and any city outside the
default 51-city list). The legacy per-city slug lookup is kept only as a
fallback for any (city, date) pair the tag-slug path did not find.

Output: _resolved_markets_log.json + CSV (unchanged format)
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

_SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = _SCRIPT_DIR / "_resolved_markets_log.json"
CSV_FILE = _SCRIPT_DIR / "_resolved_markets.csv"
DEFAULTS_FILE = _SCRIPT_DIR / "weather_monitor_defaults.json"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}
MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Polymarket uses different slugs than the naive "city name with dashes" for
# a few cities. Keys are the normalized (lower, no punctuation) city token
# produced by :func:`city_to_slug` before dashes are applied.
CITY_SLUG_ALIASES = {
    "new york": "nyc",
    "new york city": "nyc",
    "nyc": "nyc",
}

# One shared opener for the whole run (single HTTP handler reused across
# requests) — the stdlib analogue of a shared client session.
_OPENER = build_opener(HTTPSHandler(), ProxyHandler())


def f_to_c(f: float) -> float:
    return round((f - 32) * 5 / 9, 1)


def date_to_slug(date_str: str) -> str:
    """Convert '2026-08-11' to 'august-11-2026'."""
    parts = date_str.split("-")
    months = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]
    month = months[int(parts[1]) - 1]
    day = str(int(parts[2]))
    year = parts[0]
    return f"{month}-{day}-{year}"


def _normalize_city(city: str) -> str:
    """Strip country code and parenthetical, then lowercase."""
    base = city.split(",")[0].strip().lower()
    base = re.sub(r'\s*\(.*?\)\s*', '', base)
    return base.strip()


def city_to_slug(city: str) -> str:
    """Convert 'Hong Kong, HK' / 'Seoul (Incheon), KR' to Polymarket slug.

    Handles known aliases (e.g. "New York" -> "nyc").
    """
    base = _normalize_city(city)
    if base in CITY_SLUG_ALIASES:
        return CITY_SLUG_ALIASES[base]
    return base.replace(" ", "-")


def slug_date_tokens(date_str: str) -> list[str]:
    """Year-inclusive slug date tokens, e.g. on-august-12-2026 / on-aug-12-2026."""
    y, m, d = date_str.split("-")
    months = ["january", "february", "march", "april", "may", "june",
              "july", "august", "september", "october", "november", "december"]
    full = months[int(m) - 1]
    abbr = full[:3]
    day = str(int(d))
    return [f"on-{full}-{day}-{y}", f"on-{abbr}-{day}-{y}"]


def slug_to_iso(slug: str) -> str | None:
    """Extract ISO date from a slug like '...-on-august-12-2026'."""
    m = re.search(r'on-([a-z]+)-(\d{1,2})-(\d{4})', (slug or "").lower())
    if not m:
        return None
    month = MONTH_MAP.get(m.group(1)) or MONTH_ABBR.get(m.group(1))
    if month is None:
        return None
    return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"


def event_date_from_slug(slug: str, tokens_by_date: dict[str, list[str]]) -> str | None:
    """Return the target date an event slug belongs to, or None."""
    s = (slug or "").lower()
    for date_str, tokens in tokens_by_date.items():
        if any(t in s for t in tokens):
            return date_str
    return None


def extract_city_slug_from_event(slug: str) -> str | None:
    """Extract the city token from 'highest-temperature-in-{city}-on-...'."""
    m = re.match(
        r'^(?:highest|lowest)-temperature-in-(.+?)-on-[a-z]+-\d{1,2}(?:-\d{4})?$',
        (slug or "").strip().lower(),
    )
    return m.group(1) if m else None


def display_city_from_slug(city_slug: str) -> str:
    """Fallback display name for a city slug not present in the defaults."""
    if city_slug == "nyc":
        return "New York, US"
    parts = (city_slug or "").split("-")
    return " ".join(p.title() for p in parts if p)


def fetch_json(url: str) -> dict | list | None:
    """Fetch JSON with retry using the shared opener."""
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "WeatherMonitor/2.0"})
            with _OPENER.open(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception:
            if attempt == 2:
                return None
            time.sleep(1.5 ** attempt)
    return None


def parse_temp_bracket(bracket: str, city: str, question: str = "") -> dict | None:
    """Classify a resolved temperature market and return a storage payload.

    Point markets (bucket or single value) carry a native-unit numeric value.
    US °F buckets stay in °F (numeric midpoint + original bucket label) and are
    never converted to °C for comparison/display.
    Threshold markets ("X°C or higher" / "or above" / "at least" / "≥") carry
    only a lower bound and are excluded from point-value gap comparisons.
    Returns None when the market cannot be classified.
    """
    bracket = (bracket or "").strip()
    question = (question or "").strip()
    text = f"{question} {bracket}".strip()

    # Threshold markets: "X°C or higher" / "at least" / "or above" / "≥" / "or below".
    th = re.search(
        r'(\d+(?:\.\d+)?)\s*°\s*([CF])\s*'
        r'(?:or\s+higher|or\s+above|at\s+least|or\s+more|or\s+below|or\s+lower|≥|≤)',
        text, re.IGNORECASE,
    )
    if th:
        val = float(th.group(1))
        unit = th.group(2).upper()
        display = (bracket or question).strip() or None
        payload: dict = {
            "type": "threshold",
            "unit": unit,
            "bucket": display,
            "temp_display": display,
        }
        if unit == "F":
            payload["lower_bound_f"] = val
            payload["lower_bound_c"] = round((val - 32) * 5 / 9, 1)
        else:
            payload["lower_bound_c"] = val
        return payload

    # °F bucket markets: "86-87°F" or a single "92°F".
    if "°F" in bracket:
        m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°\s*F', bracket, re.IGNORECASE)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            midpoint = (lo + hi) / 2.0
            return {
                "type": "point", "unit": "F",
                "temp_f": midpoint, "value": midpoint,
                "temp_c": round((midpoint - 32) * 5 / 9, 1),  # legacy field for old consumers
                "bucket": m.group(0).strip(), "temp_display": bracket,
            }
        m = re.search(r'(\d+(?:\.\d+)?)\s*°\s*F', bracket, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return {
                "type": "point", "unit": "F",
                "temp_f": val, "value": val,
                "temp_c": round((val - 32) * 5 / 9, 1),
                "bucket": m.group(0).strip(), "temp_display": bracket,
            }

    # °C point markets.
    if "°C" in bracket:
        m = re.search(r'(\d+(?:\.\d+)?)\s*°\s*C', bracket, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            return {
                "type": "point", "unit": "C",
                "temp_c": val, "value": val,
                "bucket": m.group(0).strip(), "temp_display": bracket,
            }
    return None


def load_default_cities() -> list[dict]:
    """Load city list from weather_monitor_defaults.json."""
    if DEFAULTS_FILE.exists():
        try:
            data = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
            return data.get("default_locations", [])
        except Exception:
            pass
    return []


def build_slug_to_name(cities: list[dict]) -> dict[str, str]:
    """Map Polymarket city slug -> canonical default name (e.g. nyc -> New York, US)."""
    mapping: dict[str, str] = {}
    for loc in cities:
        name = loc.get("name", "")
        if not name:
            continue
        mapping[city_to_slug(name)] = name
    return mapping


def _coerce_list(value: Any) -> list:
    """Return value as a native list (Gamma sometimes JSON-encodes arrays)."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (json.JSONDecodeError, ValueError):
            return []
    return []


def find_winning_payload(market: dict, city: str) -> dict | None:
    """Return a storage payload for a market's winning bucket, if resolved."""
    outcomes = _coerce_list(market.get("outcomes"))
    prices = _coerce_list(market.get("outcomePrices"))
    if not outcomes or len(outcomes) != len(prices):
        return None

    group_title = market.get("groupItemTitle", "")
    for i, p in enumerate(prices):
        try:
            price_val = float(p)
        except (ValueError, TypeError):
            continue
        if price_val >= 0.999 and outcomes[i].lower() == "yes":
            return parse_temp_bracket(group_title, city, market.get("question", ""))
    return None


def _days_before(iso_date: str, days: int) -> str:
    """Return an ISO date ``days`` before the given ISO date."""
    return (date.fromisoformat(iso_date) - timedelta(days=days)).isoformat()


def fetch_events_by_tag(target_dates: list[str], page_size: int = 100,
                        max_pages: int = 12) -> list[dict]:
    """Paginate the daily-temperature tag-slug endpoint (most recent first).

    The Gamma /events endpoint paginates with an offset over a sort, which can
    return the same event on more than one page. We de-duplicate by event id
    and keep fetching until we are clearly past the oldest target date (with a
    safety margin) so overlapping pages never drop a target-date event.
    """
    oldest = min(target_dates)
    stop_after = _days_before(oldest, 5)
    events: list[dict] = []
    seen_ids: set[str] = set()

    for page in range(max_pages):
        offset = page * page_size
        url = (
            f"{GAMMA_EVENTS_URL}?tag_slug=daily-temperature&limit={page_size}"
            f"&offset={offset}&order=endDate&ascending=false"
        )
        data = fetch_json(url)
        if not data:
            break
        batch = data if isinstance(data, list) else []
        if not batch:
            break

        for event in batch:
            event_id = str(event.get("id", "") or "")
            if event_id and event_id in seen_ids:
                continue
            if event_id:
                seen_ids.add(event_id)
            events.append(event)

        # Stop once we've passed the oldest target date (with margin) so we
        # don't walk the whole history. Events are sorted by endDate desc.
        last_iso = slug_to_iso((batch[-1].get("slug", "") or ""))
        if last_iso is not None and last_iso < stop_after:
            break
        if len(batch) < page_size:
            break

    return events


def fetch_resolved_for_dates(target_dates: list[str]) -> dict[tuple[str, str], dict]:
    """Discover and parse all resolved temperature markets for the given dates."""
    resolved: dict[tuple[str, str], dict] = {}
    cities = load_default_cities()
    slug_to_name = build_slug_to_name(cities)
    tokens_by_date = {d: slug_date_tokens(d) for d in target_dates}

    print(f"Discovering resolved markets via tag_slug=daily-temperature for "
          f"{len(target_dates)} dates ({target_dates[0]} .. {target_dates[-1]})...")

    events = fetch_events_by_tag(target_dates)
    print(f"  Fetched {len(events)} daily-temperature events.")

    found_keys: set[tuple[str, str]] = set()

    for event in events:
        slug = event.get("slug", "") or ""
        title = event.get("title", "") or ""
        combined = f"{slug} {title}".lower()
        if "highest-temperature" not in combined and "highest temperature" not in combined:
            continue

        date_str = event_date_from_slug(slug, tokens_by_date)
        if date_str is None:
            continue

        city_slug = extract_city_slug_from_event(slug)
        name = slug_to_name.get(city_slug) if city_slug else None
        if not name:
            name = display_city_from_slug(city_slug or "")

        for market in event.get("markets", []):
            payload = find_winning_payload(market, name)
            if payload is None:
                continue

            key = (name, date_str)
            if key in resolved:
                break

            entry = {
                "city": name,
                "date": date_str,
                "source": "Polymarket Gamma API",
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            }
            entry.update(payload)
            resolved[key] = entry
            found_keys.add(key)

            if payload.get("type") == "threshold":
                print(f"  {name:<25s} -> THRESHOLD {payload.get('lower_bound_c')}°C+ "
                      f"(excluded from gaps)")
            elif payload.get("unit") == "F":
                print(f"  {name:<25s} -> {payload.get('bucket')} "
                      f"({payload.get('value'):.1f}°F)")
            else:
                print(f"  {name:<25s} -> {payload.get('bucket')} "
                      f"({payload.get('value'):.1f}°C)")
            break

    # Fallback: per-city slug lookup for any (city, date) not found via tag-slug.
    missing: list[tuple[str, str, str]] = []
    for date_str in target_dates:
        date_slug = date_to_slug(date_str)
        for loc in cities:
            name = loc.get("name", "")
            if not name or (name, date_str) in found_keys:
                continue
            missing.append((name, date_str, date_slug))

    if missing:
        print(f"  Fallback: {len(missing)} city/date gap(s) via per-city slug lookup...")
        for name, date_str, date_slug in missing:
            slug = f"highest-temperature-in-{city_to_slug(name)}-on-{date_slug}"
            data = fetch_json(f"{GAMMA_EVENTS_URL}?slug={slug}")
            if not data:
                continue
            events = data if isinstance(data, list) else [data]
            for event in events:
                if not event:
                    continue
                for market in event.get("markets", []):
                    payload = find_winning_payload(market, name)
                    if payload is None:
                        continue
                    key = (name, date_str)
                    if key not in resolved:
                        entry = {
                            "city": name,
                            "date": date_str,
                            "source": "Polymarket Gamma API",
                            "fetched_at": datetime.now(timezone.utc).isoformat(),
                        }
                        entry.update(payload)
                        resolved[key] = entry
                        found_keys.add(key)
                        print(f"  {name:<25s} -> {payload.get('bucket')} (fallback)")
                    break
            time.sleep(0.2)

    return resolved


def load_existing() -> dict:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_updated": "", "markets": {}}


def save_results(markets_map: dict, target_date: str) -> None:
    """Save to JSON and CSV."""
    existing = load_existing()
    existing_markets = existing.get("markets", {})

    # Merge: new data overwrites existing for same key
    for key, val in markets_map.items():
        city, date = key
        str_key = f"{city}||{date}"
        existing_markets[str_key] = val

    log = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(existing_markets),
        "latest_date": target_date,
        "markets": existing_markets,
    }
    LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")

    # CSV
    with open(CSV_FILE, "w", encoding="utf-8", newline="") as f:
        import csv
        w = csv.writer(f)
        w.writerow(["city", "date", "unit", "type", "bucket", "value",
                    "temp_c", "temp_f", "temp_display", "source"])
        for str_key, data in sorted(existing_markets.items()):
            w.writerow([data.get("city", ""), data.get("date", ""),
                        data.get("unit", ""), data.get("type", ""),
                        data.get("bucket", ""), data.get("value", ""),
                        data.get("temp_c", ""), data.get("temp_f", ""),
                        data.get("temp_display", ""),
                        data.get("source", "")])

    print(f"\nSaved {len(markets_map)} new + {len(existing_markets) - len(markets_map)} existing = {len(existing_markets)} total")
    print(f"JSON: {LOG_FILE}")
    print(f"CSV:  {CSV_FILE}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Fetch resolved Polymarket temperature markets")
    parser.add_argument("--date", default="2026-08-11", help="Target date (YYYY-MM-DD)")
    parser.add_argument("--dates", nargs="+", help="Multiple dates")
    args = parser.parse_args()

    print("=" * 50)
    print("  POLYMARKET RESOLVED MARKET FETCHER")
    print("=" * 50)

    dates = args.dates if args.dates else [args.date]
    all_resolved = fetch_resolved_for_dates(dates)

    for d in dates:
        n = sum(1 for (_city, dd) in all_resolved if dd == d)
        print(f"  {d}: {n} resolved markets")

    if all_resolved:
        save_results(all_resolved, dates[-1])
    else:
        print("\n  No resolved markets found for any date.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
