#!/usr/bin/env python3
"""
Polymarket Resolved Market Fetcher — Slug-Based
================================================
Fetches resolved temperature markets from Polymarket Gamma API using
individual event slug queries. This is the CORRECT method — the same
approach verified to work for 15+ markets on August 11, 2026.

API Pattern:
  GET /events?slug=highest-temperature-in-{city}-on-{date}
  Find market where outcomePrices=["1.0","0.0"] and outcomes=["Yes","No"]
  groupItemTitle = winning temperature bracket

Output: _resolved_markets_log.json + CSV
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

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

US_CITIES = {"New York", "Los Angeles", "Chicago", "Houston", "Dallas",
             "Austin", "Miami", "Atlanta", "Denver", "Seattle",
             "San Francisco", "Boston", "Washington", "Portland",
             "Las Vegas", "Detroit", "Baltimore", "Orlando",
             "Minneapolis", "Tampa", "St. Louis", "Phoenix",
             "San Diego", "San Antonio", "Philadelphia"}


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


def city_to_slug(city: str) -> str:
    """Convert 'Hong Kong, HK' or 'Seoul (Incheon), KR' to slug format."""
    base = city.split(",")[0].strip().lower()
    base = re.sub(r'\s*\(.*?\)\s*', '', base)
    return base.replace(" ", "-")


def fetch_json(url: str) -> dict | list | None:
    """Fetch JSON with retry."""
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "WeatherMonitor/2.0"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
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


def fetch_resolved_for_date(target_date: str) -> dict[tuple[str, str], dict]:
    """Fetch all resolved temperature markets for a specific date."""
    resolved: dict[tuple[str, str], dict] = {}
    date_slug = date_to_slug(target_date)
    cities = load_default_cities()
    print(f"Fetching resolved markets for {target_date} ({len(cities)} cities)...")

    for loc in cities:
        name = loc.get("name", "")
        if not name:
            continue
        city_slug = city_to_slug(name)
        slug = f"highest-temperature-in-{city_slug}-on-{date_slug}"
        url = f"{GAMMA_EVENTS_URL}?slug={slug}"

        data = fetch_json(url)
        if not data:
            continue

        events = data if isinstance(data, list) else [data]
        for event in events:
            if not event:
                continue
            for market in event.get("markets", []):
                outcomes_raw = market.get("outcomes", "")
                prices_raw = market.get("outcomePrices", "")
                group_title = market.get("groupItemTitle", "")

                try:
                    outcomes = json.loads(outcomes_raw) if isinstance(outcomes_raw, str) else outcomes_raw
                    prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
                except Exception:
                    continue

                if not (isinstance(outcomes, list) and isinstance(prices, list)):
                    continue

                for i, p in enumerate(prices):
                    try:
                        price_val = float(p)
                    except (ValueError, TypeError):
                        continue
                    if price_val >= 0.999 and i < len(outcomes) and outcomes[i].lower() == "yes":
                        payload = parse_temp_bracket(group_title, name, market.get("question", ""))
                        if payload is not None:
                            key = (name, target_date)
                            if key not in resolved:
                                entry = {
                                    "city": name,
                                    "date": target_date,
                                    "source": "Polymarket Gamma API",
                                    "fetched_at": datetime.now(timezone.utc).isoformat(),
                                }
                                entry.update(payload)
                                resolved[key] = entry
                                if payload.get("type") == "threshold":
                                    bound = payload.get("lower_bound_c")
                                    print(f"  {name:<25s} -> THRESHOLD {bound}°C+ (excluded from gaps)")
                                elif payload.get("unit") == "F":
                                    print(f"  {name:<25s} -> {group_title} ({payload.get('value'):.1f}°F)")
                                else:
                                    print(f"  {name:<25s} -> {group_title} ({payload.get('value'):.1f}°C)")
                        break

        time.sleep(0.5)

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
    all_resolved = {}

    for d in dates:
        resolved = fetch_resolved_for_date(d)
        all_resolved.update(resolved)
        print(f"  {d}: {len(resolved)} resolved markets")

    if all_resolved:
        save_results(all_resolved, dates[-1])
    else:
        print("\n  No resolved markets found for any date.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
