#!/usr/bin/env python3
"""
Standalone crawler for Polymarket temperature markets.

Fetches https://polymarket.com/weather/temperature and extracts
all temperature market data WITHOUT using the Gamma API as the
primary data source.

IMPORTANT FINDINGS:
- Polymarket.com/weather/temperature is a fully client-rendered React SPA.
- No market data is embedded in the static HTML (only SEO meta tags).
- The CLOB API (clob.polymarket.com) does NOT support tag/category filtering.
- The Gamma API (gamma-api.polymarket.com) `tag` parameter is ignored for
  non-standard tags like "temperature" or "weather".
- Therefore, the crawler fetches all available markets from the public
  CLOB API and filters by temperature-related keywords client-side.

Strategies (tried in order):
  1. __NEXT_DATA__ embedded JSON (Next.js SSR) — NOT PRESENT on this page
  2. Inline script state (window.__INITIAL_STATE__ etc.) — NOT PRESENT
  3. CLOB API + client-side keyword filter — PRIMARY STRATEGY
  4. Gamma API + client-side keyword filter — SECONDARY FALLBACK
  5. Regex brute-force JSON from HTML — LAST RESORT

Usage:
    cd C:/Users/PC/Desktop/polymarket-arb-bot
    python _crawl_temperature_markets.py

Output:
    - Structured JSON printed to console
    - Saved to _crawled_markets.json
"""

from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Dependency checks — try requests + bs4, fall back to urllib + regex
# ---------------------------------------------------------------------------

HAS_REQUESTS = False
HAS_BS4 = False
_requests: Any = None
_BeautifulSoup: Any = None

try:
    import requests as _requests  # type: ignore[no-redef]
    HAS_REQUESTS = True
except ImportError:
    pass

try:
    from bs4 import BeautifulSoup as _BeautifulSoup  # type: ignore[assignment]
    HAS_BS4 = True
except ImportError:
    pass

from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TARGET_URL = "https://polymarket.com/weather/temperature"
OUTPUT_FILE = Path(__file__).resolve().parent / "_crawled_markets.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Keywords used to identify temperature-related markets (simple substrings)
# These are definitive — if a question contains any of these, it's a temperature market.
TEMP_KEYWORDS_SIMPLE = [
    "temperature", "highest temperature", "lowest temperature",
    "celsius", "fahrenheit",
    "heatwave", "heat wave", "max temp", "min temp",
    "high temp", "low temp", "record high", "record low",
    "hottest", "coldest", "thermometer", "heat index",
    "wind chill", "feels like", "humidity",
    "high will the temperature", "what will the temperature",
    "degrees fahrenheit", "degrees celsius", "deg f", "deg c",
]

# Short keywords needing word-boundary matching
# NOTE: "heat" is EXCLUDED because it matches sports teams (Miami Heat)
TEMP_KEYWORDS_BOUNDARY = [
    "hot", "cold", "warm", "cool", "chill",
    "freeze", "freezing", "frost", "melt",
]

# Precompile the boundary regex
_TEMP_BOUNDARY_RE = re.compile(
    r'\b(' + '|'.join(re.escape(kw) for kw in TEMP_KEYWORDS_BOUNDARY) + r')\b',
    re.IGNORECASE,
)

# Keywords that indicate a market is NOT temperature-related (false positive guards)
NOT_TEMP_KEYWORDS = [
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

# Common city names to match in temperature market questions
_COMMON_CITIES = [
    "Taipei", "Hong Kong", "Shanghai", "Seoul", "Tokyo", "Beijing",
    "Singapore", "Kuala Lumpur", "Bangkok", "Manila", "Jakarta",
    "Mumbai", "Delhi", "Lucknow", "Karachi", "Dhaka",
    "New York", "Los Angeles", "Chicago", "Houston", "Miami",
    "Dallas", "Atlanta", "Denver", "Seattle", "San Francisco",
    "London", "Paris", "Madrid", "Berlin", "Munich", "Milan",
    "Rome", "Amsterdam", "Warsaw", "Helsinki", "Istanbul",
    "Ankara", "Moscow", "Cape Town", "Sydney", "Melbourne",
    "Wellington", "Toronto", "Mexico City", "Sao Paulo",
    "Buenos Aires", "Tel Aviv", "Dubai", "Jeddah", "Panama City",
    "Shenzhen", "Guangzhou", "Chengdu", "Wuhan", "Chongqing",
    "Qingdao", "Zhengzhou", "Jinan",
]

# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def fetch_page(url: str, timeout: int = 30) -> str:
    """Fetch page content using the best available HTTP client."""
    if HAS_REQUESTS and _requests is not None:
        print(f"  [requests] Fetching {url} ...")
        resp = _requests.get(
            url,
            headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/json"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.text
    else:
        print(f"  [urllib] Fetching {url} ...")
        req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")


def fetch_api_json(url: str, timeout: int = 30) -> Optional[Any]:
    """Try fetching a JSON API endpoint. Returns parsed JSON or None."""
    try:
        if HAS_REQUESTS and _requests is not None:
            resp = _requests.get(
                url,
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp.json()
        else:
            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                return json.loads(raw)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Strategy 1: __NEXT_DATA__ (Next.js SSR embedded data)
# ---------------------------------------------------------------------------


def strategy_next_data(html: str) -> Optional[list[dict]]:
    """Extract market data from Next.js __NEXT_DATA__ script tag."""
    print("\n[Strategy 1] Trying __NEXT_DATA__ ...")
    soup = None
    if HAS_BS4 and _BeautifulSoup is not None:
        soup = _BeautifulSoup(html, "html.parser")

    script_content = None
    if soup is not None:
        tag = soup.find("script", id="__NEXT_DATA__")
        if tag and tag.string:
            script_content = tag.string
    else:
        match = re.search(
            r'<script\s+id="__NEXT_DATA__"[^>]*>\s*(.*?)\s*</script>',
            html, re.DOTALL,
        )
        if match:
            script_content = match.group(1)

    if not script_content:
        print("  [FAIL] No __NEXT_DATA__ script found (page is client-rendered)")
        return None

    try:
        data = json.loads(script_content)
    except json.JSONDecodeError as e:
        print(f"  [FAIL] Failed to parse __NEXT_DATA__ JSON: {e}")
        return None

    markets = _extract_markets_from_obj(data, path="__NEXT_DATA__")
    if markets:
        print(f"  [OK] Found {len(markets)} markets via __NEXT_DATA__")
        return markets
    print("  [FAIL] No markets found in __NEXT_DATA__")
    return None


# ---------------------------------------------------------------------------
# Strategy 2: Inline script JSON
# ---------------------------------------------------------------------------


def strategy_inline_scripts(html: str) -> Optional[list[dict]]:
    """Look for window.__INITIAL_STATE__, __NUXT__, etc."""
    print("\n[Strategy 2] Trying inline script JSON ...")

    state_patterns = [
        r"window\.__INITIAL_STATE__\s*=\s*({.*?});",
        r"window\.__NUXT__\s*=\s*({.*?});",
        r"window\.__REDUX_STATE__\s*=\s*({.*?});",
        r"window\.__PRELOADED_STATE__\s*=\s*({.*?});",
        r"__APOLLO_STATE__\s*=\s*({.*?});",
    ]

    for pattern in state_patterns:
        match = re.search(pattern, html, re.DOTALL)
        if not match:
            continue
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        markets = _extract_markets_from_obj(data, path=f"inline:{pattern[:40]}")
        if markets:
            print(f"  [OK] Found {len(markets)} markets via inline script state")
            return markets

    print("  [FAIL] No markets found in inline scripts")
    return None


# ---------------------------------------------------------------------------
# Strategy 3: CLOB API — fetch all markets, filter client-side
# ---------------------------------------------------------------------------


def strategy_clob_api() -> Optional[list[dict]]:
    """
    Fetch all markets from the CLOB API and filter for temperature-related
    ones by keyword matching.

    The CLOB API ignores tag/search/category params, so we fetch everything
    and filter in Python.
    """
    print("\n[Strategy 3] Trying CLOB API + client-side keyword filter ...")

    all_raw: list[dict] = []
    cursor = ""
    pages = 0
    max_pages = 5  # fetch up to 5000 markets

    while pages < max_pages:
        url = f"https://clob.polymarket.com/markets?limit=1000&next_cursor={cursor}"
        data = fetch_api_json(url)
        if data is None:
            print("  [FAIL] CLOB API request failed")
            break

        items = data.get("data", []) if isinstance(data, dict) else []
        if not items:
            break

        all_raw.extend(items)
        pages += 1
        cursor = data.get("next_cursor", "") if isinstance(data, dict) else ""
        if not cursor:
            break

        print(f"  Page {pages}: fetched {len(items)} markets (total: {len(all_raw)})")

    print(f"  Total raw markets fetched: {len(all_raw)}")

    # Filter for temperature-related markets
    temp_markets = _filter_and_standardize(all_raw)
    if temp_markets:
        print(f"  [OK] Found {len(temp_markets)} temperature markets via CLOB API")
        return temp_markets

    print("  [FAIL] No temperature markets in CLOB API results")
    return None


# ---------------------------------------------------------------------------
# Strategy 4: Gamma API — fetch with various params, filter client-side
# ---------------------------------------------------------------------------


def strategy_gamma_api() -> Optional[list[dict]]:
    """
    Try the Gamma API with various approaches and filter client-side.
    The tag parameter doesn't work for temperature, so we fetch broadly.
    """
    print("\n[Strategy 4] Trying Gamma API + client-side keyword filter ...")

    all_raw: list[dict] = []

    # Try multiple gamma-api endpoints
    endpoints = [
        "https://gamma-api.polymarket.com/markets?limit=500&tag=temperature",
        "https://gamma-api.polymarket.com/markets?limit=500&tag=weather",
        "https://gamma-api.polymarket.com/markets?limit=500&tag=climate",
        "https://gamma-api.polymarket.com/markets?limit=500",
    ]

    for url in endpoints:
        data = fetch_api_json(url)
        if data is None:
            print(f"  [FAIL] Gamma API: {url.split('?')[1] if '?' in url else url}")
            continue

        items = data if isinstance(data, list) else data.get("data", [])
        label = url.split("?")[1] if "?" in url else "no params"
        print(f"  Gamma API ({label}): got {len(items)} markets")
        all_raw.extend(items)

    # Deduplicate by question
    seen = set()
    unique = []
    for m in all_raw:
        q = m.get("question", "")
        if q and q not in seen:
            seen.add(q)
            unique.append(m)

    print(f"  Total unique markets: {len(unique)}")

    temp_markets = _filter_and_standardize(unique)
    if temp_markets:
        print(f"  [OK] Found {len(temp_markets)} temperature markets via Gamma API")
        return temp_markets

    print("  [FAIL] No temperature markets in Gamma API results")
    return None


# ---------------------------------------------------------------------------
# Strategy 5: Regex brute-force JSON from HTML
# ---------------------------------------------------------------------------


def strategy_regex_json(html: str) -> Optional[list[dict]]:
    """Extract JSON blobs from HTML and check for temperature data."""
    print("\n[Strategy 5] Trying regex brute-force JSON extraction ...")

    candidates: list[str] = []

    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.DOTALL):
        candidates.append(match.group(1))

    json_like = re.findall(r'(\{(?:[^{}]|(?:\{[^{}]*\}))*\})', html)
    candidates.extend(json_like)

    for match in re.finditer(r"\{[^<]{200,}?\}", html, re.DOTALL):
        blob = match.group(0)
        try:
            json.loads(blob)
            candidates.append(blob)
        except json.JSONDecodeError:
            pass

    print(f"  Found {len(candidates)} JSON candidates to inspect")

    for i, candidate in enumerate(candidates):
        candidate_lower = candidate.lower()
        if not any(kw in candidate_lower for kw in TEMP_KEYWORDS_SIMPLE):
            if not _TEMP_BOUNDARY_RE.search(candidate_lower):
                continue
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        markets = _extract_markets_from_obj(data, path=f"regex_candidate_{i}")
        if markets:
            print(f"  [OK] Found {len(markets)} markets via regex (candidate {i})")
            return markets

    print("  [FAIL] No markets found via regex brute-force")
    return None


# ---------------------------------------------------------------------------
# Market extraction & filtering helpers
# ---------------------------------------------------------------------------


def _filter_and_standardize(raw_markets: list[dict]) -> list[dict]:
    """Filter raw market dicts for temperature-related ones and standardize."""
    result = []
    for m in raw_markets:
        parsed = _standardize_market(m)
        if parsed and _is_temperature_market(parsed):
            result.append(parsed)
    return result


def _extract_markets_from_obj(obj: Any, path: str = "") -> Optional[list[dict]]:
    """Recursively search parsed JSON for market-like structures."""
    raw_markets: list[dict] = []

    if isinstance(obj, dict):
        for key in ("markets", "data", "results", "items", "events", "listings"):
            if key in obj and isinstance(obj[key], list):
                _collect_raw_markets(obj[key], raw_markets)
        if _is_market_like(obj):
            raw_markets.append(obj)
        for nested_key in ("props", "pageProps", "initialState", "state", "queryData"):
            if nested_key in obj and isinstance(obj[nested_key], dict):
                sub = _extract_markets_from_obj(obj[nested_key], f"{path}.{nested_key}")
                if sub:
                    raw_markets.extend(sub)

    elif isinstance(obj, list):
        _collect_raw_markets(obj, raw_markets)

    if not raw_markets:
        return None

    return _filter_and_standardize(raw_markets) or None


def _collect_raw_markets(items: list, out: list[dict]) -> None:
    """Scan a list for market-like dicts."""
    for item in items:
        if isinstance(item, dict) and _is_market_like(item):
            out.append(item)


def _is_market_like(obj: dict) -> bool:
    """Check if a dict looks like a Polymarket market object."""
    has_question = (
        "question" in obj
        and isinstance(obj["question"], str)
        and len(obj["question"]) > 3
    )
    has_outcomes = any(
        k in obj for k in ("outcomes", "tokens", "outcomePrices", "options")
    )
    has_volume = any(
        k in obj for k in ("volume", "volume24hr", "liquidity", "totalVolume")
    )
    return has_question and (has_outcomes or has_volume)


def _standardize_market(raw: dict) -> Optional[dict]:
    """Convert a raw Polymarket market object into our standard format."""
    question = (
        raw.get("question", "")
        or raw.get("title", "")
        or raw.get("description", "")
    )
    if not question:
        return None

    # --- Outcomes / tokens ---
    outcomes: list[dict] = []

    # CLOB API style: tokens list
    tokens = raw.get("tokens", [])
    if tokens:
        for t in tokens:
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

    # Gamma API style: outcomes + outcomePrices (parallel arrays)
    if not outcomes:
        outcome_labels = raw.get("outcomes", []) or raw.get("options", [])
        outcome_prices = (
            raw.get("outcomePrices", [])
            or raw.get("prices", [])
            or raw.get("probabilities", [])
        )
        for idx, label in enumerate(outcome_labels):
            price = None
            if idx < len(outcome_prices):
                price = outcome_prices[idx]
                if isinstance(price, str):
                    try:
                        price = float(price)
                    except (ValueError, TypeError):
                        price = None
            elif isinstance(label, dict):
                price = label.get("price") or label.get("probability")
                label = (
                    label.get("label")
                    or label.get("name")
                    or label.get("outcome", str(label))
                )
            outcomes.append({"label": str(label), "price": price})

    # Binary market fallback
    if not outcomes:
        yes_price = _to_float(raw.get("yes_price") or raw.get("price"))
        no_price = _to_float(raw.get("no_price", 0))
        if yes_price is not None:
            outcomes.append({"label": "Yes", "price": yes_price})
            outcomes.append({"label": "No", "price": no_price})

    # --- Volume ---
    volume = (
        raw.get("volume")
        or raw.get("volume24hr")
        or raw.get("totalVolume")
        or raw.get("volumeNum")
    )
    liquidity = raw.get("liquidity") or raw.get("liquidityNum") or raw.get("liquidityClob")
    volume_str = _format_volume(volume or liquidity)

    # --- City ---
    city = _extract_city(question, raw)

    # --- Date ---
    date_str = _extract_date(question, raw)

    return {
        "city": city,
        "question": question,
        "outcomes": outcomes,
        "volume": volume_str,
        "volume_raw": volume,
        "liquidity": liquidity,
        "date": date_str,
        "slug": raw.get("slug", "") or raw.get("market_slug", ""),
        "condition_id": raw.get("conditionId", "") or raw.get("condition_id", ""),
    }


def _is_temperature_market(parsed: dict) -> bool:
    """Check if a standardized market is temperature-related."""
    text = (parsed.get("question", "") + " " + parsed.get("city", "")).lower()

    # Negative filter: if it's clearly a sports game, skip
    if any(kw in text for kw in NOT_TEMP_KEYWORDS):
        return False

    # Simple substring keywords (most specific)
    if any(kw in text for kw in TEMP_KEYWORDS_SIMPLE):
        return True

    # Word-boundary keywords (avoid "shot" matching "hot", etc.)
    if _TEMP_BOUNDARY_RE.search(text):
        # Extra check: if boundary match is the only signal, also check
        # for weather context words to reduce false positives
        weather_context = ["weather", "forecast", "temp", "degree", "climate"]
        if any(wc in text for wc in weather_context):
            return True
        # Also check if there's a city name AND a temperature-like context
        has_city = any(city.lower() in text for city in _COMMON_CITIES)
        has_temp_context = any(
            kw in text for kw in ["degrees", "temp", "weather", "forecast", "august", "july", "june"]
        )
        if has_city and has_temp_context:
            return True
        return False

    return False


def _extract_city(question: str, raw: dict) -> str:
    """Extract city name from question or raw data."""
    # Try known cities first
    for city in _COMMON_CITIES:
        if city.lower() in question.lower():
            return city

    # Generic city pattern
    city_match = re.search(
        r"(?:in|for|at)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\b",
        question,
    )
    if city_match:
        return city_match.group(1)

    for key in ("city", "location", "venue", "place"):
        if key in raw and raw[key]:
            return str(raw[key])

    return "Unknown"


def _extract_date(question: str, raw: dict) -> str:
    """Extract target date from question or raw data."""
    for key in ("end_date_iso", "endDateIso", "endDate", "closeTime", "expiry"):
        if key in raw and raw[key]:
            return str(raw[key])[:10]

    date_match = re.search(
        r"(?:on|by|for)\s+([A-Z][a-z]{2,8}\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
        question,
    )
    if date_match:
        return date_match.group(1)

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", question)
    if date_match:
        return date_match.group(1)

    return "Unknown"


def _to_float(val: Any) -> Optional[float]:
    """Safely convert to float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except (ValueError, TypeError):
        return None


def _format_volume(val: Any) -> str:
    """Format volume number to human-readable string."""
    if val is None:
        return "$0"
    try:
        num = float(val)
    except (ValueError, TypeError):
        return str(val)
    if num >= 1_000_000:
        return f"${num / 1_000_000:.1f}M"
    elif num >= 1_000:
        return f"${num / 1_000:.0f}K"
    else:
        return f"${num:.0f}"


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


def crawl() -> list[dict]:
    """Run all strategies. Returns first non-empty list or empty list."""
    print("=" * 60)
    print("Polymarket Temperature Market Crawler")
    print("=" * 60)
    print(f"Target: {TARGET_URL}")
    print(f"requests: {HAS_REQUESTS}, BeautifulSoup: {HAS_BS4}")
    print()

    # --- Fetch the page (needed for strategies 1, 2, 5) ---
    html = ""
    try:
        html = fetch_page(TARGET_URL)
        print(f"  [OK] Page fetched ({len(html):,} bytes)")
    except Exception as e:
        print(f"  [FAIL] Failed to fetch page: {e}")

    # --- Run strategies sequentially ---
    strategies: list[tuple[str, Any]] = [
        ("__NEXT_DATA__ (Strategy 1)", lambda: strategy_next_data(html) if html else None),
        ("Inline Script JSON (Strategy 2)", lambda: strategy_inline_scripts(html) if html else None),
        ("CLOB API + keyword filter (Strategy 3)", strategy_clob_api),
        ("Gamma API + keyword filter (Strategy 4)", strategy_gamma_api),
        ("Regex Brute-Force (Strategy 5)", lambda: strategy_regex_json(html) if html else None),
    ]

    for name, strategy_fn in strategies:
        try:
            markets = strategy_fn()
            if markets:
                print(f"\n{'=' * 60}")
                print(f"SUCCESS via: {name}")
                print(f"Total temperature markets found: {len(markets)}")
                print(f"{'=' * 60}")
                return markets
        except Exception as e:
            print(f"  [FAIL] '{name}' raised: {e}")
            import traceback
            traceback.print_exc()
            continue

    print("\n" + "=" * 60)
    print("ALL STRATEGIES EXHAUSTED — No temperature markets found")
    print("=" * 60)
    print("This is likely because:")
    print("  - Polymarket has no active temperature markets right now")
    print("  - Or temperature markets use different question phrasing")
    print("  - Or they are behind authentication / geo-restrictions")
    return []


def save_results(markets: list[dict]) -> None:
    """Save markets to JSON file."""
    output = {
        "crawled_at": datetime.now(timezone.utc).isoformat(),
        "source_url": TARGET_URL,
        "total_markets": len(markets),
        "markets": markets,
    }
    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    print(f"\nSaved {len(markets)} markets to: {OUTPUT_FILE}")


def print_sample(markets: list[dict], max_show: int = 5) -> None:
    """Print a sample of markets to console."""
    sample = markets[:max_show]
    print(f"\n--- Sample ({len(sample)} of {len(markets)}) ---")
    print(json.dumps(sample, indent=2, ensure_ascii=False))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    """Main entry point. Returns exit code (0 = success)."""
    start = time.monotonic()

    markets = crawl()

    if markets:
        print_sample(markets)
        save_results(markets)

    elapsed = time.monotonic() - start
    print(f"\nCompleted in {elapsed:.1f}s")
    return 0 if markets else 1


if __name__ == "__main__":
    sys.exit(main())
