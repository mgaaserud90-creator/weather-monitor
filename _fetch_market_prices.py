#!/usr/bin/env python3
"""
Polymarket Market Price Fetcher — Optimized (tag-slug primary)
==============================================================

Fetches today's daily-temperature markets from the Polymarket Gamma API and
writes their live prices to ``_market_prices.json``.

The previous implementation ran five independent strategy passes (CLOB keyset
pagination, Gamma /events, Gamma /markets with three query variants, Gamma
/tags discovery, and direct polymarket.com probes) with 0.3 s sleeps between
pages. That produced dozens of redundant HTTP requests and made a single run
too close to the 5-minute workflow window.

This optimized version:

  * PRIMARY  — Gamma ``/events?tag_slug=daily-temperature`` (the tag-slug path
    introduced for market discovery). The nested ``markets[]`` on each event
    already carry the individual markets AND their live ``outcomePrices``, so
    one or two requests replace the old multi-strategy fan-out. See
    ``src/config/constants.py`` (WEATHER_TAG_SLUG) and
    ``src/strategies/weather/discovery.py``.
  * FALLBACK — a single volume-sorted Gamma ``/markets`` query (max 3 pages),
    kept only for resilience if the tag-slug endpoint changes.
  * Reuses one shared HTTP client/session for every request and drops all the
    inter-page sleeps (there are only a handful of requests now).

The output schema of ``_market_prices.json`` is byte-for-byte compatible with
the previous version (same top-level keys and the same per-market keys), so
``_generate_quality_report.py`` consumers (``_load_market_city_set`` and
``_load_resolved_market_outcomes``) and ``_compute_market_edge.py`` keep
working unchanged.

USAGE:
  python _fetch_market_prices.py
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Optional HTTP libraries — prefer a real client with a persistent session.
# ---------------------------------------------------------------------------
HAS_REQUESTS = False
_requests: Any = None
try:
    import requests as _requests
    HAS_REQUESTS = True
except ImportError:
    pass

HAS_HTTPX = False
_httpx: Any = None
try:
    import httpx as _httpx
    HAS_HTTPX = True
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Paths / endpoints
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SCRIPT_DIR / "_market_prices.json"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
GAMMA_MARKETS_URL = "https://gamma-api.polymarket.com/markets"

# Mirrors src/config/constants.py — the reliable daily-temperature tag slug.
WEATHER_TAG_SLUG = "daily-temperature"
EVENTS_PAGE_SIZE = 100
EVENTS_MAX_PAGES = 3          # known-good: 144 events = 2 pages, cap at 3
MARKETS_PAGE_SIZE = 100
MARKETS_MAX_PAGES = 3         # fallback only; 300 markets is plenty

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
# Shared HTTP client
# ---------------------------------------------------------------------------

class HTTPClient:
    """Minimal HTTP JSON client with one persistent session for all requests.

    Uses ``requests`` or ``httpx`` when available (both keep a connection
    pool), falling back to stdlib ``urllib`` so the script still runs on a
    bare GitHub Actions runner.
    """

    def __init__(self) -> None:
        self._session: Any = None
        self._mode = "urllib"
        if HAS_REQUESTS and _requests is not None:
            self._session = _requests.Session()
            self._session.headers.update({
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
            })
            self._mode = "requests"
        elif HAS_HTTPX and _httpx is not None:
            self._session = _httpx.Client(
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                timeout=30.0,
            )
            self._mode = "httpx"

    def get_json(self, url: str, timeout: int = 30) -> Optional[Any]:
        """GET ``url`` and return parsed JSON (or raw text if not JSON)."""
        try:
            if self._session is not None:
                resp = self._session.get(url, timeout=timeout)
                resp.raise_for_status()
                ct = resp.headers.get("content-type", "")
                if "application/json" in ct:
                    return resp.json()
                return resp.text

            req = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
            with urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except Exception as e:  # noqa: BLE001 — fail soft, return None
            print(f"    [HTTP ERROR] {url[:80]}: {e}")
            return None

    def close(self) -> None:
        """Close the underlying session, if any."""
        if self._session is not None:
            try:
                self._session.close()
            except Exception:  # noqa: BLE001
                pass

    @property
    def mode(self) -> str:
        return self._mode


# ---------------------------------------------------------------------------
# Keyword matching
# ---------------------------------------------------------------------------

def is_weather_market(question: str) -> bool:
    """Check if a question is temperature/weather related."""
    q = question.lower()
    for kw in NOT_WEATHER:
        if kw in q:
            return False
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


def _as_list(value: Any) -> list:
    """Normalize Gamma nested-market JSON-string arrays to native lists."""
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


# ---------------------------------------------------------------------------
# Standardization
# ---------------------------------------------------------------------------

def _extract_question_type(question: str) -> str:
    """Determine if this is a highest or lowest temperature market."""
    q = question.lower()
    if "highest temperature" in q or "highest temp" in q or "høyeste temperatur" in q:
        return "highest"
    if "lowest temperature" in q or "lowest temp" in q or "laveste temperatur" in q:
        return "lowest"
    return "unknown"


def _extract_market_date_from_question(question: str) -> str:
    """Extract the target date from the question text.

    Handles 'on 2026-08-10', 'August 10', 'Aug 10, 2026', etc.
    Returns YYYY-MM-DD or empty string.
    """
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', question)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"

    _MONTH_MAP = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
        "jan": 1, "feb": 2, "mar": 3, "apr": 4,
        "jun": 6, "jul": 7, "aug": 8,
        "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    }
    m = re.search(
        r'(January|February|March|April|May|June|July|August|September|October|November|December|'
        r'Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\.?\s+(\d{1,2})(?:,?\s+(\d{4}))?',
        question, re.IGNORECASE,
    )
    if m:
        from datetime import date as _date
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year_str = m.group(3)
        year = int(year_str) if year_str else _date.today().year
        month = _MONTH_MAP.get(month_name)
        if month:
            try:
                return _date(year, month, day).isoformat()
            except ValueError:
                pass
    return ""


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

    # Gamma style: outcomes + outcomePrices (parallel arrays, possibly
    # JSON-encoded strings). Callers are expected to pre-normalize via
    # ``_as_list``, but we also handle raw values defensively here.
    if not outcomes:
        olabels = _as_list(_parse_json_string(raw.get("outcomes", []) or raw.get("options", [])))
        oprices = _as_list(
            _parse_json_string(
                raw.get("outcomePrices", [])
                or raw.get("prices", [])
                or raw.get("probabilities", [])
            )
        )

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

    if date_str == "Unknown":
        date_str = _extract_market_date_from_question(question) or "Unknown"

    question_type = _extract_question_type(question)

    return {
        "city": city,
        "question": question,
        "outcomes": outcomes,
        "volume": int(vol_num),
        "volume_display": _format_volume(vol_num),
        "date": date_str,
        "question_type": question_type,
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
# STRATEGY 1 (PRIMARY): Gamma /events?tag_slug=daily-temperature
# ---------------------------------------------------------------------------

def strategy_gamma_tag_slug(client: HTTPClient) -> tuple[list[dict], str]:
    """Fetch today's daily-temperature events and flatten nested markets."""
    print("\n" + "=" * 60)
    print("STRATEGY 1 (PRIMARY): Gamma /events?tag_slug=daily-temperature")
    print("=" * 60)

    all_events: list[dict] = []
    offset = 0
    pages = 0

    for _ in range(EVENTS_MAX_PAGES):
        url = (
            f"{GAMMA_EVENTS_URL}?tag_slug={WEATHER_TAG_SLUG}"
            f"&active=true&closed=false&limit={EVENTS_PAGE_SIZE}&offset={offset}"
        )
        data = client.get_json(url)
        if data is None:
            print(f"  [FAIL] Request failed at offset {offset}")
            break

        events = data if isinstance(data, list) else data.get("data", [])
        if not events:
            break

        all_events.extend(events)
        pages += 1
        print(f"  offset={offset}: {len(events)} events (total {len(all_events)})")

        if len(events) < EVENTS_PAGE_SIZE:
            break
        offset += EVENTS_PAGE_SIZE

    if not all_events:
        print("  [FAIL] No events returned by the tag-slug endpoint")
        return [], "tag-slug: no events"

    markets: list[dict] = []
    total_markets = 0
    for ev in all_events:
        event_title = ev.get("title", "") or ev.get("ticker", "")
        for mkt in ev.get("markets", []):
            if not isinstance(mkt, dict):
                continue
            total_markets += 1
            normalized = dict(mkt)
            # Gamma nested markets encode these as JSON strings.
            normalized["outcomes"] = _as_list(mkt.get("outcomes"))
            normalized["outcomePrices"] = _as_list(mkt.get("outcomePrices"))
            normalized["clobTokenIds"] = _as_list(mkt.get("clobTokenIds"))
            q = normalized.get("question", "") or event_title
            if q and is_weather_market(q):
                std = standardize_market(normalized)
                if std:
                    markets.append(std)

    print(f"  Events: {len(all_events)}, embedded markets: {total_markets}, "
          f"weather markets: {len(markets)}")
    return markets, (
        f"Gamma tag-slug ({len(all_events)} events, {total_markets} markets)"
    )


# ---------------------------------------------------------------------------
# STRATEGY 2 (FALLBACK): Gamma /markets volume-sorted
# ---------------------------------------------------------------------------

def strategy_gamma_markets(client: HTTPClient) -> tuple[list[dict], str]:
    """Fallback: query Gamma /markets directly (volume-sorted, max 3 pages)."""
    print("\n" + "=" * 60)
    print("STRATEGY 2 (FALLBACK): Gamma /markets — volume-sorted")
    print("=" * 60)

    all_raw: list[dict] = []
    offset = 0

    for _ in range(MARKETS_MAX_PAGES):
        url = (
            f"{GAMMA_MARKETS_URL}?active=true&closed=false"
            f"&limit={MARKETS_PAGE_SIZE}&order=volume24hr&ascending=false"
            f"&offset={offset}"
        )
        data = client.get_json(url)
        if data is None:
            break

        items = data if isinstance(data, list) else data.get("data", [])
        if not items:
            break

        all_raw.extend(items)
        if len(items) < MARKETS_PAGE_SIZE:
            break
        offset += MARKETS_PAGE_SIZE

    seen: set[str] = set()
    weather: list[dict] = []
    for m in all_raw:
        q = m.get("question", "")
        if not q or q in seen:
            continue
        seen.add(q)
        if is_weather_market(q):
            std = standardize_market(m)
            if std:
                weather.append(std)

    print(f"  Scanned {len(all_raw)} markets, found {len(weather)} weather markets")
    return weather, f"Gamma markets ({len(all_raw)} scanned, {len(weather)} weather)"


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
    result = sorted(by_q.values(), key=lambda x: x.get("volume", 0), reverse=True)
    return result


def save_results(markets: list[dict], source_info: str) -> None:
    """Save markets to _market_prices.json (schema-compatible output)."""
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
                "question_type": m.get("question_type", "unknown"),
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
    """Run the (now two) strategies, aggregate results, save output."""
    print("=" * 60)
    print("POLYMARKET MARKET PRICE FETCHER (optimized)")
    print(f"Time: {datetime.now(timezone.utc).isoformat()}")
    print(f"HTTP client: requests={HAS_REQUESTS}, httpx={HAS_HTTPX}")
    print("=" * 60)

    client = HTTPClient()
    try:
        strategies = [
            ("Gamma tag-slug", strategy_gamma_tag_slug),
            ("Gamma markets", strategy_gamma_markets),
        ]

        all_markets: list[dict] = []
        sources: list[str] = []

        for name, fn in strategies:
            try:
                markets, info = fn(client)
                if markets:
                    print(f"\n  [OK] {name}: {len(markets)} weather markets -- {info}")
                    all_markets.extend(markets)
                    sources.append(info)
                else:
                    print(f"\n  [NO] {name}: No weather markets found -- {info}")
            except Exception as e:  # noqa: BLE001
                print(f"\n  [CRASH] {name}: {e}")
                import traceback
                traceback.print_exc()
    finally:
        client.close()

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

    print("\n[WARN] ALL API STRATEGIES FAILED to find weather markets.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
