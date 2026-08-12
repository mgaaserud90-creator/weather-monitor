#!/usr/bin/env python3
"""
Polymarket Resolved Market Collector
====================================
Fetches ALL resolved temperature markets from Polymarket Gamma API,
not just active ones. Extracts city, date, and resolved temperature.

Stores in _resolved_markets_log.json with date-matched keys for
comparison against our archive peaks.

Uses Gamma /events with closed=true + /markets to find resolved markets,
then extracts outcomes with price > 0.99 (YES) or price < 0.01 (NO).
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

_SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = _SCRIPT_DIR / "_resolved_markets_log.json"

# Pollymarket Gamma API
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

# US cities for Fahrenheit conversion
US_CITIES = ["New York", "Los Angeles", "Chicago", "Houston", "Phoenix",
             "Philadelphia", "San Antonio", "San Diego", "Dallas", "Austin",
             "Miami", "Atlanta", "Denver", "Seattle", "San Francisco",
             "Boston", "Washington", "Portland", "Las Vegas", "Detroit",
             "Baltimore", "Orlando", "Minneapolis", "Tampa", "St. Louis"]


def fetch_json(url: str, params: dict | None = None) -> dict | list:
    """Fetch JSON from URL with retry."""
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "WeatherMonitor/2.0"})
            with urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as e:
            if attempt == 2:
                print(f"  Failed to fetch {url}: {e}")
                return {}
            time.sleep(2 ** attempt)
    return {}


def extract_date(question: str) -> str | None:
    """Extract target date from Polymarket question text."""
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2})(?:,\s*(\d{4}))?',
        question, re.IGNORECASE)
    if m:
        month = MONTH_MAP.get(m.group(1).lower(), 1)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else datetime.now(timezone.utc).year
        return f"{year:04d}-{month:02d}-{day:02d}"
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', question)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def extract_city(title: str) -> str | None:
    """Extract city name from market title like 'Highest temperature in X on...'"""
    m = re.search(r'Highest temperature in (.+?) on', title)
    if m:
        return m.group(1).strip()
    m = re.search(r'in (.+?) on', title)
    if m:
        return m.group(1).strip()
    return None


def f_to_c(f: float) -> float:
    """Fahrenheit to Celsius."""
    return round((f - 32) * 5 / 9, 1)


def collect_resolved(limit: int = 500, offset: int = 0) -> dict[tuple[str, str], dict]:
    """Collect all resolved temperature markets from Gamma API.

    Returns: {(city, date_iso): {temp_c, temp_f, question, resolution_source}}
    """
    resolved: dict[tuple[str, str], dict] = {}

    # Strategy 1: Query Gamma /markets for temperature markets with closed=true
    print("Fetching closed temperature markets from Gamma /markets...")
    for off in range(0, limit, 100):
        params = {
            "closed": "true",
            "tag": "temperature",  # temperature tag
            "limit": 100,
            "offset": off,
        }
        data = fetch_json(GAMMA_MARKETS_URL, params)
        markets = data if isinstance(data, list) else data.get("markets", data.get("data", []))
        if not markets:
            break
        _process_markets(markets, resolved)
        time.sleep(0.5)

    # Strategy 2: Query Gamma /events for "highest temperature" with closed=true
    print("Fetching closed events from Gamma /events...")
    for off in range(0, limit, 100):
        params = {
            "closed": "true",
            "tag": "temperature",
            "limit": 100,
            "offset": off,
        }
        data = fetch_json(GAMMA_EVENTS_URL, params)
        events = data if isinstance(data, list) else data.get("events", data.get("data", []))
        if not events:
            break
        for event in events:
            title = event.get("title", "")
            if "temperature" not in title.lower() and "highest" not in title.lower():
                continue
            city = extract_city(title)
            if not city:
                continue
            # For each event, fetch its markets
            slug = event.get("slug", "")
            if slug:
                markets_data = fetch_json(f"{GAMMA_MARKETS_URL}?event_slug={slug}&limit=50")
                mkts = markets_data if isinstance(markets_data, list) else markets_data.get("markets", [])
                _process_markets(mkts, resolved)
                time.sleep(0.3)
        time.sleep(0.5)

    return resolved


def _process_markets(markets: list[dict], resolved: dict) -> None:
    """Process a list of market dicts, extracting resolved outcomes."""
    for m in markets:
        question = m.get("question", m.get("title", ""))
        city = m.get("city") or extract_city(question)
        if not city:
            continue
        if "temperature" not in question.lower() and "highest" not in question.lower():
            continue

        date_str = extract_date(question)
        if not date_str:
            continue

        outcomes = m.get("outcomes", m.get("clobTokenIds", []))
        if isinstance(outcomes, list) and outcomes and isinstance(outcomes[0], str):
            # clobTokenIds format — skip, need different parsing
            continue

        for o in (outcomes if isinstance(outcomes, list) else []):
            price = o.get("price", 0)
            label = o.get("label", o.get("outcome", ""))
            if price > 0.99 and label.lower() == "yes":
                match = re.search(r'(\d+)[°\s]*[FC]', question)
                if not match:
                    match = re.search(r'(\d+)[°\s]*[FC]', label)
                if match:
                    temp_val = int(match.group(1))
                    # Check if Fahrenheit
                    if any(us in city for us in US_CITIES) or "°F" in question or "F" in label[-2:]:
                        temp_c = f_to_c(temp_val)
                    else:
                        temp_c = float(temp_val)
                    key = (city, date_str)
                    if key not in resolved:
                        resolved[key] = {
                            "temp_c": temp_c, "temp_display": temp_val,
                            "question": question, "date": date_str,
                        }
                break


def load_existing() -> dict:
    """Load existing resolved markets log."""
    if LOG_FILE.exists():
        try:
            raw = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            return raw.get("markets", {})
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def save_log(markets: dict) -> None:
    """Save resolved markets log."""
    # Convert tuple keys to string keys for JSON
    serializable = {}
    for (city, date_str), data in sorted(markets.items()):
        key = f"{city}||{date_str}"
        serializable[key] = data

    log = {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_markets": len(markets),
        "markets": serializable,
    }
    LOG_FILE.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved {len(markets)} resolved markets to {LOG_FILE}")


def main() -> int:
    print("=" * 50)
    print("  POLYMARKET RESOLVED MARKET COLLECTOR")
    print("=" * 50)
    print()

    existing = load_existing()
    print(f"Existing resolved markets in log: {len(existing)}")

    new_resolved = collect_resolved()
    print(f"\nNewly fetched resolved markets: {len(new_resolved)}")

    # Merge: new data overwrites existing for same keys
    merged = dict(existing)
    merged.update(new_resolved)

    print(f"Total after merge: {len(merged)}")

    # Print summary
    for (city, date_str), data in sorted(merged.items()):
        print(f"  {city:<25s} {date_str}  → {data['temp_c']:.1f}°C")

    save_log(merged)

    # Also generate simple CSV for easy data analysis
    csv_path = _SCRIPT_DIR / "_resolved_markets.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("city,date,temp_c,temp_display,question\n")
        for (city, date_str), data in sorted(merged.items()):
            q = data.get("question", "").replace('"', '""')
            f.write(f'"{city}","{date_str}",{data["temp_c"]},{data["temp_display"]},"{q}"\n')
    print(f"CSV written to {csv_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
