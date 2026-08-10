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

        # Skip resolved/settled markets (price at extremes)
        if yes_price > 0.99 or yes_price < 0.01:
            continue

        # Stronger resolved filter: very low price (<2%) with negligible volume
        # → likely a losing bucket in an already-resolved market
        if yes_price < 0.02 and m.get("volume", 0) < 10:
            continue

        # Skip past-date markets (only show today or tomorrow)
        market_date = _extract_market_date(question)
        if market_date is not None and market_date < date.today():
            continue

        opportunities.append({
            "city": city,
            "city_raw": parsed["city_raw"],
            "temp": parsed["temp"],
            "type": parsed["type"],
            "market_prob": market_prob,
            "volume": m.get("volume", 0),
            "volume_display": m.get("volume_display", ""),
            "date": parsed["date"] or m.get("date", ""),
            "question": question,
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
        List of trading opportunities with computed edges, sorted by |edge| desc.
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
        edge = round(bma_prob - market_prob, 1)

        # Determine signal
        # edge = BMA_prob − market_price
        #   edge > 0 → BMA thinks outcome is MORE likely than market is pricing
        #             → market is undervaluing the outcome → BUY
        #   edge < 0 → BMA thinks outcome is LESS likely than market is pricing
        #             → market is overvaluing the outcome → SHORT
        if edge > 0:
            signal = "🟢 BUY"       # bma_prob > market_price → undervalued
        elif edge < 0:
            signal = "🔴 SHORT"     # bma_prob < market_price → overvalued
        else:
            signal = "⚪ FLAT"

        # DEBUG: Show probability comparison for key cities
        if city.lower().startswith("dallas") and temp == 37:
            print(f"[DEBUG EDGE] {city} {temp}°C: "
                  f"BMA μ={mean_c:.1f}°C σ={std_c:.2f}°C → "
                  f"P(round={temp}) = {bma_prob:.1f}% vs "
                  f"Market = {market_prob:.1f}% → "
                  f"Edge = {edge:+.1f}% → Signal = {signal}",
                  file=sys.stderr)

        results.append({
            "city": city,
            "temp": temp,
            "qtype": qtype,
            "bma_prob": bma_prob,
            "market_prob": market_prob,
            "edge": edge,
            "signal": signal,
            "bma_mean": round(mean_c, 1),
            "bma_std": round(std_c, 2),
            "confidence": bma.get("confidence", 0),
            "volume": opp["volume"],
            "volume_display": opp["volume_display"],
            "date": opp["date"],
            "question": opp["question"],
        })

    # Deduplicate by (city, temp, qtype) — keep entry with highest volume.
    # This prevents the same market appearing twice when both lead_days
    # predictions produce similar edges for the same Polymarket question.
    seen: dict[tuple[str, int, str], dict] = {}
    for r in results:
        key = (r["city"].lower(), r["temp"], r["qtype"])
        if key not in seen or r.get("volume", 0) > seen[key].get("volume", 0):
            seen[key] = r
    results = list(seen.values())

    # Sort by absolute edge descending
    results.sort(key=lambda x: abs(x["edge"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def format_edge_table(edges: list[dict]) -> str:
    """Format edge results as a markdown table string."""
    if not edges:
        return "No trading opportunities found (no matching cities between BMA and market data)."

    lines = [
        f"| By | Spill (°C) | BMA % | Marked % | Edge | Handel | BMA μ | BMA σ | Volum |",
        f"|-----|-----------|-------|----------|------|--------|-------|-------|-------|",
    ]

    for e in edges:
        edge_str = f"{e['edge']:+.1f}%"
        lines.append(
            f"| {e['city']} | {e['temp']}°C | {e['bma_prob']:.1f}% | "
            f"{e['market_prob']:.1f}% | {edge_str} | {e['signal']} | "
            f"{e['bma_mean']:.1f}°C | σ={e['bma_std']:.1f} | {e['volume_display']} |"
        )

    return "\n".join(lines)


def format_edge_html_rows(edges: list[dict]) -> str:
    """Format edge results as HTML table rows."""
    if not edges:
        return '<tr><td colspan="8" style="color: var(--text-dim);">Ingen matchende markeder funnet.</td></tr>'

    rows = ""
    for i, e in enumerate(edges[:20]):  # Top 20
        edge_val = e["edge"]
        if abs(edge_val) > 10:
            edge_class = 'style="font-weight:700; color: var(--green);"' if edge_val > 0 else 'style="font-weight:700; color: var(--red);"'
        else:
            edge_class = 'style="color: var(--text-dim);"'

        signal_class = ""
        if "BUY" in e["signal"]:
            signal_class = 'style="color: var(--green); font-weight: 600;"'
        elif "SHORT" in e["signal"]:
            signal_class = 'style="color: var(--red); font-weight: 600;"'

        rows += f"""<tr>
                <td>{i+1}</td>
                <td><strong>{e['city']}</strong></td>
                <td>{e['temp']}°C</td>
                <td>{e['bma_prob']:.1f}%</td>
                <td>{e['market_prob']:.1f}%</td>
                <td {edge_class}>{e['edge']:+.1f}%</td>
                <td {signal_class}>{e['signal']}</td>
                <td style="color: var(--text-dim);">{e['bma_mean']:.1f}°C / σ={e['bma_std']:.1f}</td>
            </tr>"""

    return rows


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

            if is_loser and 1 <= price_cents <= 50:
                # SHORT: sell at price_cents, collect 100c at resolution
                action = "SHORT"
                profit_cents = round(100 - price_cents, 1)
            elif is_winner and 50 <= price_cents <= 95:
                # BUY: buy at price_cents, collect 100c at resolution
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
        "| By | Vinner | Taper | Taper Pris | Profitt | Handling |",
        "|----|--------|-------|-----------|---------|----------|",
    ]

    for r in opportunities:
        action_emoji = "🔴" if r["action"] == "SHORT" else "🟢"
        lines.append(
            f"| {r['city_display']} | {r['winning_temp']}°C | "
            f"{r['losing_temp']}°C @ {r['price_cents']:.1f}c | "
            f"+{r['profit_cents']:.1f}c | "
            f"{action_emoji} {r['action']} {r['losing_temp']}°C |"
        )

    return "\n".join(lines)


def format_resolution_arbitrage_html_rows(opportunities: list[dict]) -> str:
    """Format resolution arbitrage results as HTML table rows."""
    if not opportunities:
        return '<tr><td colspan="6" style="color: var(--text-dim);">Ingen resolusjonsarbitrasje funnet — peak windows har ikke passert enda.</td></tr>'

    rows = ""
    for r in opportunities:
        action_color = "var(--red)" if r["action"] == "SHORT" else "var(--green)"
        action_emoji = "🔴" if r["action"] == "SHORT" else "🟢"
        action_label = f"SHORT {r['losing_temp']}°C" if r["action"] == "SHORT" else f"BUY {r['winning_temp']}°C"

        rows += f"""<tr>
            <td><strong>{r['city_display']}</strong></td>
            <td style="color: var(--green); font-weight: 600;">{r['winning_temp']}°C</td>
            <td>{r['losing_temp']}°C @ <span style="font-family: monospace;">{r['price_cents']:.1f}c</span></td>
            <td style="color: var(--green); font-weight: 700; font-family: monospace;">+{r['profit_cents']:.1f}c</td>
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
          <th>By</th><th>Vinner</th><th>Taper</th><th>Profitt</th><th>Handling</th>
        </tr></thead>
        <tbody>
          {rows_html}
        </tbody>
      </table>
      </div>
    </div>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

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
    print("║   MARKED EDGE — BMA vs Polymarket Arbitrage     ║")
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
        # Print summary stats
        buys = [e for e in edges if e["edge"] > 0]
        shorts = [e for e in edges if e["edge"] < 0]
        big_edges = [e for e in edges if abs(e["edge"]) > 10]

        print(f"  🟢 BUY signals:  {len(buys)}")
        print(f"  🔴 SHORT signals: {len(shorts)}")
        print(f"  ⚡ Edge >10%:    {len(big_edges)}")
        print()

        print("💹 MARKED EDGE — STØRST AVVIK BMA vs MARKED")
        print()
        print(format_edge_table(edges[:15]))
        print()

        if len(edges) > 15:
            print(f"  ... and {len(edges) - 15} more (use --json for full output)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
