#!/usr/bin/env python3
"""
Market Edge Computer — BMA vs Polymarket Price Arbitrage Scanner
================================================================

Reads:
  - _market_prices.json   (fetched Polymarket prices)
  - _model_quality_log.json (BMA ensemble predictions)

Matches cities by name and temperature bucket, computes BMA probability
for each market's temperature outcome, and calculates edge:

    edge = BMA_prob - market_price  (in percentage points)

Positive edge = BMA thinks it's more likely → BUY (undervurdert)
Negative edge = Market thinks it's more likely → SHORT (overvurdert)

Output: sorted list of trading opportunities (largest absolute edge first).

USAGE:
    python _compute_market_edge.py
    python _compute_market_edge.py --min-edge 10
    python _compute_market_edge.py --json   # JSON output
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

# Fix Windows encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(os.path.dirname(os.path.abspath(__file__)))
MARKET_PRICES_FILE = SCRIPT_DIR / "_market_prices.json"
QUALITY_LOG_FILE = SCRIPT_DIR / "_model_quality_log.json"

# ---------------------------------------------------------------------------
# City name aliases for matching Polymarket names → BMA names
# ---------------------------------------------------------------------------
CITY_ALIASES: dict[str, str] = {
    # Polymarket uses "Incheon" for Seoul
    "seoul (incheon)": "Seoul",
    "busan": "Busan",
    # Polymarket uses full names sometimes
    "new york city": "New York",
    "mexico city": "Mexico City",
    "kuala lumpur": "Kuala Lumpur",
    "hong kong": "Hong Kong",
    "cape town": "Cape Town",
    "buenos aires": "Buenos Aires",
    "sao paulo": "Sao Paulo",
    "tel aviv": "Tel Aviv",
    "panama city": "Panama City",
    "san francisco": "San Francisco",
    "los angeles": "Los Angeles",
}

# ---------------------------------------------------------------------------
# US Cities — Fahrenheit display for Polymarket markets
# ---------------------------------------------------------------------------
US_CITIES: set[str] = {
    "dallas", "houston", "atlanta", "new york", "new york city",
    "chicago", "los angeles", "san francisco", "seattle", "miami",
    "denver", "austin", "la", "nyc", "sf",
}

# US state-suffixed cities e.g. "Dallas, TX", "Houston, US"
US_CITY_BASES: set[str] = {
    "dallas", "houston", "atlanta", "new york", "new york city",
    "chicago", "los angeles", "san francisco", "seattle", "miami",
    "denver", "austin",
}


def c_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9.0 / 5.0 + 32.0


def is_us_city(city: str) -> bool:
    """Check if a city name refers to a US market."""
    c = city.lower().strip()
    if c in US_CITIES:
        return True
    # Check base name (strip country code like ", US")
    base = c.split(",")[0].strip()
    if base in US_CITY_BASES:
        return True
    # Check parenthetical stripping e.g. "Seoul (Incheon)"
    base_no_paren = re.sub(r"\s*\(.*?\)\s*", "", base).strip()
    if base_no_paren in US_CITY_BASES:
        return True
    return False


def fmt_temp(temp_c: float | int | str, city: str = "", unit: str = "°C") -> str:
    """Format a temperature value for display, converting to °F if US city.
    
    Args:
        temp_c: Temperature in Celsius (int, float, or string like "?")
        city: City name to check if US
        unit: Override unit (if "°F" is passed explicitly)
    
    Returns:
        Formatted string like "95°F" or "35°C"
    """
    if isinstance(temp_c, str):
        val = temp_c
        return f"{val}{unit}" if unit else val
    if unit == "°F" or (city and is_us_city(city)):
        f = c_to_f(float(temp_c))
        return f"{f:.1f}°F"
    return f"{temp_c}°C"


def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))


def _normalize_city(city: str) -> str:
    """Normalize city name for matching."""
    c = city.strip().lower()
    return CITY_ALIASES.get(c, city.strip())


def _parse_market_question(question: str) -> dict[str, Any] | None:
    """Parse a Polymarket temperature market question to extract city, temp bucket, and type.

    Returns None if it's not a city-specific temperature market.
    """
    # Determine question_type: highest or lowest
    q_lower = question.lower()
    if "highest temperature" in q_lower or "highest temp" in q_lower:
        question_type = "highest"
    elif "lowest temperature" in q_lower or "lowest temp" in q_lower:
        question_type = "lowest"
    else:
        question_type = "unknown"

    # Pattern: "Will the highest temperature in CityName be XX°C on YYYY-MM-DD?"
    m = re.search(
        r"(?:highest|lowest)\s+temperature\s+in\s+"
        r"(.+?)\s+be\s+(\d+)\s*°\s*C\s+"
        r"(?:or\s+(higher|below))?\s*"
        r"(?:on\s+(\d{4}-\d{2}-\d{2}))?",
        question,
    )
    if not m:
        # Fahrenheit patterns: "between XX-YY°F"
        m = re.search(
            r"(?:highest|lowest)\s+temperature\s+in\s+"
            r"(.+?)\s+be\s+(?:between\s+)?(\d+)\s*(?:-|to)\s*(\d+)\s*°\s*F"
            r"(?:\s+on\s+(\d{4}-\d{2}-\d{2}))?",
            question,
        )
        if m:
            city_raw = m.group(1).strip()
            lo_f = int(m.group(2))
            hi_f = int(m.group(3))
            date_str = m.group(4) or ""
            # Convert Fahrenheit to Celsius: C = (F - 32) * 5/9
            lo_c = round((lo_f - 32) * 5 / 9)
            hi_c = round((hi_f - 32) * 5 / 9)
            return {
                "city": _normalize_city(city_raw),
                "city_raw": city_raw,
                "temp": lo_c,  # Use lower bound
                "temp_range": f"{lo_c}-{hi_c}",
                "type": "bucket",  # Treat range as a single bucket
                "date": date_str,
                "question_type": question_type,
            }
        return None

    city_raw = m.group(1).strip()
    temp = int(m.group(2))
    suffix = m.group(3)  # "higher", "below", or None
    date_str = m.group(4) or ""

    qtype = "exact"
    if suffix == "higher":
        qtype = "higher"
    elif suffix == "below":
        qtype = "below"

    return {
        "city": _normalize_city(city_raw),
        "city_raw": city_raw,
        "temp": temp,
        "type": qtype,
        "date": date_str,
        "question_type": question_type,
    }


def _extract_market_date(question: str) -> date | None:
    """Extract the target date from a Polymarket temperature question.

    Handles formats like:
      - "Highest temperature in Shanghai on August 10?"
      - "Lowest temperature in Tokyo on Aug 5?"
      - "on 2026-08-10"

    Returns the market date as a `date` object, or None if unparseable.
    """
    # ISO format: YYYY-MM-DD
    m = re.search(r'(\d{4})-(\d{2})-(\d{2})', question)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass

    # Human-readable: "August 10" / "Aug 10" / "August 10, 2026"
    _MONTH_MAP: dict[str, int] = {
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
        month_name = m.group(1).lower()
        day = int(m.group(2))
        year_str = m.group(3)
        year = int(year_str) if year_str else date.today().year
        month = _MONTH_MAP.get(month_name)
        if month:
            try:
                return date(year, month, day)
            except ValueError:
                return None

    return None


# ---------------------------------------------------------------------------
# Core: Compute Edge
# ---------------------------------------------------------------------------

def compute_bma_prob(mean_c: float, std_c: float, temp: int, qtype: str) -> float:
    """Compute BMA probability for a given temperature bucket.

    Args:
        mean_c: BMA mean temperature in Celsius
        std_c: BMA standard deviation
        temp: Temperature bucket (integer)
        qtype: "exact", "higher", or "below"

    Returns:
        Probability as percentage (0-100).
    """
    if std_c <= 0:
        std_c = 1.0  # fallback

    if qtype == "exact":
        # P(round(actual) == temp) = P(temp-0.5 <= actual < temp+0.5)
        prob = _norm_cdf((temp + 0.5 - mean_c) / std_c) - _norm_cdf((temp - 0.5 - mean_c) / std_c)
    elif qtype == "higher":
        # P(round(actual) >= temp) = P(actual > temp - 0.5) = 1 - CDF(temp - 0.5)
        prob = 1.0 - _norm_cdf((temp - 0.5 - mean_c) / std_c)
    elif qtype == "below":
        # P(round(actual) <= temp) = P(actual < temp + 0.5) = CDF(temp + 0.5)
        prob = _norm_cdf((temp + 0.5 - mean_c) / std_c)
    else:
        prob = _norm_cdf((temp + 0.5 - mean_c) / std_c) - _norm_cdf((temp - 0.5 - mean_c) / std_c)

    return round(prob * 100, 1)


def load_market_prices() -> tuple[list[dict], str]:
    """Load and parse market prices into a list of structured opportunities.

    Returns:
        (opportunities list, fetched_at timestamp)
    """
    if not MARKET_PRICES_FILE.exists():
        print(f"[WARN] Market prices file not found: {MARKET_PRICES_FILE}", file=sys.stderr)
        return [], ""

    raw = json.loads(MARKET_PRICES_FILE.read_text(encoding="utf-8"))
    fetched_at = raw.get("fetched_at", "")
    markets = raw.get("markets", [])

    opportunities: list[dict] = []
    for m in markets:
        question = m.get("question", "")
        parsed = _parse_market_question(question)
        if parsed is None:
            continue

        city = parsed["city"]
        if city.lower() in ("unknown", ""):
            continue

        # Extract market price: probability * 100
        outcomes = m.get("outcomes", [])
        yes_price = None
        for o in outcomes:
            if o.get("label", "").strip().lower() == "yes":
                yes_price = o.get("price")
                break

        if yes_price is None:
            continue

        market_prob = round(float(yes_price) * 100, 1)

        # Mark resolved markets (price at extremes) but still include them for display.
        # Markets at 99%+ or <1% are effectively resolved — the peak window has passed
        # and the actual temperature is known. Show them so users can see results.
        is_resolved = (yes_price > 0.99 or yes_price < 0.01)

        # Very low price (<2%) with negligible volume → likely a losing bucket
        # in an already-resolved market. Still include but mark as resolved.
        if yes_price < 0.02 and m.get("volume", 0) < 10:
            is_resolved = True

        # Extract question type (highest/lowest) — from parsed question or raw market data
        question_type = parsed.get("question_type", m.get("question_type", "unknown"))

        # Only include today's markets — skip past AND future markets
        market_date = _extract_market_date(question)
        today = date.today()
        if market_date is not None:
            if market_date != today:
                continue  # Only today's markets
        else:
            # If we can't parse the date from the question, use the market date field
            raw_date_str = parsed.get("date") or m.get("date", "")
            if raw_date_str and raw_date_str not in ("", "Unknown"):
                try:
                    raw_date = date.fromisoformat(raw_date_str[:10])
                    if raw_date != today:
                        continue  # Only today's markets
                except (ValueError, TypeError):
                    pass  # Can't parse, include anyway (best effort)
            # If no date at all is available, include it (best effort)

        opportunities.append({
            "city": city,
            "city_raw": parsed["city_raw"],
            "temp": parsed["temp"],
            "type": parsed["type"],
            "question_type": question_type,
            "market_prob": market_prob,
            "volume": m.get("volume", 0),
            "volume_display": m.get("volume_display", ""),
            "date": parsed["date"] or m.get("date", ""),
            "question": question,
            "is_resolved": is_resolved,
        })

    return opportunities, fetched_at


def load_bma_predictions() -> dict[str, dict]:
    """Load BMA predictions from quality log for the latest run.

    Prefers lead_days=1 predictions (tomorrow) for market edge comparison.
    lead_days=0 (today) is for model quality tracking only — markets trade
    on tomorrow's temperature, not today's.

    Returns:
        {city_name: {"bma_mean": float, "bma_std": float, ...}, ...}
    """
    if not QUALITY_LOG_FILE.exists():
        print(f"[WARN] Quality log not found: {QUALITY_LOG_FILE}", file=sys.stderr)
        return {}

    raw = json.loads(QUALITY_LOG_FILE.read_text(encoding="utf-8"))
    runs = raw.get("runs", [])

    if not runs:
        return {}

    # Use latest run — prefer multi_day.day2 (lead_days=1 = tomorrow)
    # which is what Polymarket markets trade on.
    # Fall back to predictions (lead_days=0) if day2 not available.
    latest = runs[-1]
    multi_day = latest.get("predictions_multi_day", {})
    day2 = multi_day.get("day2", {}) if multi_day else {}

    if day2:
        predictions = day2
        source = "predictions_multi_day.day2 (lead_days=1)"
    else:
        predictions = latest.get("predictions", {})
        source = "predictions (lead_days=0, fallback)"

    print(f"[INFO] BMA predictions loaded from: {source}", file=sys.stderr)

    result: dict[str, dict] = {}
    for city, pdata in predictions.items():
        result[city] = {
            "bma_mean": pdata.get("bma_mean", 0),
            "bma_std": pdata.get("bma_std", 1.0),
            "confidence": pdata.get("confidence", 0),
            "models": pdata.get("models", 0),
            "strategies": pdata.get("strategies", {}),
        }

    return result


def build_market_lookup() -> dict[tuple[str, int], float]:
    """Build a lookup dict: {(city_lowercase, temp_celsius): market_prob_pct}.

    Used by all-cities dashboard to look up market prices per city row.
    Stores both "cityname" and "cityname, cc" keys for matching.
    """
    market_opps, _ = load_market_prices()
    lookup: dict[tuple[str, int], float] = {}
    for opp in market_opps:
        city_raw = opp["city"].lower().strip()
        prob = opp["market_prob"]
        vol = opp.get("volume", 0)
        
        # Store by raw city name (market format)
        key = (city_raw, opp["temp"])
        if key not in lookup or vol > 0:
            lookup[key] = prob
        
        # Also store without any parenthetical suffix (e.g., "Seoul (Incheon)" -> "Seoul")
        city_no_paren = re.sub(r'\s*\(.*?\)\s*', '', city_raw).strip()
        if city_no_paren != city_raw:
            key2 = (city_no_paren, opp["temp"])
            if key2 not in lookup or vol > 0:
                lookup[key2] = prob
    
    return lookup


# ── Kelly Criterion Position Sizing ──

DEFAULT_BANKROLL = 1000.0  # USD
KELLY_FRACTION = 0.25       # Quarter-Kelly (conservative)

# P1 — Market-price gate: only bet when BMA_prob − market_price ≥ threshold.
# Configurable via the EDGE_THRESHOLD environment variable (default 5pp).
EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))


def compute_kelly_size(
    bma_win_prob: float,      # BMA win probability (0-1)
    market_price_yes: float,  # Polymarket YES price (0-1)
    bankroll: float = DEFAULT_BANKROLL,
    kelly_fraction: float = KELLY_FRACTION,
) -> dict:
    """Compute Kelly-optimal position size for a binary market.

    For Polymarket binary outcomes:
      b = (1 - price) / price  (net odds for buying YES)
      f* = (p*b - q) / b       (full Kelly)
      Recommended = f* * kelly_fraction * bankroll

    Returns dict with 'full_kelly', 'recommended', 'net_odds', 'is_valid'.
    Returns 0 if edge is negative or probability too low.
    """
    price = max(0.01, min(0.99, market_price_yes))  # clamp to avoid div by zero
    p_win = max(0.0, min(1.0, bma_win_prob))
    q_lose = 1.0 - p_win
    net_odds = (1.0 - price) / price

    if net_odds <= 0 or p_win <= 0.5:
        return {"full_kelly": 0.0, "recommended": 0.0, "net_odds": net_odds, "is_valid": False}

    full_kelly = (p_win * net_odds - q_lose) / net_odds
    full_kelly = max(0.0, full_kelly)
    recommended = full_kelly * kelly_fraction * bankroll

    return {
        "full_kelly": round(full_kelly, 4),
        "recommended": round(recommended, 2),
        "net_odds": round(net_odds, 4),
        "is_valid": full_kelly > 0.01,
    }


def compute_edges(
    market_opps: list[dict],
    bma_preds: dict[str, dict],
    min_vol: int = 0,
) -> list[dict]:
    """Compute edge for each market opportunity against BMA predictions.

    Args:
        market_opps: Parsed market opportunities from _market_prices.json
        bma_preds: BMA predictions from quality log
        min_vol: Minimum volume filter (0 = no filter)

    Returns:
        List of trading opportunities with computed edges, sorted by confidence desc.
        No BUY/SHORT/ARBITRAGE signals — pure data display.
    """
    results: list[dict] = []

    def _match_city(market_city: str, bma_cities: dict[str, dict]) -> tuple[str | None, dict | None]:
        """Match market city name to BMA city name, handling country codes.
        
        BMA uses "CityName, CC" or "CityName (SubLocation), CC" format.
        Market uses "CityName" or "CityName (SubLocation)".
        """
        mc = market_city.lower().strip()
        
        # Exact match (case-insensitive)
        for bma_city, bma_data in bma_cities.items():
            if bma_city.lower() == mc:
                return bma_city, bma_data
        
        # Try matching without country code: "Moscow, RU" matches "Moscow"
        for bma_city, bma_data in bma_cities.items():
            bma_base = bma_city.split(",")[0].strip().lower()
            if bma_base == mc:
                return bma_city, bma_data
        
        # Try matching with parenthetical stripped from market city
        # e.g., market "Seoul (Incheon)" → BMA "Seoul (Incheon), KR"
        mc_no_paren = re.sub(r'\s*\(.*?\)\s*', '', mc).strip()
        if mc_no_paren and mc_no_paren != mc:
            for bma_city, bma_data in bma_cities.items():
                bma_base = bma_city.split(",")[0].strip().lower()
                if bma_base == mc_no_paren:
                    return bma_city, bma_data
        
        # Try matching with parenthetical stripped from BMA base
        # e.g., BMA "Seoul (Incheon), KR" → base "seoul (incheon)" but market "seoul"
        for bma_city, bma_data in bma_cities.items():
            bma_base = bma_city.split(",")[0].strip().lower()
            bma_base_no_paren = re.sub(r'\s*\(.*?\)\s*', '', bma_base).strip()
            if bma_base_no_paren == mc:
                return bma_city, bma_data
        
        # Try fuzzy matching: market city is contained in BMA city or vice versa
        # e.g., market "Kuala Lumpur" → BMA "Kuala Lumpur, MY"
        for bma_city, bma_data in bma_cities.items():
            bma_lower = bma_city.lower()
            if mc in bma_lower or bma_lower in mc:
                return bma_city, bma_data
        
        return None, None

    for opp in market_opps:
        market_city = opp["city"]
        matched_city, bma = _match_city(market_city, bma_preds)
        
        if bma is None:
            continue
        
        city = matched_city or market_city

        if min_vol > 0 and opp["volume"] < min_vol:
            continue

        mean_c = bma["bma_mean"]
        std_c = bma["bma_std"]
        temp = opp["temp"]
        qtype = opp["type"]

        bma_prob = compute_bma_prob(mean_c, std_c, temp, qtype)
        market_prob = opp["market_prob"]
        question_type = opp.get("question_type", "unknown")

        # Extract per-strategy spill temperatures from BMA predictions
        strategies = bma.get("strategies", {})
        sigma_spill = strategies.get("sigma", {}).get("spill", "?")
        p5_spill = strategies.get("p5", {}).get("spill", "?")
        mean_spill = strategies.get("mean", {}).get("spill", "?")

        # Edge: raw BMA probability minus market implied probability.
        # Legacy `edge` stays in percentage points for backwards compatibility;
        # the new `edge_frac` (0-1) feeds the P1 price gate.
        market_price = market_prob / 100.0
        bma_prob_frac = bma_prob / 100.0
        edge_frac = bma_prob_frac - market_price
        if 15 < market_prob < 85:
            edge = round(bma_prob - market_prob, 1)
        else:
            edge = 0

        # Kelly sizing
        kelly = compute_kelly_size(bma_prob_frac, market_price)

        # P1 market-price gate: eligible only when edge beats the threshold,
        # quarter-Kelly is positive, and the market is not already resolved.
        eligible = bool(
            not opp.get("is_resolved", False)
            and edge_frac >= EDGE_THRESHOLD
            and kelly.get("is_valid", False)
            and kelly.get("recommended", 0) > 0
        )

        results.append({
            "city": city,
            "temp": temp,
            "qtype": qtype,
            "question_type": question_type,
            "bma_prob": bma_prob,
            "market_prob": market_prob,
            "edge": edge,
            "edge_frac": round(edge_frac, 4),
            "market_price": round(market_price, 4),
            "eligible": eligible,
            "kelly": kelly["recommended"],
            "bma_mean": round(mean_c, 1),
            "bma_std": round(std_c, 2),
            "confidence": bma.get("confidence", 0),
            "sigma_spill": sigma_spill,
            "p5_spill": p5_spill,
            "mean_spill": mean_spill,
            "volume": opp["volume"],
            "volume_display": opp["volume_display"],
            "date": opp["date"],
            "question": opp["question"],
            "is_resolved": opp.get("is_resolved", False),
            "kelly_size": kelly["recommended"],
            "kelly_valid": kelly["is_valid"],
            "net_odds": kelly["net_odds"],
        })

    # Deduplicate by (city, temp, qtype) — keep entry with highest volume.
    seen: dict[tuple[str, int, str], dict] = {}
    for r in results:
        key = (r["city"].lower(), r["temp"], r["qtype"])
        if key not in seen or r.get("volume", 0) > seen[key].get("volume", 0):
            seen[key] = r
    results = list(seen.values())

    # Sort by BMA confidence descending (primary), then edge descending (secondary)
    results.sort(key=lambda x: (x["confidence"], abs(x["edge"])), reverse=True)
    return results


# ---------------------------------------------------------------------------
# P1 + P3 — Market-type-aware Mean(round) bucket picker and price gate
# ---------------------------------------------------------------------------

def _f_bucket_bounds(opp: dict) -> tuple[float, float] | None:
    """Extract native inclusive °F bucket bounds [lo_f, hi_f] from an opportunity."""
    q = str(opp.get("question", ""))
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°\s*F', q, re.IGNORECASE)
    if m:
        return float(m.group(1)), float(m.group(2))
    m2 = re.search(r'(\d+)\s*°\s*F', q, re.IGNORECASE)
    if m2:
        v = float(m2.group(1))
        return v - 0.5, v + 0.5
    return None


def _bucket_label(opp: dict) -> str:
    """Native bucket label for an opportunity (e.g. "86-87°F", "35°C")."""
    q = str(opp.get("question", ""))
    m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°\s*F', q, re.IGNORECASE)
    if m:
        return f"{m.group(1)}-{m.group(2)}°F"
    m2 = re.search(r'(\d+)\s*°\s*F', q, re.IGNORECASE)
    if m2:
        return f"{m2.group(1)}°F"
    m3 = re.search(r'(\d+)\s*°\s*C', q, re.IGNORECASE)
    if m3:
        return f"{m3.group(1)}°C"
    return fmt_temp(opp.get("temp", "?"), opp.get("city", ""))


def _normalize_base(city: str) -> str:
    """Normalize a city key to its base name for matching (strip country/parens)."""
    base = city.strip().lower().split(",")[0].strip()
    return re.sub(r"\s*\(.*?\)\s*", "", base).strip()


def compute_kelly_fraction(bma_win_prob: float, market_price_yes: float) -> float:
    """Standard full-Kelly fraction: f* = (p·b − q)/b with b = (1−price)/price.

    Returns 0.0 when the bet has no positive expectation (p ≤ price).
    Unlike the legacy `compute_kelly_size`, this does NOT reject p < 0.5 —
    a low-probability bet can still have positive expectation at a low price.
    """
    price = max(0.01, min(0.99, market_price_yes))
    p = max(0.0, min(1.0, bma_win_prob))
    q = 1.0 - p
    b = (1.0 - price) / price
    if b <= 0:
        return 0.0
    f = (p * b - q) / b
    return max(0.0, f)


def pick_bucket(mean_c: float, std_c: float, market_opps: list[dict]) -> dict | None:
    """Pick the single market bucket the Mean(round) strategy should bet on.

    Market-type aware (P3):
      - The highest-temperature market is preferred; fall back to other
        question types when a city only has lowest/unknown markets.
      - US °F bucket markets → the native 1°F bucket whose inclusive
        [lo_f, hi_f] range contains the forecast converted to °F
        (fallback: bucket whose midpoint is nearest the °F forecast).
      - exact °C point markets → argmax bucket mass ≈ int(round(mean_c)).
      - threshold markets → only used when no exact bucket exists.
    Returns the chosen market opportunity dict, or None when nothing matches.
    """
    if not market_opps:
        return None

    preferred = [o for o in market_opps if o.get("question_type") == "highest"]
    pool = preferred or list(market_opps)

    # Native °F bucket markets first (US cities).
    f_buckets = [o for o in pool if _f_bucket_bounds(o) is not None]
    if f_buckets:
        f_mean = c_to_f(mean_c)
        best = None
        best_dist = float("inf")
        for o in f_buckets:
            bounds = _f_bucket_bounds(o)
            if bounds is None:
                continue
            lo, hi = bounds
            if lo <= f_mean <= hi:
                return o
            mid = (lo + hi) / 2.0
            dist = abs(mid - f_mean)
            if dist < best_dist:
                best_dist = dist
                best = o
        return best

    # Exact °C point markets → nearest integer bucket to the mean.
    exact = [o for o in pool if o.get("type") in ("exact", None)]
    if exact:
        target = int(round(mean_c))
        best = None
        best_dist = float("inf")
        for o in exact:
            try:
                temp = int(o.get("temp", 0))
            except (TypeError, ValueError):
                continue
            dist = abs(temp - target)
            if dist < best_dist:
                best_dist = dist
                best = o
        return best

    # Threshold markets (only when no exact bucket exists) → max tail probability.
    threshold = [o for o in pool if o.get("type") in ("higher", "below")]
    if threshold:
        best = None
        best_prob = -1.0
        for o in threshold:
            prob = compute_bma_prob(mean_c, std_c, o.get("temp", 0), o.get("type", "exact"))
            if prob > best_prob:
                best_prob = prob
                best = o
        return best

    # Last resort: nearest bucket in the full pool.
    target = int(round(mean_c))
    best = None
    best_dist = float("inf")
    for o in pool:
        try:
            temp = int(o.get("temp", 0))
        except (TypeError, ValueError):
            continue
        dist = abs(temp - target)
        if dist < best_dist:
            best_dist = dist
            best = o
    return best


def compute_mean_spill_bets(
    market_opps: list[dict],
    bma_preds: dict[str, dict],
) -> list[dict]:
    """Compute candidate Mean(round) bets with P1 price gating.

    For each matched city, pick the market-type-aware bucket (P3), compute
    edge = bma_prob − market_price, quarter-Kelly sizing, and mark eligible
    (edge ≥ EDGE_THRESHOLD and quarter-Kelly > 0 and market not resolved).
    """
    by_city: dict[str, list[dict]] = {}
    for opp in market_opps:
        base = _normalize_base(str(opp.get("city", "")))
        if base:
            by_city.setdefault(base, []).append(opp)

    bets: list[dict] = []
    for bma_city, bma in bma_preds.items():
        base = _normalize_base(bma_city)
        opps = by_city.get(base)
        if not opps:
            # Fuzzy containment match (e.g. "Kuala Lumpur" ↔ "Kuala Lumpur, MY").
            for mbase, mlist in by_city.items():
                if base and (base in mbase or mbase in base):
                    opps = mlist
                    break
        if not opps:
            continue

        strategies = bma.get("strategies", {}) or {}
        mean = strategies.get("mean", {}) or {}
        mean_spill = mean.get("spill")
        if mean_spill is None:
            continue

        mean_c = bma.get("bma_mean", 0)
        std_c = bma.get("bma_std", 1.0)

        chosen = pick_bucket(mean_c, std_c, opps)
        if chosen is None:
            continue

        stored_win_prob = mean.get("win_prob")
        if isinstance(stored_win_prob, (int, float)):
            bma_prob = max(0.0, min(1.0, float(stored_win_prob)))
        else:
            bma_prob = compute_bma_prob(
                mean_c, std_c,
                chosen.get("temp", int(round(mean_c))),
                chosen.get("type", "exact"),
            ) / 100.0

        market_prob = chosen.get("market_prob", 0)  # percentage points (0-100)
        market_price = market_prob / 100.0
        edge = bma_prob - market_price
        kelly_frac = compute_kelly_fraction(bma_prob, market_price)
        quarter_kelly_stake = round(kelly_frac * KELLY_FRACTION * DEFAULT_BANKROLL, 2)
        eligible = bool(
            not chosen.get("is_resolved", False)
            and edge >= EDGE_THRESHOLD
            and kelly_frac > 0
        )

        bets.append({
            "city": bma_city,
            "city_display": bma_city.split(",")[0].strip(),
            "spill": mean_spill,
            "bucket_label": _bucket_label(chosen),
            "qtype": chosen.get("type"),
            "question_type": chosen.get("question_type", "unknown"),
            "bma_prob": round(bma_prob, 4),
            "market_price": round(market_price, 4),
            "edge": round(edge, 4),
            "kelly": quarter_kelly_stake,
            "kelly_fraction": round(kelly_frac, 4),
            "eligible": eligible,
            "volume": chosen.get("volume", 0),
            "volume_display": chosen.get("volume_display", ""),
            "date": chosen.get("date", ""),
            "is_resolved": chosen.get("is_resolved", False),
            "bma_mean": round(mean_c, 1),
        })

    bets.sort(key=lambda b: (-int(b["eligible"]), -b["edge"], b["city"]))
    return bets


def compute_eligible_bets() -> list[dict]:
    """Load data and return only the P1-eligible Mean(round) bets (GODE ODDS)."""
    market_opps, _ = load_market_prices()
    if not market_opps:
        return []
    bma_preds = load_bma_predictions()
    if not bma_preds:
        return []
    bets = compute_mean_spill_bets(market_opps, bma_preds)
    return [b for b in bets if b.get("eligible")]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_edge_table(edges: list[dict]) -> str:
    """Format edge results as a markdown table string — data-only, no signals.

    Separates highest and lowest temperature markets into distinct sections.
    Shows per-market BMA probability instead of city-level strategy spills.
    """
    if not edges:
        return "No trading opportunities found (no matching cities between BMA and market data)."

    # Split by question type
    highest = [e for e in edges if e.get("question_type") == "highest"]
    lowest = [e for e in edges if e.get("question_type") == "lowest"]
    other = [e for e in edges if e.get("question_type") not in ("highest", "lowest")]

    lines: list[str] = []
    header = "| By | Dato | Spill | BMA Sanns. | Marked | BMA μ | Volum |"
    sep = "|-----|------|-------|-----------|--------|-------|-------|"

    if highest:
        lines.append("")
        lines.append("🔺 HØYESTE TEMPERATUR")
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for e in highest:
            date_display = e.get("date", "?") or "?"
            city = e["city"]
            lines.append(
                f"| {city} | {date_display} | {fmt_temp(e['temp'], city)} | "
                f"{e['bma_prob']:.1f}% | "
                f"{e['market_prob']:.1f}% | "
                f"{fmt_temp(e['bma_mean'], city)} | {e['volume_display']} |"
            )

    if lowest:
        lines.append("")
        lines.append("🔻 LAVESTE TEMPERATUR")
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for e in lowest:
            date_display = e.get("date", "?") or "?"
            city = e["city"]
            lines.append(
                f"| {city} | {date_display} | {fmt_temp(e['temp'], city)} | "
                f"{e['bma_prob']:.1f}% | "
                f"{e['market_prob']:.1f}% | "
                f"{fmt_temp(e['bma_mean'], city)} | {e['volume_display']} |"
            )

    if other:
        lines.append("")
        lines.append("📊 ANDRE TEMPERATURMARKEDER")
        lines.append("")
        lines.append(header)
        lines.append(sep)
        for e in other:
            date_display = e.get("date", "?") or "?"
            city = e["city"]
            lines.append(
                f"| {city} | {date_display} | {fmt_temp(e['temp'], city)} | "
                f"{e['bma_prob']:.1f}% | "
                f"{e['market_prob']:.1f}% | "
                f"{fmt_temp(e['bma_mean'], city)} | {e['volume_display']} |"
            )

    return "\n".join(lines)


def format_edge_html_rows(edges: list[dict]) -> str:
    """Format edge results as HTML table rows — pure data display, no trading signals.

    Shows per-market BMA probability instead of city-level strategy spills.
    Resolved markets (price > 99% or < 1%) are marked with a checkmark badge.
    """
    if not edges:
        return '<tr><td colspan="8" style="color: var(--text-dim);">Ingen matchende markeder funnet.</td></tr>'

    rows = ""
    for i, e in enumerate(edges[:20]):  # Top 20
        date_display = e.get("date", "?") or "?"
        resolved_badge = ""
        market_prob_style = ""
        if e.get("is_resolved"):
            resolved_badge = ' <span style="color: var(--green); font-size: 0.75rem;" title="Peak window has passed, market is resolved">✅</span>'
            if e["market_prob"] > 99:
                market_prob_style = ' style="color: var(--green); font-weight: 600;"'
            elif e["market_prob"] < 0.01:
                market_prob_style = ' style="color: var(--red); font-weight: 600;"'
        city = e["city"]
        ks = e.get("kelly_size", 0)
        kv = e.get("kelly_valid", False)
        if kv and ks > 0:
            kelly_cell = f'<span style="color:var(--green);font-weight:600;">${ks:.0f}</span>'
        elif ks > 0:
            kelly_cell = f'<span style="color:var(--text-dim);">${ks:.0f}</span>'
        else:
            kelly_cell = '<span style="color:var(--text-dim);">—</span>'
        rows += f"""<tr>
                <td>{i+1}</td>
                <td><strong>{city}</strong>{resolved_badge}</td>
                <td>{date_display}</td>
                <td>{fmt_temp(e['temp'], city)}</td>
                <td>{e['bma_prob']:.1f}%</td>
                <td{market_prob_style}>{e['market_prob']:.1f}%</td>
                <td style="color: var(--text-dim);">{fmt_temp(e['bma_mean'], city)}</td>
                <td style="color: var(--text-dim);">{e.get('volume_display', '')}</td>
                <td>{kelly_cell}</td>
            </tr>"""

    return rows


def split_edges_by_type(edges: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split edges into highest, lowest, and other markets."""
    highest = [e for e in edges if e.get("question_type") == "highest"]
    lowest = [e for e in edges if e.get("question_type") == "lowest"]
    other = [e for e in edges if e.get("question_type") not in ("highest", "lowest")]
    return highest, lowest, other


def build_market_type_section_html(
    edges: list[dict], title: str, emoji: str, color: str, n_show: int = 20,
    peak_data: dict | None = None,
) -> str:
    """Build an HTML section for a specific market type (highest or lowest).

    Includes the full HTML table with header, not just rows.
    Now includes live peak detection columns: 📡 Foreløpig Peak, 📈 Trend, ⚡ Peak Status.
    When peak_data is not None, pre-fills cells from pipeline data (no JS fetch needed).
    When peak_data is None, shows manual fetch button (all-cities mode).
    """
    if not edges:
        return ""
    embed_peaks = peak_data is not None
    if peak_data is None:
        peak_data = {}

    rows_html = ""
    for i, e in enumerate(edges[:n_show]):
        date_display = e.get("date", "?") or "?"
        resolved_badge = ""
        market_prob_style = ""
        if e.get("is_resolved"):
            resolved_badge = ' <span style="color: var(--green); font-size: 0.75rem;" title="Peak window has passed, market is resolved">✅</span>'
            if e["market_prob"] > 99:
                market_prob_style = ' style="color: var(--green); font-weight: 600;"'
            elif e["market_prob"] < 0.01:
                market_prob_style = ' style="color: var(--red); font-weight: 600;"'
        # Compute city slug for peak detection IDs
        city = e["city"]
        city_slug = re.sub(r"[^a-zA-Z0-9]+", "-", city).lower().strip("-")
        temp = e["temp"]
        market_prob = e.get("market_prob", 0)

        # Pre-fill peak cell from pipeline data if available
        pip_peak = peak_data.get(city)
        if isinstance(pip_peak, (int, float)):
            peak_cell = f'📡 {pip_peak:.1f}°C ✅'
        else:
            peak_cell = '⏳'

        # Kelly cell
        ks = e.get("kelly_size", 0)
        kv = e.get("kelly_valid", False)
        if kv and ks > 0:
            kelly_cell = f'<span style="color:var(--green);font-weight:600;">${ks:.0f}</span>'
        elif ks > 0:
            kelly_cell = f'<span style="color:var(--text-dim);">${ks:.0f}</span>'
        else:
            kelly_cell = '<span style="color:var(--text-dim);">—</span>'

        rows_html += f"""<tr>
                <td>{i+1}</td>
                <td><strong>{city}</strong>{resolved_badge}</td>
                <td>{date_display}</td>
                <td>{fmt_temp(temp, city)}</td>
                <td>{e['bma_prob']:.1f}%</td>
                <td{market_prob_style}>{e['market_prob']:.1f}%</td>
                <td style="color: var(--text-dim);">{fmt_temp(e['bma_mean'], city)}</td>
                <td style="color: var(--text-dim);">{e.get('volume_display', '')}</td>
                <td>{kelly_cell}</td>
                <td class="col-peak" id="peak-{city_slug}-{temp}" data-spill="{temp}" data-market="{market_prob}">{peak_cell}</td>
                <td class="col-trend" id="trend-{city_slug}-{temp}">—</td>
                <td class="col-spark">⚡ —</td>
            </tr>"""

    # Only show fetch button when NOT called from quality report (peak_data is None)
    if embed_peaks:
        fetch_section = ''
    else:
        fetch_section = f"""
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Sammenligner BMA-ensemblets prediksjoner mot Polymarket-priser.
        Sortert etter BMA-konfidens (høyest først). Ingen trading-signaler — ren data.
        Trykk <strong>🔄 Hent Live Peak</strong> for sanntids temperatur-data.
      </p>
      <div style="text-align: center; margin-bottom: 12px;">
        <button class="live-btn" onclick="fetchLivePeak()" id="fetch-btn">🔄 Hent Live Peak</button>
        <span class="live-status" id="fetch-status"></span>
        <span class="live-updated" id="live-updated"></span>
      </div>"""

    return f"""
    <div class="section" style="border-color: {color};">
      <h2>{emoji} {title} <span style="color: var(--text-dim); font-size: 0.8rem;">({len(edges)} markeder)</span></h2>
      {fetch_section}
      <div style="overflow-x: auto;">
     <table>
       <thead><tr><th>#</th><th>By</th><th>Dato</th><th>Spill</th><th>BMA Sanns.</th><th>Marked</th><th>BMA μ</th><th>Volum</th><th>Kelly ($)</th><th>📡 Foreløpig Peak</th><th>📈 Trend</th><th>⚡ Peak Status</th></tr></thead>
       <tbody>{rows_html}</tbody>
     </table>
     </div>
   </div>"""


# ---------------------------------------------------------------------------
# Resolution Arbitrage: Post-Peak Market Scanner
# ---------------------------------------------------------------------------

def compute_resolution_arbitrage(
    quality_log_runs: list[dict] | None = None,
) -> list[dict]:
    """Scan for markets still open AFTER peak window has passed and actual temp is known.

    For each city where:
      1. Market date is today AND peak window has passed (>1 hour ago)
      2. We have actual_peak from the quality log
      3. A market bucket (losing or winning) is still mispriced

    Returns a list of arbitrage opportunities.
    """
    results: list[dict] = []

    # Load quality log if not provided
    if quality_log_runs is None:
        if not QUALITY_LOG_FILE.exists():
            return results
        raw = json.loads(QUALITY_LOG_FILE.read_text(encoding="utf-8"))
        quality_log_runs = raw.get("runs", [])

    if not quality_log_runs:
        return results

    # Load market prices
    market_opps, _ = load_market_prices()
    if not market_opps:
        return results

    # Get latest run's predictions for city metadata (tz, peak window, actual_peak)
    latest = quality_log_runs[-1]
    predictions = latest.get("predictions", {})
    today = date.today()

    # Group markets by city for quick lookup
    markets_by_city: dict[str, list[dict]] = {}
    for opp in market_opps:
        city_lower = opp["city"].lower().strip()
        markets_by_city.setdefault(city_lower, []).append(opp)

    for bma_city, pdata in predictions.items():
        # Extract metadata
        tz_str = pdata.get("_tz", "UTC")
        peak_start = pdata.get("_peak_hour_start", 14)
        peak_end = pdata.get("_peak_hour_end", 17)
        target_date_str = pdata.get("_target_date", "")

        # Check if market date is today
        try:
            target_date = date.fromisoformat(target_date_str) if target_date_str else None
        except ValueError:
            target_date = None

        if target_date is None or target_date != today:
            continue

        # Check if peak window has passed (>1 hour ago in local timezone)
        try:
            from zoneinfo import ZoneInfo
        except ImportError:
            from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

        try:
            local_now = datetime.now(ZoneInfo(tz_str))
        except Exception:
            continue

        # Peak window: local hour range [peak_start, peak_end]
        # "Passed" means current local hour > peak_end (peak ended)
        # We require >1 hour after peak_end for certainty
        if local_now.hour <= peak_end:
            continue

        # Peak window has passed — check if we have actual_peak
        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        actual_peak = sigma.get("actual_peak")

        if actual_peak is None:
            continue

        # Only show if BMA confidence > 80%
        confidence = pdata.get("confidence", 0)
        if confidence < 0.80:
            continue

        try:
            actual_val = float(actual_peak)
        except (TypeError, ValueError):
            continue

        # Determine which bucket WON
        winning_temp = round(actual_val)

        # Find markets for this city
        city_base = bma_city.split(",")[0].strip().lower()
        market_opps_for_city = markets_by_city.get(city_base, [])
        if not market_opps_for_city:
            # Try with parenthetical removed
            city_no_paren = re.sub(r'\s*\(.*?\)\s*', '', city_base).strip()
            market_opps_for_city = markets_by_city.get(city_no_paren, [])
        if not market_opps_for_city:
            # Try exact BMA city name (lowercase)
            market_opps_for_city = markets_by_city.get(bma_city.lower(), [])

        for opp in market_opps_for_city:
            temp = opp["temp"]
            qtype = opp["type"]
            price = opp["market_prob"]  # in percentage points (0-100)
            price_cents = price  # already in cents (0-100)

            # Only consider "exact" type buckets for resolution arbitrage
            if qtype != "exact":
                continue

            is_winner = (temp == winning_temp)
            is_loser = not is_winner

            action = None
            profit_cents = 0.0

            if is_loser and 1 < price_cents <= 50:
                # SHORT: losing bucket still has value (>1c, not yet resolved)
                action = "SHORT"
                profit_cents = round(100 - price_cents, 1)
            elif is_winner and 1 < price_cents < 50:
                # BUY: winning bucket undervalued (1-49c = genuine arb)
                action = "BUY"
                profit_cents = round(100 - price_cents, 1)
            else:
                continue

            results.append({
                "city": bma_city,
                "city_display": bma_city.split(",")[0].strip(),
                "actual_peak": actual_val,
                "winning_temp": winning_temp,
                "losing_temp": temp,
                "temp": temp,
                "price_cents": price_cents,
                "action": action,
                "profit_cents": profit_cents,
                "volume": opp.get("volume", 0),
                "volume_display": opp.get("volume_display", ""),
                "question": opp.get("question", ""),
                "tz": tz_str,
                "local_hour": local_now.hour,
                "peak_end": peak_end,
                "is_winner": is_winner,
                "resolution_probability": "99% sannsynlig resolved",
                "confidence": confidence,
            })

    # Deduplicate by (city, temp, action)
    seen: dict[tuple[str, int, str], dict] = {}
    for r in results:
        key = (r["city"], r["temp"], r["action"])
        if key not in seen or r.get("volume", 0) > seen[key].get("volume", 0):
            seen[key] = r
    results = list(seen.values())

    # Sort: SHORT first (sell overpriced losers), then BUY (buy underpriced winners),
    # then by profit descending
    results.sort(key=lambda x: (0 if x["action"] == "SHORT" else 1, -x["profit_cents"]))
    return results


def format_resolution_arbitrage_table(opportunities: list[dict]) -> str:
    """Format resolution arbitrage results as markdown table."""
    if not opportunities:
        return ""

    lines = [
        "| By | Vinner | Taper | Taper Pris | Profitt | Sannsynlighet | Handling |",
        "|----|--------|-------|-----------|---------|---------------|----------|",
    ]

    for r in opportunities:
        action_emoji = "🔴" if r["action"] == "SHORT" else "🟢"
        res_prob = r.get("resolution_probability", "99% sannsynlig resolved")
        city = r.get("city_display", r.get("city", ""))
        lines.append(
            f"| {city} | {fmt_temp(r['winning_temp'], city)} | "
            f"{fmt_temp(r['losing_temp'], city)} @ {r['price_cents']:.1f}c | "
            f"+{r['profit_cents']:.1f}c | "
            f"{res_prob} | "
            f"{action_emoji} {r['action']} {fmt_temp(r['losing_temp'], city)} |"
        )

    return "\n".join(lines)


def format_resolution_arbitrage_html_rows(opportunities: list[dict]) -> str:
    """Format resolution arbitrage results as HTML table rows."""
    if not opportunities:
        return '<tr><td colspan="7" style="color: var(--text-dim);">Ingen resolusjonsarbitrasje funnet — peak windows har ikke passert enda.</td></tr>'

    rows = ""
    for r in opportunities:
        action_color = "var(--red)" if r["action"] == "SHORT" else "var(--green)"
        action_emoji = "🔴" if r["action"] == "SHORT" else "🟢"
        city = r.get("city_display", r.get("city", ""))
        action_label = f"SHORT {fmt_temp(r['losing_temp'], city)}" if r["action"] == "SHORT" else f"BUY {fmt_temp(r['winning_temp'], city)}"
        res_prob = r.get("resolution_probability", "99% sannsynlig resolved")

        rows += f"""<tr>
            <td><strong>{city}</strong></td>
            <td style="color: var(--green); font-weight: 600;">{fmt_temp(r['winning_temp'], city)}</td>
            <td>{fmt_temp(r['losing_temp'], city)} @ <span style="font-family: monospace;">{r['price_cents']:.1f}c</span></td>
            <td style="color: var(--green); font-weight: 700; font-family: monospace;">+{r['profit_cents']:.1f}c</td>
            <td style="color: var(--green); font-weight: 600;">{res_prob}</td>
            <td style="color: {action_color}; font-weight: 600;">{action_emoji} {action_label}</td>
        </tr>"""

    return rows


def format_resolution_arbitrage_summary_html(opportunities: list[dict]) -> str:
    """Build a full HTML section for resolution arbitrage."""
    if not opportunities:
        return """<div class="section">
      <h2>💰 RESOLUTION ARBITRAGE — Gratis Penger</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Ingen resolusjonsarbitrasje funnet. Peak-vinduer har ikke passert, eller ingen faktiske temperaturer er bekreftet enda.
      </p>
    </div>"""

    shorts = [r for r in opportunities if r["action"] == "SHORT"]
    buys = [r for r in opportunities if r["action"] == "BUY"]
    total_profit = sum(r["profit_cents"] for r in opportunities)

    rows_html = format_resolution_arbitrage_html_rows(opportunities)

    return f"""<div class="section" style="border-color: rgba(210,153,29,0.3);">
      <h2>💰 RESOLUTION ARBITRAGE — Gratis Penger</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 8px;">
        Markeder der peak-vinduet har passert og faktisk temperatur er kjent,
        men markedet handler fortsatt som om utfallet er usikkert.
      </p>
      <div style="display: flex; gap: 16px; margin-bottom: 12px; flex-wrap: wrap;">
        <span style="color: var(--green); font-weight: 600;">🟢 {len(buys)} BUY</span>
        <span style="color: var(--red); font-weight: 600;">🔴 {len(shorts)} SHORT</span>
        <span style="color: var(--orange); font-weight: 700;">💰 Total profitt: +{total_profit:.1f}c per 100c</span>
      </div>
      <div style="overflow-x: auto;">
      <table>
        <thead><tr>
          <th>By</th><th>Vinner</th><th>Taper</th><th>Profitt</th><th>Sannsynlighet</th><th>Handling</th>
        </tr></thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# Safe Winners: Near-Resolved Markets with High Profit
# ---------------------------------------------------------------------------

def build_safe_winners_html_section(edges: list[dict]) -> str:
    """Build HTML section highlighting markets at 85-99% where BMA strongly agrees.

    These are "practically resolved" markets — the peak has happened, the outcome
    is ~95%+ certain via BMA, and the market is trading at 85-95%. These offer
    5-15% safe profit with very low risk.

    Also includes Kelly sizing to show optimal position per market.
    """
    safe = [
        e for e in edges
        if 85 <= e["market_prob"] <= 99
        and not e.get("is_resolved")
        and e.get("bma_prob", 0) >= e["market_prob"]
    ]
    if not safe:
        return ""

    safe.sort(key=lambda e: e.get("kelly_size", 0), reverse=True)

    total_kelly = sum(e.get("kelly_size", 0) for e in safe)
    avg_return = sum((100 - e["market_prob"]) for e in safe) / len(safe)

    rows = ""
    for i, e in enumerate(safe[:10]):
        city = e["city"]
        ks = e.get("kelly_size", 0)
        ret = round((100 - e["market_prob"]), 1)
        rows += f"""<tr>
                <td>{i+1}</td>
                <td><strong>{city}</strong></td>
                <td>{fmt_temp(e['temp'], city)}</td>
                <td style="color:var(--green);font-weight:600;">{e['bma_prob']:.1f}%</td>
                <td style="color:var(--green);font-weight:600;">{e['market_prob']:.1f}%</td>
                <td style="color:var(--green);font-weight:700;">+{ret:.1f}%</td>
                <td style="color:var(--green);font-weight:600;">${ks:.0f}</td>
            </tr>"""

    return f"""<div class="section" style="border-color: rgba(63,185,80,0.5);">
      <h2>🟢 SIKRE VINNERE — Near-Resolved Safe Profit</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 8px;">
        Markeder som handles til 85-99% der BMA er enig eller sterkere.
        Disse gir {avg_return:.0f}% snitt avkastning med svært lav risiko.
        15% på en sikker bet er bedre enn 80% på en usikker.
      </p>
      <div style="display: flex; gap: 16px; margin-bottom: 12px; flex-wrap: wrap;">
        <span style="color: var(--green); font-weight: 600;">🔒 {len(safe)} sikre markeder</span>
        <span style="color: var(--green); font-weight: 600;">📈 Snitt: +{avg_return:.0f}%</span>
        <span style="color: var(--purple); font-weight: 700;">💰 Total Kelly: ${total_kelly:.0f}</span>
      </div>
      <div style="overflow-x: auto;">
      <table>
        <thead><tr><th>#</th><th>By</th><th>Spill</th><th>BMA Sanns.</th><th>Marked</th><th>Avkastning</th><th>Kelly ($)</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
    </div>"""

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Compute BMA vs Polymarket edge for temperature markets",
    )
    parser.add_argument("--min-edge", type=float, default=0,
                        help="Minimum absolute edge to show (default: 0 = show all)")
    parser.add_argument("--min-vol", type=int, default=0,
                        help="Minimum volume to include (default: 0)")
    parser.add_argument("--json", action="store_true",
                        help="Output as JSON instead of table")
    parser.add_argument("--resolution-arb", action="store_true",
                        help="Show resolution arbitrage opportunities (post-peak)")
    args = parser.parse_args()

    print("╔══════════════════════════════════════════════════╗")
    print("║   MARKEDSSAMMENLIGNING — BMA vs Polymarket      ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    if args.resolution_arb:
        # Resolution arbitrage mode
        print("💰 RESOLUTION ARBITRAGE — Post-Peak Market Scanner")
        print()
        opportunities = compute_resolution_arbitrage()
        if not opportunities:
            print("  No resolution arbitrage opportunities found.")
            print("  (Peak windows have not passed, or no actual temps confirmed yet.)")
            return 0
        print(f"  Opportunities found: {len(opportunities)}")
        print()
        print(format_resolution_arbitrage_table(opportunities))
        print()
        return 0

    # Load data
    market_opps, fetched_at = load_market_prices()
    if not market_opps:
        print("[ERROR] No market data available. Run _fetch_market_prices.py first.")
        return 1

    print(f"  Market prices loaded: {len(market_opps)} city-temperature markets")
    print(f"  Fetched at: {fetched_at}")

    bma_preds = load_bma_predictions()
    if not bma_preds:
        print("[ERROR] No BMA predictions available. Run _model_quality_tracker.py first.")
        return 1

    print(f"  BMA predictions loaded: {len(bma_preds)} cities")
    print()

    # Compute edges
    edges = compute_edges(market_opps, bma_preds, min_vol=args.min_vol)

    # Filter by min edge
    if args.min_edge > 0:
        edges = [e for e in edges if abs(e["edge"]) >= args.min_edge]

    print(f"  Matching opportunities: {len(edges)}")
    print()

    if args.json:
        output = {
            "computed_at": datetime.now(timezone.utc).isoformat(),
            "market_prices_fetched_at": fetched_at,
            "total_opportunities": len(edges),
            "opportunities": edges,
        }
        print(json.dumps(output, indent=2, ensure_ascii=False))
    else:
        # Print summary stats (data only, no trading signals)
        print(f"  📊 Markeder matchet: {len(edges)}")
        print()

        print("📊 MARKEDSSAMMENLIGNING — BMA vs Polymarket")
        print("  (sortert etter BMA-konfidens)")
        print()
        print(format_edge_table(edges[:15]))
        print()

        if len(edges) > 15:
            print(f"  ... and {len(edges) - 15} more (use --json for full output)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
