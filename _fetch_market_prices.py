#!/usr/bin/env python3
"""
Polymarket Market Price Fetcher — Multi-Strategy Aggregator
============================================================

Fetches temperature/weather markets from Polymarket using ALL available
API endpoints, trying harder than the previous crawler.

STRATEGIES (tried in order):
  1. CLOB API — keyset pagination with next_cursor, fetches ALL markets
  2. Gamma /events — lists all active events with embedded markets
  3. Gamma /markets — direct market listing with various filter combos
  4. Gamma /tags — discover tag IDs, then filter by tag_id
  5. Direct polymarket.com API endpoints (if available)

FALLBACK:
  If ALL APIs fail, generates _scrape_bookmarklet.js — a browser
  console script that extracts market data from polymarket.com.

OUTPUT:
  _market_prices.json — structured JSON with market prices.

USAGE:
  cd "C:/Users/PC/Desktop/vaer monitor"
  python _fetch_market_prices.py
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Optional: requests library (much better than urllib)
# ---------------------------------------------------------------------------
HAS_REQUESTS = False
_requests: Any = None

try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "_market_prices.json"
BOOKMARKLET_FILE = SCRIPT_DIR / "_scrape_bookmarklet.js"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Temperature keywords — comprehensive matching
# ---------------------------------------------------------------------------
TEMP_KEYWORDS = [
    "temperature", "highest temperature", "lowest temperature",
    "celsius", "fahrenheit", "heatwave", "heat wave",
    "max temp", "min temp", "high temp", "low temp",
    "record high", "record low", "hottest", "coldest",
    "thermometer", "heat index", "wind chill", "feels like",
    "humidity", "degrees fahrenheit", "degrees celsius",
    "deg f", "deg c", "weather forecast",
    "high will the temperature", "what will the temperature",
]

# Anti-keywords: sports/other false positives
NOT_WEATHER = [
    "nba:", "nfl:", "mlb:", "nhl:", "ncaab:", "ncaaf:",
    "heat vs", "heat -", "magic vs", "thunder vs", "warriors vs",
    "lakers vs", "celtics vs", "bulls vs", "knicks vs", "nets vs",
    "raptors vs", "hawks vs", "cavaliers vs", "pistons vs",
    "pacers vs", "bucks vs", "76ers vs", "hornets vs",
    "wizards vs", "grizzlies vs", "pelicans vs", "spurs vs",
    "nuggets vs", "timberwolves vs", "trail blazers vs",
    "jazz vs", "suns vs", "kings vs", "clippers vs", "rockets vs",
    "mavericks vs", "miami heat",
]

# City names for extraction
CITIES = [
    "Moscow", "Taipei", "Hong Kong", "Shanghai", "Seoul", "Tokyo",
    "Beijing", "Singapore", "Kuala Lumpur", "Bangkok", "Manila",
    "Jakarta", "Mumbai", "Delhi", "Lucknow", "Karachi", "Dhaka",
    "New York", "Los Angeles", "Chicago", "Houston", "Miami",
    "Dallas", "Atlanta", "Denver", "Seattle", "San Francisco",
    "London", "Paris", "Madrid", "Berlin", "Munich", "Milan",
    "Rome", "Amsterdam", "Warsaw", "Helsinki", "Istanbul",
    "Ankara", "Cape Town", "Sydney", "Melbourne", "Wellington",
    "Toronto", "Mexico City", "Sao Paulo", "Buenos Aires",
    "Tel Aviv", "Dubai", "Jeddah", "Panama City",
    "Shenzhen", "Guangzhou", "Chengdu", "Wuhan", "Chongqing",
    "Qingdao", "Zhengzhou", "Jinan",
]


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 30, accept: str = "application/json") -> Optional[Any]:
    """GET a URL, parse as JSON if possible. Returns parsed JSON or raw text."""
    try:
        if HAS_REQUESTS and _requests is not None:
            resp = _requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": accept},
                timeout=timeout,
            )
            resp.raise_for_status()
            ct = resp.headers.get("content-type", "")
            if "application/json" in ct:
                return resp.json()
            return resp.text
        else:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
    except Exception as e:
        print(f"    [HTTP ERROR] {url[:80]}: {e}")
        return None


def http_get_json(url: str, timeout: int = 30) -> Optional[Any]:
    """GET a URL expecting JSON back."""
    return http_get(url, timeout, accept="application/json")


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def is_weather_market(question: str) -> bool:
    """Check if a question is temperature/weather related."""
    q = question.lower()
    # Reject sports/other
    for kw in NOT_WEATHER:
        if kw in q:
            return False
    # Accept weather keywords
    for kw in TEMP_KEYWORDS:
        if kw in q:
            return True
    return False


def extract_city(question: str) -> str:
    """Extract city name from a question string."""
    q_lower = question.lower()
    for city in CITIES:
        if city.lower() in q_lower:
            return city
    # Fallback: match "in City Name" pattern
    m = re.search(r"(?:in|for|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b", question)
    if m:
        return m.group(1)
    return "Unknown"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_json_string(val: Any) -> Any:
    """If val is a JSON-encoded string, parse it. Otherwise return as-is."""
    if isinstance(val, str) and val.strip().startswith(("[\"", "[\'", "{")):
        try:
            return json.loads(val)
        except (json.JSONDecodeError, ValueError):
            pass
    return val


# ---------------------------------------------------------------------------
# Standardization
# ---------------------------------------------------------------------------

def standardize_market(raw: dict) -> Optional[dict]:
    """Convert raw API market/event data to standardized format."""
    question = (
        raw.get("question", "")
        or raw.get("title", "")
        or raw.get("ticker", "")
        or raw.get("description", "")
    )
    if not question:
        return None

    # ---- Outcomes ----
    outcomes: list[dict] = []

    # CLOB style: tokens array
    tokens = raw.get("tokens", [])
    if isinstance(tokens, list) and tokens:
        for t in tokens:
            if not isinstance(t, dict):
                continue
            label = t.get("outcome", "") or t.get("label", "") or t.get("option", "")
            price = t.get("price", None)
            if price is None:
                price = t.get("lastPrice") or t.get("outcomePrice") or t.get("probability")
            if isinstance(price, str):
                try:
                    price = float(price)
                except (ValueError, TypeError):
                    price = None
            outcomes.append({"label": str(label), "price": price})

    # Gamma style: outcomes + outcomePrices (parallel arrays)
    # NOTE: Gamma API often returns these as JSON-encoded strings like '["Yes","No"]'
    if not outcomes:
        olabels = _parse_json_string(raw.get("outcomes", []) or raw.get("options", []))
        oprices = _parse_json_string(
            raw.get("outcomePrices", [])
            or raw.get("prices", [])
            or raw.get("probabilities", [])
        )

        # If still a string (not parseable), skip
        if isinstance(olabels, str):
            olabels = []
        if isinstance(oprices, str):
            oprices = []

        if isinstance(olabels, list) and olabels:
            for i, lbl in enumerate(olabels):
                p = None
                if isinstance(oprices, list) and i < len(oprices):
                    p = oprices[i]
                    if isinstance(p, str):
                        try:
                            p = float(p)
                        except (ValueError, TypeError):
                            p = None
                elif isinstance(lbl, dict):
                    p = lbl.get("price") or lbl.get("probability")
                    lbl = lbl.get("label") or lbl.get("name") or lbl.get("outcome", str(lbl))
                outcomes.append({"label": str(lbl), "price": p})

    # Binary fallback
    if not outcomes:
        yp = _to_float(raw.get("yes_price") or raw.get("price"))
        np = _to_float(raw.get("no_price", 0))
        if yp is not None:
            outcomes = [{"label": "Yes", "price": yp}, {"label": "No", "price": np}]

    # ---- Volume ----
    vol = (
        raw.get("volume")
        or raw.get("volume24hr")
        or raw.get("totalVolume")
        or raw.get("volumeNum")
        or raw.get("liquidity")
        or raw.get("liquidityNum")
        or raw.get("liquidityClob")
    )
    vol_num = _to_float(vol) or 0

    # ---- City & Date ----
    city = extract_city(question)

    date_str = "Unknown"
    for k in ("end_date_iso", "endDateIso", "endDate", "closeTime", "expiry"):
        if k in raw and raw[k]:
            date_str = str(raw[k])[:10]
            break

    return {
        "city": city,
        "question": question,
        "outcomes": outcomes,
        "volume": int(vol_num),
        "volume_display": _format_volume(vol_num),
        "date": date_str,
        "slug": raw.get("slug", "") or raw.get("market_slug", ""),
        "condition_id": raw.get("conditionId", "") or raw.get("condition_id", ""),
    }


def _to_float(val: Any) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return None


def _format_volume(val: float) -> str:
    if val >= 1_000_000:
        return f"${val / 1_000_000:.1f}M"
    elif val >= 1_000:
        return f"${val / 1_000:.0f}K"
    else:
        return f"${val:.0f}"


# ---------------------------------------------------------------------------
# STRATEGY 1: CLOB API with keyset pagination
# ---------------------------------------------------------------------------

def strategy_clob_paginated() -> tuple[list[dict], str]:
    """
    Fetch ALL markets from the CLOB API using keyset (cursor) pagination.
    The CLOB API returns markets in batches; we iterate until exhausted.
    """
    print("\n" + "=" * 60)
    print("STRATEGY 1: CLOB API — Keyset Pagination")
    print("=" * 60)

    all_raw: list[dict] = []
    cursor = ""
    pages = 0
    max_pages = 10  # 10 pages * 1000 = 10000 markets (proven sufficient)

    while pages < max_pages:
        url = f"https://clob.polymarket.com/markets?limit=500&next_cursor={cursor}"
        data = http_get_json(url)

        if data is None:
            print(f"  [FAIL] Request failed on page {pages + 1}")
            break

        # CLOB API wraps results in {"data": [...], "next_cursor": "..."}
        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            print(f"  [DONE] No more items after page {pages}")
            break

        all_raw.extend(items)
        pages += 1

        new_cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""
        print(f"  Page {pages}: {len(items)} markets "
              f"(total: {len(all_raw)}, cursor: {new_cursor[:20]}...)")

        if not new_cursor or new_cursor == cursor:
            break
        cursor = new_cursor
        time.sleep(0.3)  # rate-limit courtesy

    print(f"  Total raw CLOB markets: {len(all_raw)}")

    # Filter for weather
    weather: list[dict] = []
    for raw in all_raw:
        q = raw.get("question", "")
        if q and is_weather_market(q):
            std = standardize_market(raw)
            if std:
                weather.append(std)

    print(f"  Weather markets found: {len(weather)}")
    return weather, f"CLOB paginated ({pages} pages, {len(all_raw)} raw markets)"


# ---------------------------------------------------------------------------
# STRATEGY 2: Gamma /events with embedded markets
# ---------------------------------------------------------------------------

def strategy_gamma_events() -> tuple[list[dict], str]:
    """
    Fetch all active events from Gamma API. Each event contains
    embedded markets. Filter for temperature/weather keywords.
    """
    print("\n" + "=" * 60)
    print("STRATEGY 2: Gamma /events — Active Events")
    print("=" * 60)

    all_events: list[dict] = []
    offset = 0
    limit = 100
    pages = 0
    max_pages = 20  # 20 pages * 100 events = 2000 events

    while pages < max_pages:
        url = (
            f"https://gamma-api.polymarket.com/events"
            f"?active=true&closed=false&limit={limit}&offset={offset}"
        )
        data = http_get_json(url)

        if data is None:
            print(f"  [FAIL] Request failed at offset {offset}")
            break

        items = data if isinstance(data, list) else data.get("data", [])
        if not items:
            print(f"  [DONE] No more events at offset {offset}")
            break

        all_events.extend(items)
        pages += 1
        print(f"  Page {pages}: {len(items)} events (total: {len(all_events)}), "
              f"offset={offset}")

        if len(items) < limit:
            break
        offset += limit
        time.sleep(0.3)

    print(f"  Total events fetched: {len(all_events)}")

    # Extract markets from events
    weather: list[dict] = []
    total_markets = 0

    for event in all_events:
        title = event.get("title", "") or event.get("ticker", "")
        markets = event.get("markets", [])

        if not markets:
            # Also check if the event itself is a market
            if title and is_weather_market(title):
                std = standardize_market(event)
                if std:
                    weather.append(std)
            continue

        total_markets += len(markets)

        for mkt in markets:
            q = mkt.get("question", "") or title
            if q and is_weather_market(q):
                std = standardize_market(mkt)
                if std:
                    weather.append(std)

    print(f"  Total embedded markets examined: {total_markets}")
    print(f"  Weather markets found: {len(weather)}")
    return weather, f"Gamma events ({len(all_events)} events, {total_markets} markets)"


# ---------------------------------------------------------------------------
# STRATEGY 3: Gamma /markets direct
# ---------------------------------------------------------------------------

def strategy_gamma_markets() -> tuple[list[dict], str]:
    """
    Query Gamma /markets directly with various parameter combinations.
    Try different orders and filters to maximize coverage.
    """
    print("\n" + "=" * 60)
    print("STRATEGY 3: Gamma /markets — Direct Market Queries")
    print("=" * 60)

    all_raw: list[dict] = []

    # Different query variants to try
    queries = [
        ("active=true&closed=false&limit=100", "active markets"),
        ("active=true&closed=false&limit=100&order=volume24hr&ascending=false",
         "active by volume"),
        ("active=true&closed=false&limit=100&order=liquidity&ascending=false",
         "active by liquidity"),
    ]

    for params, label in queries:
        offset = 0
        pages = 0
        while pages < 5:  # 5 pages * 100 = 500 markets per query
            url = (
                f"https://gamma-api.polymarket.com/markets"
                f"?{params}&offset={offset}"
            )
            data = http_get_json(url)

            if data is None:
                break

            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                break

            all_raw.extend(items)
            pages += 1
            print(f"  [{label}] offset={offset}: {len(items)} markets "
                  f"(total: {len(all_raw)})")

            if len(items) < 100:
                break
            offset += 100
            time.sleep(0.3)

        offset = 0  # reset for next query

    # Deduplicate by question
    seen = set()
    unique = []
    for m in all_raw:
        q = m.get("question", "")
        if q and q not in seen:
            seen.add(q)
            unique.append(m)

    print(f"  Total unique markets: {len(unique)}")

    # Filter weather
    weather: list[dict] = []
    for m in unique:
        q = m.get("question", "")
        if q and is_weather_market(q):
            std = standardize_market(m)
            if std:
                weather.append(std)

    print(f"  Weather markets found: {len(weather)}")
    return weather, f"Gamma markets ({len(unique)} unique)"


# ---------------------------------------------------------------------------
# STRATEGY 4: Gamma /tags → discover tag IDs → filter
# ---------------------------------------------------------------------------

def strategy_gamma_tags() -> tuple[list[dict], str]:
    """
    Discover available tags from Gamma API, find weather-related tag IDs,
    then query events/markets by those tag IDs.
    """
    print("\n" + "=" * 60)
    print("STRATEGY 4: Gamma /tags — Tag Discovery")
    print("=" * 60)

    # Fetch all tags
    tags_url = "https://gamma-api.polymarket.com/tags"
    tags_data = http_get_json(tags_url)

    if tags_data is None:
        print("  [FAIL] Could not fetch tags")
        return [], "tags discovery failed"

    tags = tags_data if isinstance(tags_data, list) else tags_data.get("data", [])
    print(f"  Found {len(tags)} tags")

    # Look for weather/temperature related tags
    weather_tag_ids: list[int] = []
    for tag in tags:
        tid = tag.get("id")
        tlabel = (tag.get("label", "") or tag.get("name", "") or tag.get("slug", "")).lower()
        if any(kw in tlabel for kw in ["weather", "temperature", "climate", "temp"]):
            weather_tag_ids.append(tid)
            print(f"  → Weather tag: id={tid}, label={tag.get('label', '?')}")

    if not weather_tag_ids:
        print("  No weather-specific tags found. Printing all tag labels:")
        for tag in tags[:30]:
            print(f"    id={tag.get('id')}: {tag.get('label', '?')}")
        return [], "no weather tags found"

    # Query events by weather tag IDs
    all_raw: list[dict] = []
    for tid in weather_tag_ids:
        offset = 0
        while True:
            url = (
                f"https://gamma-api.polymarket.com/events"
                f"?tag_id={tid}&active=true&closed=false&limit=100&offset={offset}"
            )
            data = http_get_json(url)
            if data is None:
                break
            items = data if isinstance(data, list) else data.get("data", [])
            if not items:
                break
            all_raw.extend(items)
            print(f"  tag_id={tid} offset={offset}: {len(items)} events "
                  f"(total: {len(all_raw)})")
            if len(items) < 100:
                break
            offset += 100
            time.sleep(0.3)

    # Extract markets from events
    weather: list[dict] = []
    total_markets = 0
    for event in all_raw:
        markets = event.get("markets", [])
        total_markets += len(markets)
        for mkt in markets:
            q = mkt.get("question", "")
            if q and is_weather_market(q):
                std = standardize_market(mkt)
                if std:
                    weather.append(std)

    print(f"  Total embedded markets: {total_markets}")
    print(f"  Weather markets found: {len(weather)}")
    return weather, (
        f"Gamma tags ({len(weather_tag_ids)} tags, "
        f"{len(all_raw)} events, {total_markets} markets)"
    )


# ---------------------------------------------------------------------------
# STRATEGY 5: Direct polymarket.com API
# ---------------------------------------------------------------------------

def strategy_direct_api() -> tuple[list[dict], str]:
    """
    Try direct polymarket.com API endpoints.
    These may or may not exist; we attempt several patterns.
    """
    print("\n" + "=" * 60)
    print("STRATEGY 5: Direct polymarket.com API")
    print("=" * 60)

    endpoints = [
        "https://polymarket.com/api/events?tag=temperature",
        "https://polymarket.com/api/markets?tag=temperature",
        "https://polymarket.com/api/events?active=true&limit=100",
        "https://polymarket.com/_next/data/.../weather/temperature.json",  # won't work but worth noting
    ]

    weather: list[dict] = []
    results = []

    for url in endpoints[:3]:  # skip the placeholder
        data = http_get_json(url)
        if data is None:
            print(f"  [FAIL] {url[:70]}...")
            results.append("failed")
            continue

        results.append("success")
        items = data if isinstance(data, list) else data.get("data", []) or data.get("events", [])
        print(f"  {url[:70]}... → {len(items) if isinstance(items, list) else type(items).__name__}")

        if isinstance(items, list):
            for item in items:
                # Check if the item has markets embedded (event format)
                sub_markets = item.get("markets", [])
                for mkt in sub_markets:
                    q = mkt.get("question", "") or item.get("title", "")
                    if q and is_weather_market(q):
                        std = standardize_market(mkt)
                        if std:
                            weather.append(std)
                # Also check if item itself is a market
                q = item.get("question", "") or item.get("title", "")
                if q and is_weather_market(q):
                    std = standardize_market(item)
                    if std:
                        weather.append(std)

    if any(r == "success" for r in results):
        print(f"  Weather markets found: {len(weather)}")
        return weather, "direct API"
    else:
        print("  All direct API endpoints failed")
        return [], "direct API failed"


# ---------------------------------------------------------------------------
# Bookmarklet fallback generator
# ---------------------------------------------------------------------------

def generate_bookmarklet() -> None:
    """
    Generate a JavaScript bookmarklet that extracts market data from
    polymarket.com/weather/temperature when run in the browser console.
    """
    print("\n" + "=" * 60)
    print("GENERATING BOOKMARKLET FALLBACK")
    print("=" * 60)

    js_code = r'''// === Polymarket Temperature Market Scraper ===
// Run this in the browser console on polymarket.com/weather/temperature
// to extract market data as JSON.

(async function scrapePolymarketWeather() {
  console.log("🔍 Scraping Polymarket temperature markets...");

  const markets = [];

  // Strategy A: Look for React state / Redux store
  const selectors = [
    '[data-testid="market-card"]',
    '.market-card',
    '[class*="MarketCard"]',
    '[class*="market"]',
    'a[href*="/event/"]',
  ];

  // Try to find market data in window / React internals
  const rootEl = document.getElementById('__next');
  if (rootEl && rootEl._reactRootContainer) {
    console.log("Found React root container");
  }

  // Strategy B: Intercept network requests
  const origFetch = window.fetch;
  const apiData = [];

  window.fetch = async function(...args) {
    const response = await origFetch.apply(this, args);
    const url = args[0];
    if (typeof url === 'string' &&
        (url.includes('gamma-api.polymarket.com') ||
         url.includes('clob.polymarket.com'))) {
      try {
        const clone = response.clone();
        const json = await clone.json();
        apiData.push({url, data: json});
        console.log("📡 Captured API response:", url);
      } catch(e) {}
    }
    return response;
  };

  // Strategy C: Extract from DOM
  document.querySelectorAll('a[href*="/event/"]').forEach(el => {
    const text = el.textContent?.trim() || '';
    const href = el.getAttribute('href') || '';
    if (text.toLowerCase().includes('temperature') ||
        text.toLowerCase().includes('celsius') ||
        text.toLowerCase().includes('fahrenheit')) {
      markets.push({
        question: text,
        slug: href.replace('/event/', ''),
        source: 'dom'
      });
    }
  });

  // Strategy D: Look for __NEXT_DATA__
  const nextDataEl = document.getElementById('__NEXT_DATA__');
  if (nextDataEl) {
    try {
      const nextData = JSON.parse(nextDataEl.textContent);
      console.log("📦 Found __NEXT_DATA__:", Object.keys(nextData));
      // Recursively search for market data
      function findMarkets(obj, depth = 0) {
        if (!obj || depth > 10) return [];
        if (Array.isArray(obj)) {
          return obj.filter(item =>
            item && typeof item === 'object' &&
            (item.question || item.title) &&
            (item.outcomes || item.outcomePrices || item.tokens)
          );
        }
        if (typeof obj === 'object') {
          let results = [];
          for (const key of ['markets', 'events', 'data', 'results', 'items']) {
            if (obj[key] && Array.isArray(obj[key])) {
              results = results.concat(findMarkets(obj[key], depth + 1));
            }
          }
          return results;
        }
        return [];
      }
      const found = findMarkets(nextData);
      found.forEach(m => markets.push({...m, source: 'next_data'}));
    } catch(e) {
      console.log("Could not parse __NEXT_DATA__:", e.message);
    }
  }

  // Wait a moment for any API calls to complete
  await new Promise(r => setTimeout(r, 2000));

  // Process API data
  apiData.forEach(({url, data}) => {
    const items = Array.isArray(data) ? data :
                  data.data || data.markets || data.events || [];
    items.forEach(item => {
      const q = item.question || item.title || '';
      if (q.toLowerCase().includes('temperature') ||
          q.toLowerCase().includes('celsius') ||
          q.toLowerCase().includes('fahrenheit') ||
          q.toLowerCase().includes('weather')) {
        markets.push({...item, source: 'api'});
      }
    });
  });

  // Deduplicate
  const seen = new Set();
  const unique = markets.filter(m => {
    const key = m.question || m.title || m.slug || JSON.stringify(m);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });

  // Build output
  const output = {
    scraped_at: new Date().toISOString(),
    source_url: window.location.href,
    total_markets: unique.length,
    markets: unique.map(m => ({
      city: (m.question || m.title || '').match(
        /(?:in|for|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)/
      )?.[1] || 'Unknown',
      question: m.question || m.title || '',
      outcomes: (m.outcomes || m.outcomePrices || []).map((o, i) => ({
        label: typeof o === 'string' ? o : (o.label || o.outcome || o.name || String(i)),
        price: typeof o === 'object' ? (o.price || o.probability || null) :
               (m.outcomePrices ? m.outcomePrices[i] : null)
      })),
      volume: m.volume || m.volume24hr || m.totalVolume || 0,
      date: m.endDate || m.end_date_iso || m.closeTime || 'Unknown',
      slug: m.slug || (m.question||'').toLowerCase().replace(/[^a-z0-9]+/g, '-'),
    }))
  };

  console.log(`✅ Found ${unique.length} temperature markets`);
  console.log("📋 Copy this JSON:");
  console.log(JSON.stringify(output, null, 2));

  // Also offer download
  const blob = new Blob([JSON.stringify(output, null, 2)], {type: 'application/json'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'polymarket_temperature_markets.json';
  a.click();

  return output;
})();
'''

    BOOKMARKLET_FILE.write_text(js_code, encoding="utf-8")
    print(f"  Bookmarklet saved to: {BOOKMARKLET_FILE}")
    print(f"  Open polymarket.com/weather/temperature, press F12,")
    print(f"  paste the contents of _scrape_bookmarklet.js into the console,")
    print(f"  and it will extract and download the market data.")


# ---------------------------------------------------------------------------
# Aggregation & Output
# ---------------------------------------------------------------------------

def deduplicate_markets(markets: list[dict]) -> list[dict]:
    """Remove duplicates by question, keeping the one with most outcomes."""
    by_q: dict[str, dict] = {}
    for m in markets:
        q = m.get("question", "")
        if q not in by_q or len(m.get("outcomes", [])) > len(by_q[q].get("outcomes", [])):
            by_q[q] = m
    # Sort by volume descending
    result = sorted(by_q.values(), key=lambda x: x.get("volume", 0), reverse=True)
    return result


def save_results(markets: list[dict], source_info: str) -> None:
    """Save markets to _market_prices.json."""
    markets = deduplicate_markets(markets)

    output = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": source_info,
        "total_markets": len(markets),
        "markets": [
            {
                "city": m["city"],
                "question": m["question"],
                "outcomes": m["outcomes"],
                "volume": m["volume"],
                "volume_display": m["volume_display"],
                "date": m["date"],
            }
            for m in markets
        ],
    }

    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"\n[SAVED] {len(markets)} markets to: {OUTPUT_FILE}")


def print_sample(markets: list[dict], n: int = 5) -> None:
    """Print a sample of markets."""
    if not markets:
        return
    sample = markets[:n]
    print(f"\n--- Sample ({len(sample)} of {len(markets)}) ---")
    print(json.dumps(sample, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    """Run all strategies, aggregate results, save output."""
    print("=" * 60)
    print("POLYMARKET MARKET PRICE FETCHER")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"requests available: {HAS_REQUESTS}")
    print("=" * 60)

    all_markets: list[dict] = []
    sources: list[str] = []

    strategies = [
        ("CLOB Paginated", strategy_clob_paginated),
        ("Gamma Events", strategy_gamma_events),
        ("Gamma Markets", strategy_gamma_markets),
        ("Gamma Tags", strategy_gamma_tags),
        ("Direct API", strategy_direct_api),
    ]

    for name, fn in strategies:
        try:
            markets, info = fn()
            if markets:
                print(f"\n  [OK] {name}: {len(markets)} weather markets -- {info}")
                all_markets.extend(markets)
                sources.append(info)
            else:
                print(f"\n  [NO] {name}: No weather markets found -- {info}")
        except Exception as e:
            print(f"\n  [CRASH] {name}: {e}")
            import traceback
            traceback.print_exc()

    # Deduplicate and save
    all_markets = deduplicate_markets(all_markets)
    combined_source = " + ".join(sources) if sources else "all strategies failed"

    print("\n" + "=" * 60)
    print(f"FINAL: {len(all_markets)} unique weather/temperature markets")
    print(f"Sources: {combined_source}")
    print("=" * 60)

    if all_markets:
        print_sample(all_markets)
        save_results(all_markets, combined_source)
        return 0
    else:
        print("\n[WARN] ALL API STRATEGIES FAILED to find weather markets.")
        print("   Generating bookmarklet fallback...")
        generate_bookmarklet()
        return 1


if __name__ == "__main__":
    sys.exit(main())
