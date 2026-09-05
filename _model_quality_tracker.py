#!/usr/bin/env python3
"""
Model Quality Tracker — automated BMA ensemble quality monitoring.

Designed for GitHub Actions multi-run pipeline:
    --mode daily_bma     06:00 UTC — Run BMA for ALL 51 cities,
                                     lead_days=0 (today) AND lead_days=1 (tomorrow).
                                     Semaphore(5) protects against rate limits.
    --mode hourly_check  07:00-22:00 UTC — Check top 5 temps, detect peaks
    --mode daily_close   23:00 UTC — Finalize ALL 51 cities, compare 3 strategies.
                                     Resolves TODAY's predictions against TODAY's
                                     actual archive data (same-day model).
    --mode full_report   Generate comprehensive markdown report from all history

Each run is ~4 minutes for full 51×2 cities. State is persisted in _model_quality_log.json.

DO NOT MODIFY weather_monitor_cli.py — this is a standalone consumer.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Path setup — ensure we can import from the polymarket-arb-bot package
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# Fix Windows encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Project imports — READ from existing modules, do NOT modify them
# ---------------------------------------------------------------------------
from weather_monitor_cli import (  # type: ignore[import-not-found]
    WeatherAnalyzer,
    LocationManager,
    detect_peak_state,
    SavedLocation,
    compute_kelly,
    check_correlations,
)

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    import structlog
    log = structlog.get_logger(__name__)
except ImportError:
    import logging
    log = logging.getLogger(__name__)  # type: ignore[assignment]
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# =============================================================================
# Constants
# =============================================================================

LOG_FILE = Path(_SCRIPT_DIR) / "_model_quality_log.json"
REPORT_FILE = Path(_SCRIPT_DIR) / "_quality_report.md"
RAPID_PEAK_LOG = Path(_SCRIPT_DIR) / "_rapid_peak_log.json"
PEAK_VERIFICATION_LOG = Path(_SCRIPT_DIR) / "_peak_verification_log.json"
MAX_LOG_DAYS = 90  # Keep last 90 days in log
MAX_OBS_HISTORY = 144  # Max observations per city (~12 hours at 5-min, ample for GH)
MAX_RAPID_RUNTIME_HOURS = 4  # Max runtime for rapid polling (fits GH 6h limit)
RAPID_POLL_INTERVAL_MINUTES = 3  # Poll every 3 min during peak windows
MIN_SAMPLE = int(os.environ.get("MIN_SAMPLE", "5"))  # Min resolved bets before a per-city rate is shown

# Open-Meteo API endpoints
CURRENT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


# =============================================================================
# Data Classes
# =============================================================================

@dataclass
class CityPrediction:
    """BMA prediction for a single city on a given target date."""
    city: str
    lat: float
    lon: float
    tz: str
    target_date: str
    suggested_spill: int       # Sigma-adjusted optimal BUY level (°C), statistically derived
    bma_mean: float
    bma_std: float             # Estimated std from P5-P95 range (normal approx)
    p5: float
    p95: float
    confidence: float          # overall_confidence from ConfidenceResult
    model_count: int
    best_bucket_label: str
    best_bucket_prob: float
    peak_hour_start: int = 14
    peak_hour_end: int = 16

    # Optimal strategy fields (sigma-adjusted, P5-based, mean-based)
    optimal_k: float = 0.5
    sigma_win_prob: float = 0.69
    p5_spill: int = 0
    mean_spill: int = 0
    p5_win_prob: float = 0.95
    mean_win_prob: float = 0.5


# =============================================================================
# Helpers
# =============================================================================

def _parse_bucket_hi(label: str) -> int:
    """Parse the upper bound from a bucket label like '32-34°C' or '34.0-36.0°C' → 34 or 36."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m:
        return int(float(m.group(2)))
    m2 = re.search(r"<\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m2:
        return int(float(m2.group(1)))
    m3 = re.search(r">\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m3:
        return int(float(m3.group(1)))
    return -1


def _parse_bucket_lo(label: str) -> int:
    """Parse the lower bound from a bucket label like '32-34°C' or '34.0-36.0°C' → 32 or 34."""
    m = re.search(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m:
        return int(float(m.group(1)))
    m2 = re.search(r"<\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m2:
        return max(0, int(float(m2.group(1))) - 2)
    m3 = re.search(r">\s*(\d+(?:\.\d+)?)\s*°\s*C", label)
    if m3:
        return int(float(m3.group(1)))
    return 0


def _get_city_historical_winrate(city_name: str) -> float | None:
    """Read the historical win-rate for a city from ``_model_quality_log.json``.

    Looks at all resolved runs and counts sigma-strategy WINS / total resolved.
    Returns None if fewer than 3 resolved days exist.
    """
    log_data = _load_log()
    runs = log_data.get("runs", [])
    wins = 0
    total = 0
    for run in runs:
        preds = run.get("predictions", {})
        pdata = preds.get(city_name)
        if pdata is None:
            continue
        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        result = sigma.get("result")
        if result in ("WIN", "LOSS"):
            total += 1
            if result == "WIN":
                wins += 1
    if total < 3:
        return None
    return wins / total


def _compute_optimal_spill(
    mean_c: float, std_c: float, confidence: float, p5_c: float,
    city_name: str = "",
) -> dict[str, float | int]:
    """Compute optimal bet levels using BMA statistics with dynamic k calibration.

    For Polymarket "Highest temp round(T) == spill?" markets:
      P(win) = 1 - Φ((T - μ)/σ)  assuming normal distribution.

    Strategy: Sigma-Adjusted Bet Level
      suggested_spill = int(μ - k × σ)

    Where k is the risk-adjustment factor, dynamically set by confidence
    AND calibrated against historical city win-rate (PRI 2):

      | k   | Win Prob | Style                          |
      |-----|----------|--------------------------------|
      | 0   | 50%      | At mean — balanced, 50/50      |
      | 0.3 | 62%      | Aggressive (high conf)         |
      | 0.5 | 69%      | Conservative — good risk/reward|
      | 0.7 | 76%      | Cautious (low conf)            |
      | 0.84| 80%      | Safe — high confidence         |
      | 1.0 | 84%      | Very safe — 1σ below mean      |

    Dynamic k calibration (PRI 2):
      - If actual win-rate < predicted win-prob → overconfident → increase k
      - If actual win-rate > predicted → underconfident → decrease k
    Also computes P5-based (ultra-conservative ~95%) and mean-based (50%) for comparison.
    """
    # Dynamic k based on confidence
    if confidence > 0.80:
        k = 0.3   # High confidence → aggressive
    elif confidence > 0.70:
        k = 0.5   # Medium → balanced
    else:
        k = 0.7   # Low confidence → conservative

    sigma_spill = int(round(mean_c - k * std_c))
    p5_spill = int(round(p5_c))
    mean_spill = int(round(mean_c))

    # Win probability under normal approximation: P(round(temp) == spill)
    # Polymarket resolves to round(actual_temp) == spill_bucket.
    # Correct probability: P(spill - 0.5 <= temp < spill + 0.5) = Φ(hi) - Φ(lo)
    def win_prob(t: float, mu: float, sigma: float) -> float:
        if sigma <= 0:
            return 0.5 if abs(mu - t) < 0.5 else 0.0
        sqrt2 = 1.4142135623730951
        z_hi = (t + 0.5 - mu) / sigma
        z_lo = (t - 0.5 - mu) / sigma
        phi_hi = 0.5 * (1 + math.erf(z_hi / sqrt2))
        phi_lo = 0.5 * (1 + math.erf(z_lo / sqrt2))
        return round(max(0.0, phi_hi - phi_lo), 3)

    predicted_wp = win_prob(sigma_spill, mean_c, std_c)

    # ---- Dynamic k calibration (PRI 2) ----
    if city_name:
        historical_wr = _get_city_historical_winrate(city_name)
        if historical_wr is not None and predicted_wp > 0:
            calibration_factor = historical_wr / predicted_wp
            k = k * (2.0 - calibration_factor)  # Adjust k
            k = max(0.1, min(1.5, k))  # Clamp
            # Recompute sigma_spill with calibrated k
            sigma_spill = int(round(mean_c - k * std_c))
            predicted_wp = win_prob(sigma_spill, mean_c, std_c)

    return {
        "recommended": sigma_spill,
        "k_used": round(k, 2),
        "sigma_spill": sigma_spill,
        "p5_spill": p5_spill,
        "mean_spill": mean_spill,
        "sigma_win_prob": predicted_wp,
        "p5_win_prob": win_prob(p5_spill, mean_c, std_c),
        "mean_win_prob": win_prob(mean_spill, mean_c, std_c),
    }


def _load_log() -> dict:
    """Load existing quality log or return empty structure."""
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {
        "runs": [],
        "cumulative": {
            "total_days": 0,
            "total_predictions": 0,
            "sigma_wins": 0,
            "sigma_losses": 0,
            "p5_wins": 0,
            "p5_losses": 0,
            "mean_wins": 0,
            "mean_losses": 0,
        },
    }


def _save_log(data: dict) -> None:
    """Thread-safe log write with 90-day rotation."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    runs = data.get("runs", [])
    if len(runs) > MAX_LOG_DAYS:
        data["runs"] = runs[-MAX_LOG_DAYS:]
    LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today_iso() -> str:
    return date.today().isoformat()


# =============================================================================
# Timezone-Region Mapping & Active Window Helpers
# =============================================================================

# City → timezone lookups are built dynamically from weather_monitor_defaults.json
# (the single source of truth for the 51 canonical cities) instead of a stale
# hardcoded map. Region is derived from the IANA tz string and the UTC offset
# is computed live via ZoneInfo so DST is respected.


def _load_default_locations() -> list[dict]:
    """Load the canonical city list from weather_monitor_defaults.json."""
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    if defaults_path.exists():
        try:
            data = json.loads(defaults_path.read_text(encoding="utf-8"))
            locs = data.get("default_locations", [])
            if isinstance(locs, list):
                return locs
        except (json.JSONDecodeError, OSError):
            pass
    return []


def _city_tz_lookup() -> dict[str, str]:
    """Map city name → tz string, keyed by both the full name and base name."""
    lookup: dict[str, str] = {}
    for loc in _load_default_locations():
        name = str(loc.get("name", "")).strip()
        tz = str(loc.get("tz", "")).strip()
        if not name or not tz:
            continue
        lookup[name] = tz
        base = name.split(",")[0].strip()
        lookup.setdefault(base, tz)
    return lookup


def _region_from_tz(tz: str) -> str:
    """Derive a reporting region label from an IANA timezone string."""
    if not tz:
        return "Other"
    if tz in ("Asia/Riyadh", "Asia/Jerusalem", "Asia/Dubai", "Asia/Tehran"):
        return "MIDDLE_EAST"
    if tz.startswith("America/Argentina") or tz in (
        "America/Sao_Paulo", "America/Lima", "America/Santiago",
    ):
        return "SOUTH_AM"
    if tz.startswith("America/"):
        return "AMERICAS"
    if tz.startswith("Europe/"):
        return "EUROPE"
    if tz.startswith("Africa/"):
        return "AFRICA"
    if tz.startswith(("Pacific/", "Australia/")):
        return "OCEANIA"
    if tz.startswith("Asia/"):
        return "ASIA"
    return "Other"


def _get_utc_offset_for_city(city_name: str, tz_str: str = "UTC") -> float:
    """Get the city's current UTC offset in hours.

    Prefers the tz from the canonical defaults file, falls back to the passed
    tz string, and computes the offset live via ZoneInfo (DST-aware).
    """
    tz = _city_tz_lookup().get(city_name)
    if not tz:
        tz = _city_tz_lookup().get(city_name.split(",")[0].strip(), "")
    tz = tz or tz_str or "UTC"
    try:
        tz_obj = ZoneInfo(tz)
        offset = datetime.now(tz_obj).utcoffset()
        if offset is not None:
            return offset.total_seconds() / 3600.0
    except Exception:
        pass
    return 0.0


def _get_region_for_city(city_name: str) -> str:
    """Get the geographic reporting region for a city name.

    Derived from the city's IANA tz in weather_monitor_defaults.json; never
    returns "Unknown" for the 51 canonical cities.
    """
    tz = _city_tz_lookup().get(city_name)
    if not tz:
        tz = _city_tz_lookup().get(city_name.split(",")[0].strip(), "")
    return _region_from_tz(tz)


def _compute_city_local_hour(utc_offset: float, utc_hour: int) -> int:
    """Convert UTC hour to city-local hour given its UTC offset."""
    return int((utc_hour + utc_offset + 24) % 24)


def _is_city_active(
    tz_str: str,
    city_name: str,
    peak_hour_end: int,
    utc_hour: int,
) -> bool:
    """Check if a city is in its active window.

    Active window = 04:00 local time to (peak_hour_end + 2) local time.
    Before 04:00 = too early, no data. After peak_end+2 = market settled.
    """
    offset = _get_utc_offset_for_city(city_name, tz_str)
    local_hour = _compute_city_local_hour(offset, utc_hour)

    active_start = 4  # 04:00 local
    active_end = peak_hour_end + 2  # e.g., peak 14-16 → end at 18:00 local

    # Handle wrap-around: active_end may be < active_start
    # (e.g., Tokyo: active 04:00-18:00 doesn't wrap, but if peak was 22:00 it could)
    if active_start <= active_end:
        return active_start <= local_hour <= active_end
    else:
        # Wrap-around window (active over midnight)
        return local_hour >= active_start or local_hour <= active_end


def _get_active_cities(
    locations: list["SavedLocation"],
    utc_hour: int | None = None,
) -> list["SavedLocation"]:
    """Return only cities currently in their active window.

    Active window: 04:00 local to peak_hour_end+2 local.
    At these hours predictions are relevant and markets are open.

    Args:
        locations: All available city locations.
        utc_hour: Current UTC hour (0-23). Defaults to now.

    Returns:
        Filtered list of locations in active window.
    """
    if utc_hour is None:
        utc_hour = datetime.now(timezone.utc).hour

    active: list["SavedLocation"] = []
    for loc in locations:
        tz = getattr(loc, "tz", "UTC")
        name = getattr(loc, "name", "")
        ph_end = getattr(loc, "peak_hour_end", 16)

        if _is_city_active(tz, name, ph_end, utc_hour):
            active.append(loc)

    # Sort by region for organized processing
    active.sort(key=lambda loc: _get_region_for_city(getattr(loc, "name", "")))
    return active


def _format_active_summary(active: list["SavedLocation"], utc_hour: int) -> str:
    """Generate a human-readable summary of active cities by region."""
    if not active:
        return "  No cities in active window.\n"

    regions: dict[str, list[str]] = {}
    for loc in active:
        name = getattr(loc, "name", "?")
        region = _get_region_for_city(name)
        offset = _get_utc_offset_for_city(name, getattr(loc, "tz", "UTC"))
        local_h = _compute_city_local_hour(offset, utc_hour)
        ph_end = getattr(loc, "peak_hour_end", 16)
        active_until = ph_end + 2
        regions.setdefault(region, []).append(
            f"{name} (local {local_h:02d}:00, active until {active_until:02d}:00)"
        )

    lines = [f"  📍 {len(active)} active city(s) at UTC {utc_hour:02d}:00:\n"]
    for region, cities in sorted(regions.items()):
        lines.append(f"  🌍 {region} ({len(cities)}):")
        for c in cities:
            lines.append(f"     • {c}")
        lines.append("")
    return "\n".join(lines)


def _find_or_create_today_entry(log_data: dict) -> dict:
    """Find today's run entry in the log, or create a new one."""
    return _find_or_create_entry(log_data, _today_iso())


def _find_or_create_entry(log_data: dict, run_date: str) -> dict:
    """Find a run entry for ``run_date`` in the log, or create a new one.

    Returns the entry dict (mutated in place within log_data).
    """
    runs = log_data.setdefault("runs", [])

    # Look for an existing entry for the requested date
    for entry in runs:
        if entry.get("run_date") == run_date:
            return entry

    # Create new entry: predict run_date → resolve run_date
    entry = {
        "run_date": run_date,
        "target_date": run_date,
        "phase": "daily_bma",
        "run_started": _now_utc(),
        "last_updated": _now_utc(),
        "top_5_confidence": [],
        "predictions": {},
        "observations": {},
        "arbitrage_results": {},
        "summary": {
            "sigma_wins": 0,
            "sigma_losses": 0,
            "p5_wins": 0,
            "p5_losses": 0,
            "mean_wins": 0,
            "mean_losses": 0,
        },
    }
    runs.append(entry)
    return entry


# =============================================================================
# Core Logic
# =============================================================================

async def run_bma_for_all(
    analyzer: WeatherAnalyzer,
    locations: list[SavedLocation],
    lead_days: int = 1,
) -> list[CityPrediction]:
    """Run BMA ensemble analysis for all cities, return predictions sorted by confidence."""
    print(f"\n{'='*60}")
    print(f"  BMA ANALYSE — {len(locations)} byer (lead_days={lead_days})")
    print(f"{'='*60}\n")

    results = await analyzer.bulk_confidence_analysis(locations, lead_days=lead_days)

    predictions: list[CityPrediction] = []
    for i, r in enumerate(results):
        city = r.get("city", "?")
        mean_c = r.get("mean_c", 0)
        p5_c = r.get("p5_c", 0)
        p95_c = r.get("p95_c", 0)
        conf = r.get("overall_confidence", 0)
        model_ct = r.get("model_count", 0)
        target_date = r.get("target_date", _today_iso())

        bb = r.get("best_bucket")
        if bb:
            label = bb.get("label", "")
            prob = bb.get("probability", 0)
        else:
            label = f"{int(mean_c)}-{int(mean_c)+2}°C"
            prob = 0.5

        # Estimate std from P5-P95 range (normal approx: P95-P5 ≈ 2*1.645*σ = 3.29σ)
        std_c = (p95_c - p5_c) / 3.29 if p95_c > p5_c else max(1.0, abs(mean_c) * 0.05)

        # Compute statistically optimal bet level (sigma-adjusted, replaces arbitrary floor/ceiling)
        optimal = _compute_optimal_spill(mean_c, std_c, conf, p5_c, city_name=city)
        spill = int(optimal["recommended"])
        k_used = float(optimal["k_used"])

        loc = next((l for l in locations if l.name == city), None)
        lat = loc.lat if loc else 0
        lon = loc.lon if loc else 0
        tz = loc.tz if loc else "UTC"
        ph_start = getattr(loc, "peak_hour_start", 14) if loc else 14
        ph_end = getattr(loc, "peak_hour_end", 16) if loc else 16

        pred = CityPrediction(
            city=city,
            lat=lat,
            lon=lon,
            tz=tz,
            target_date=target_date,
            suggested_spill=spill,
            bma_mean=round(mean_c, 1),
            bma_std=round(std_c, 2),
            p5=round(p5_c, 1),
            p95=round(p95_c, 1),
            confidence=round(conf, 3),
            model_count=model_ct,
            best_bucket_label=label,
            best_bucket_prob=round(prob, 3),
            peak_hour_start=ph_start,
            peak_hour_end=ph_end,
            optimal_k=k_used,
            sigma_win_prob=float(optimal["sigma_win_prob"]),
            p5_spill=int(optimal["p5_spill"]),
            mean_spill=int(optimal["mean_spill"]),
            p5_win_prob=float(optimal["p5_win_prob"]),
            mean_win_prob=float(optimal["mean_win_prob"]),
        )
        predictions.append(pred)

        swp = optimal["sigma_win_prob"]
        pwp = optimal["p5_win_prob"]
        mwp = optimal["mean_win_prob"]
        status = "🟢" if conf >= 0.8 else ("🟠" if conf >= 0.7 else "🔴")
        print(f"  {i+1:2d}. {status} {city:<30s} spill={spill:2d}°C  "
              f"μ={mean_c:5.1f}°C  σ={std_c:.1f}  k={k_used:.1f}  "
              f"P5-P95=[{p5_c:.1f}, {p95_c:.1f}]  "
              f"conf={conf:.3f}  models={model_ct}")
        print(f"       🎯 Sigma-justert: BUY {spill}°C (~{swp*100:.0f}%)  |  "
              f"P5-basert: BUY {int(optimal['p5_spill'])}°C (~{pwp*100:.0f}%)  |  "
              f"Mean-basert: BUY {int(optimal['mean_spill'])}°C (~{mwp*100:.0f}%)")

    predictions.sort(key=lambda p: p.confidence, reverse=True)
    return predictions


def select_top_n(predictions: list[CityPrediction], n: int = 5) -> list[CityPrediction]:
    """Select top N predictions by confidence."""
    return predictions[:n]


async def fetch_current_temp(
    lat: float, lon: float, tz: str = "UTC"
) -> dict[str, Any] | None:
    """Fetch current observed temperature from Open-Meteo."""
    if httpx is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                CURRENT_WEATHER_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,"
                               "wind_direction_10m,cloud_cover",
                    "timezone": tz,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            current = data.get("current", {})
            temp_c = current.get("temperature_2m")
            time_str = current.get("time", "")
            if temp_c is None:
                return None
            try:
                local_dt = datetime.fromisoformat(time_str)
            except (ValueError, TypeError):
                local_dt = datetime.now(ZoneInfo(tz)) if tz != "UTC" else datetime.now(timezone.utc)
            return {
                "temp_c": float(temp_c),
                "time_utc": time_str,
                "time_local": local_dt,
                "humidity": current.get("relative_humidity_2m"),
                "wind_speed": current.get("wind_speed_10m"),
                "wind_direction": current.get("wind_direction_10m"),
                "cloud_cover": current.get("cloud_cover"),
            }
    except Exception as exc:
        log.warning("fetch_current_temp failed (%s, %s): %s", lat, lon, str(exc))
        return None


async def _fetch_daily_max(
    lat: float, lon: float, tz: str, target_date: str
) -> float | None:
    """Fetch the daily maximum temperature from Open-Meteo archive API."""
    if httpx is None:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                ARCHIVE_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "start_date": target_date,
                    "end_date": target_date,
                    "daily": "temperature_2m_max",
                    "timezone": tz,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily", {})
            temps = daily.get("temperature_2m_max", [])
            if temps and temps[0] is not None:
                return float(temps[0])
            return None
    except Exception as exc:
        log.warning("_fetch_daily_max failed (%s, %s): %s", lat, lon, str(exc))
        return None


def _is_in_peak_window(local_dt: datetime, peak_start: int, peak_end: int) -> bool:
    """Check if the given local datetime falls within the city's peak window."""
    return peak_start <= local_dt.hour <= peak_end


# =============================================================================
# --mode hourly_active  (replaces daily_bma + hourly_check)
# =============================================================================

async def hourly_active_mode() -> None:
    """Timezone-aware hourly run: process only cities in their active window.

    Replaces the old daily_bma (06:00 UTC for ALL 51 cities) and hourly_check
    (top 5 only) with a unified timezone-aware approach:

    1. Determine which cities are in their active window (04:00 local → peak_end+2)
    2. Run BMA for those cities (if not already done today)
    3. Run hourly peak check for active cities

    This reduces API calls from 51×2 per day to only processing cities when
    their local time makes predictions relevant.
    """
    now_utc_dt = datetime.now(timezone.utc)
    utc_hour = now_utc_dt.hour
    active_region_names: set[str] = set()

    print("╔══════════════════════════════════════════════════╗")
    print(f"║   MODELLKVALITET — TIMEZONE-AWARE ({utc_hour:02d}:00 UTC)     ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    lm = LocationManager()
    all_locations = lm.locations
    active = _get_active_cities(all_locations, utc_hour)

    print(f"\n{_format_active_summary(active, utc_hour)}")

    if not active:
        print("  ⚠️ No cities in active window — nothing to do.")
        print("     Next run will check again.\n")
        return

    for loc in active:
        active_region_names.add(_get_region_for_city(getattr(loc, "name", "")))

    regions_str = ", ".join(sorted(active_region_names))
    print(f"  🌐 Active regions: {regions_str}")
    print(f"  📊 Running BMA + peak check for {len(active)} cities.\n")

    analyzer = WeatherAnalyzer()
    await analyzer.initialize()

    try:
        today_date = date.today()
        today_str = today_date.isoformat()

        print(f"\n   🎯 BMA (lead_days=0, target: {today_str} — I DAG)\n")
        predictions = await run_bma_for_all(analyzer, active, lead_days=0)
        top5 = select_top_n(predictions, 5)

        print(f"\n  {'─'*60}")
        print(f"  🏆 TOP 5 — ACTIVE ({today_str}):")
        for i, p in enumerate(top5):
            utc_peak = _local_peak_to_utc(p.tz, p.peak_hour_start, p.peak_hour_end)
            print(f"     {i+1}. {p.city:<30s} spill={p.suggested_spill}°C  "
                  f"μ={p.bma_mean:.1f}°C  conf={p.confidence:.3f}  "
                  f"({p.model_count}/8 modeller)  peak={utc_peak}")

        log_data = _load_log()
        entry = _find_or_create_today_entry(log_data)
        entry["phase"] = "hourly_active"
        entry["last_updated"] = _now_utc()
        entry["target_date"] = today_str
        entry["utc_hour"] = utc_hour
        entry["active_regions"] = sorted(active_region_names)
        entry["active_city_count"] = len(active)
        entry["all_city_count"] = len(all_locations)

        top5_city_names = [p.city for p in top5]
        entry["top_5_confidence"] = top5_city_names

        # Merge predictions — don't clobber cities already resolved in earlier
        # hourly runs. Preserve an existing city's result/actual_peak so a
        # re-run of the same day can never wipe out a resolved outcome.
        new_preds = _preds_to_dict(predictions, active, lead_days=0)
        existing_preds = entry.get("predictions", {})
        entry["predictions"] = _merge_predictions(existing_preds, new_preds)
        # Track which cities were active in THIS run (for debugging)
        entry["predictions_active"] = new_preds

        obs = entry.setdefault("observations", {})
        for city in [p.city for p in predictions]:
            if city not in obs:
                obs[city] = []

        _save_log(log_data)

        total_predictions = len(predictions)
        coverage_pct = round(len(active) / max(1, len(all_locations)) * 100, 1)
        print(f"\n  ✅ hourly_active fullført — {total_predictions} predictions "
              f"({len(active)}/{len(all_locations)} cities, {coverage_pct}% coverage)")
        print(f"  🎯 Top 5 for peak monitoring: {', '.join(top5_city_names)}")
        print(f"  🌐 Regions: {regions_str}\n")

        await _hourly_check_active(entry, predictions, active, log_data)

    finally:
        await analyzer.close()


async def _hourly_check_active(
    entry: dict,
    predictions: list[CityPrediction],
    active_locations: list["SavedLocation"],
    log_data: dict,
) -> None:
    """Run peak detection for active cities (timezone-aware hourly check).

    Replaces old hourly_check_mode which only checked top 5.
    Now checks ALL active cities in their peak windows.
    """
    now_utc_dt = datetime.now(timezone.utc)
    utc_hour = now_utc_dt.hour
    today = _today_iso()

    preds_dict = entry.get("predictions", {})
    observations = entry.setdefault("observations", {})

    print(f"\n  {'─'*60}")
    print(f"  🔍 PEAK CHECK — {len(predictions)} active cities")
    print(f"  {'─'*60}\n")

    all_confirmed = True
    newly_confirmed = 0

    for pred in predictions:
        city = pred.city
        pdata = preds_dict.get(city)
        if pdata is None:
            print(f"  ⚠️ {city}: mangler data — hopper over")
            continue

        strategies = _get_strategies(pdata)
        sigma_result = strategies.get("sigma", {}).get("result")
        if sigma_result in ("WIN", "LOSS"):
            print(f"  ✅ {city}: allerede ferdig (sigma={sigma_result})")
            continue

        all_confirmed = False

        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        ph_start = pdata.get("_peak_hour_start", 14)
        ph_end = pdata.get("_peak_hour_end", 16)
        suggested_spill = _get_sigma_spill(pdata)

        current = await fetch_current_temp(lat, lon, tz)
        if current is None:
            print(f"  ⚠️ {city}: kunne ikke hente temperatur")
            continue

        temp_c = current["temp_c"]
        local_dt = current["time_local"]

        city_obs = observations.setdefault(city, [])
        city_obs.append({
            "time": local_dt.isoformat(),
            "temp_c": temp_c,
            "peak_state": "unknown",
        })
        if len(city_obs) > MAX_OBS_HISTORY:
            observations[city] = city_obs[-MAX_OBS_HISTORY:]

        in_window = _is_in_peak_window(local_dt, ph_start, ph_end)

        if in_window:
            obs_history: list[tuple[datetime, float]] = []
            for o in city_obs:
                try:
                    t = datetime.fromisoformat(o["time"])
                    obs_history.append((t, o["temp_c"]))
                except (ValueError, TypeError):
                    pass

            try:
                target_date_obj = date.fromisoformat(pdata.get("_target_date") or "")
            except (ValueError, TypeError):
                target_date_obj = date.today()
            today_obs = [(dt, t) for dt, t in obs_history if dt.date() == target_date_obj]
            today_max: tuple[float, datetime] | None = None
            if today_obs:
                today_max = (max(t[1] for t in today_obs),
                             max(today_obs, key=lambda x: x[1])[0])

            peak_confirmed = None
            if pdata.get("peak_detected_at"):
                sigma_ap = strategies.get("sigma", {}).get("actual_peak")
                if sigma_ap is not None:
                    try:
                        confirmed_time = datetime.fromisoformat(pdata["peak_detected_at"])
                        peak_confirmed = (float(sigma_ap), confirmed_time)
                    except (ValueError, TypeError):
                        pass

            peak_state = detect_peak_state(
                obs_history=obs_history,
                today_max=today_max,
                peak_hour_start=ph_start,
                peak_hour_end=ph_end,
                local_now=local_dt,
                target_date=target_date_obj,
                peak_confirmed=peak_confirmed,
                suggested_temp=float(suggested_spill),
            )

            status_icon = getattr(peak_state, "emoji", "🌡️")
            status_text = getattr(peak_state, "state_label", peak_state.state)
            trend = getattr(peak_state, "trend", "")
            live_conf = getattr(peak_state, "live_confidence", 0)
            print(f"  {status_icon} {city:<30s} {temp_c:.1f}°C {trend}  "
                  f"{status_text} (live conf: {live_conf:.0f}%)  🔍 IN WINDOW")

            if city_obs:
                city_obs[-1]["peak_state"] = peak_state.state

            if peak_state.state in ("confirmed", "completed"):
                confirmed_temp = getattr(peak_state, "confirmed_temp", None)
                confirmed_time = getattr(peak_state, "confirmed_time", None)
                if (confirmed_temp is not None and confirmed_time is not None
                        and not pdata.get("peak_detected_at")):
                    pdata["peak_detected_at"] = confirmed_time.isoformat()

                    # Record OUR observed peak (📡) only. WIN/LOSS is decided by
                    # Polymarket resolution, never by round(live peak) == spill.
                    for strat_name in ("sigma", "p5", "mean"):
                        strat = strategies.get(strat_name, {})
                        strat["actual_peak"] = round(confirmed_temp, 1)
                    _resolve_strategies_vs_polymarket(pdata, city)

                    _update_recommendation(pdata)

                    # Record arbitrage result if sigma lost (SHORT opportunity)
                    sigma_result2 = strategies.get("sigma", {}).get("result", "?")
                    if sigma_result2 == "LOSS":
                        _record_arbitrage_result(
                            entry, city,
                            f"SHORT_{suggested_spill}",
                            confirmed_temp,
                            float(suggested_spill),
                            sigma_result2,
                        )
                    elif sigma_result2 == "WIN":
                        _record_arbitrage_result(
                            entry, city,
                            f"BUY_{suggested_spill}",
                            confirmed_temp,
                            float(suggested_spill),
                            sigma_result2,
                        )

                    result_icon = (
                        "✅ WIN" if sigma_result2 == "WIN"
                        else ("❌ LOSS" if sigma_result2 == "LOSS" else "⏳ ULAVKLART")
                    )
                    print(f"\n  ╔{'═'*58}╗")
                    print(f"  ║  {result_icon}: {city}")
                    print(f"  ║  Spill (sigma): BUY {suggested_spill}°C")
                    print(f"  ║  Faktisk peak: {confirmed_temp:.1f}°C")
                    print(f"  ║  P5 strat: {strategies.get('p5', {}).get('result', '?')} | "
                          f"Mean strat: {strategies.get('mean', {}).get('result', '?')}")
                    print(f"  ║  {pdata.get('recommendation', '')}")
                    print(f"  ║  Bekreftet: {confirmed_time.strftime('%H:%M:%S')}")
                    print(f"  ╚{'═'*58}╝")
                    newly_confirmed += 1
        else:
            local_h = local_dt.hour
            active_until = ph_end + 2
            print(f"  🌡️ {city:<30s} {temp_c:.1f}°C  "
                  f"(local {local_h:02d}:00, peak={ph_start}-{ph_end}, "
                  f"active until {active_until:02d}:00)  ⏳ venter")

            if city_obs:
                city_obs[-1]["peak_state"] = "pre_peak" if local_dt.hour < ph_start else "post_peak"

    cities_for_rapid: list[str] = []
    for pred in predictions:
        city = pred.city
        pdata = preds_dict.get(city)
        if pdata is None:
            continue
        strategies = _get_strategies(pdata)
        if strategies.get("sigma", {}).get("result") in ("WIN", "LOSS"):
            continue
        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        ph_start = pdata.get("_peak_hour_start", 14)
        ph_end = pdata.get("_peak_hour_end", 16)
        try:
            tz_obj = ZoneInfo(tz) if tz != "UTC" else timezone.utc
            local_dt = datetime.now(tz_obj)
            if _is_in_peak_window(local_dt, ph_start, ph_end):
                cities_for_rapid.append(city)
        except Exception:
            utc_hour_now = datetime.now(timezone.utc).hour
            if 10 <= utc_hour_now <= 18:
                cities_for_rapid.append(city)

    # Re-resolve every city against Polymarket outcomes so the dashboard shows
    # the true WIN/LOSS as soon as a market resolves (not only at daily_close).
    resolved_markets = _load_market_resolved_details()
    for city, pdata in entry.get("predictions", {}).items():
        _resolve_strategies_vs_polymarket(pdata, city, resolved_markets)

    entry["phase"] = "hourly_active"
    entry["last_updated"] = _now_utc()
    _recompute_summary(entry)
    _save_log(log_data)

    if all_confirmed and not newly_confirmed:
        print(f"\n  🎉 ALLE AKTIVE HAR BEKREFTET PEAK — venter på daily_close kl 23:00 UTC\n")
    elif newly_confirmed:
        print(f"\n  🔔 {newly_confirmed} ny(e) peak(er) bekreftet denne runden.\n")
    elif cities_for_rapid:
        print(f"\n  ⚡ {len(cities_for_rapid)} by(er) i peak-vindu — rapid monitor DISABLED (next hourly check will poll again)")
        # Rapid peak monitor removed — too many API calls and ties up runner.
        # The next hourly pipeline run will do another single-pass peak check.
    else:
        print(f"\n  ✅ timesone-aktiv sjekk fullført — {_now_utc()}\n")

    # ── Post-Peak Archive Fetch & Arbitrage Tracking ──
    # For cities past their active window but unresolved, fetch daily max
    # from archive API. This enables arbitrage detection when the peak has
    # already passed and we know the actual temperature.
    await _post_peak_arbitrage_check(entry, predictions, active_locations, log_data)


# =============================================================================
# --mode daily_bma  (kept for backward compat — prefer hourly_active)
# =============================================================================

def _norm_cdf(x: float) -> float:
    """Standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))


def _preds_to_dict(predictions: list[CityPrediction], locations: list[SavedLocation], lead_days: int = 0) -> dict[str, dict]:
    """Convert CityPrediction list to log-ready dict with 3 strategies per city.

    New structure:
    {
      "Madrid, ES": {
        "bma_mean": 35.4, "bma_std": 0.6, "p5": 34.4, "p95": 36.4,
        "confidence": 0.82, "models": 8,
        "bma_probs": {"30": 0.1, "31": 1.2, ..., "38": 0.3},
        "strategies": {
          "sigma": {"spill": 35, "k": 0.3, "win_prob": 0.74, "result": null, "actual_peak": null},
          "p5":    {"spill": 34, "k": null, "win_prob": 0.99, "result": null, "actual_peak": null},
          "mean":  {"spill": 35, "k": 0.0, "win_prob": 0.74, "result": null, "actual_peak": null}
        },
        "peak_detected_at": null,
        "recommendation": null,
        "_lat": ..., "_lon": ..., "_tz": ..., "_peak_hour_start": ..., "_peak_hour_end": ...,
        "_target_date": ..., "_uhi_adjustment": ..., "_lead_days": 0
      }
    }
    """
    loc_map = {l.name: l for l in locations}
    preds_dict: dict[str, dict] = {}
    for p in predictions:
        loc = loc_map.get(p.city)
        uhi = getattr(loc, "uhi_adjustment", 0.0) if loc else 0.0

        # Compute BMA probability for each temperature bucket in the P5-P95 range
        bma_probs: dict[str, float] = {}
        mean_c = p.bma_mean
        std_c = max(p.bma_std, 0.01)  # Guard against zero std
        lo = max(0, int(p.p5) - 3)
        hi = int(p.p95) + 3
        for temp in range(lo, hi + 1):
            prob = _norm_cdf((temp + 0.5 - mean_c) / std_c) - _norm_cdf((temp - 0.5 - mean_c) / std_c)
            bma_probs[str(temp)] = round(prob * 100, 1)

        preds_dict[p.city] = {
            "bma_mean": p.bma_mean,
            "bma_std": p.bma_std,
            "p5": p.p5,
            "p95": p.p95,
            "confidence": p.confidence,
            "models": p.model_count,
            "bma_probs": bma_probs,
            "strategies": {
                "sigma": {
                    "spill": p.suggested_spill,
                    "k": p.optimal_k,
                    "win_prob": p.sigma_win_prob,
                    "result": None,
                    "actual_peak": None,
                },
                "p5": {
                    "spill": p.p5_spill,
                    "k": None,
                    "win_prob": p.p5_win_prob,
                    "result": None,
                    "actual_peak": None,
                },
                "mean": {
                    "spill": p.mean_spill,
                    "k": 0.0,
                    "win_prob": p.mean_win_prob,
                    "result": None,
                    "actual_peak": None,
                },
            },
            "peak_detected_at": None,
            "recommendation": None,
            # Internal fields for API calls — prefixed with _ to distinguish from spec
            "_lat": p.lat,
            "_lon": p.lon,
            "_tz": p.tz,
            "_peak_hour_start": p.peak_hour_start,
            "_peak_hour_end": p.peak_hour_end,
            "_target_date": p.target_date,
            "_uhi_adjustment": round(uhi, 1),
            "_lead_days": lead_days,
            "_features": {
                "model_weighting": p.model_count >= 6,
                "dynamic_k": p.optimal_k != 0.5,
                "spread_filter": "narrow" if (p.p95 - p.p5) < 2.0 else ("medium" if (p.p95 - p.p5) < 4.0 else "wide"),
                "uhi_adjusted": (uhi if uhi else 0.0) > 0.5,
            },
        }
    return preds_dict


def _get_yesterday_top5() -> list[str]:
    """Return yesterday's top 5 city names from the log, or empty list if unavailable."""
    log_data = _load_log()
    runs = log_data.get("runs", [])
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    for run in runs:
        if run.get("run_date") == yesterday:
            top5 = run.get("top_5_confidence", [])
            if top5:
                return top5
    return []


async def _backfill_bma_for_date(
    analyzer: WeatherAnalyzer,
    locations: list[SavedLocation],
    target_date: str,
) -> None:
    """Regenerate BMA predictions for a specific date and store a run entry.

    Backfill path for missed days whose ``hourly_active`` predictions were
    never committed. ``lead_days`` is derived relative to today so the target
    date labels are correct; the forecast data itself is whatever the live
    model APIs return now (exact for today, approximate for past dates).
    Idempotent: reuses an existing run entry for ``target_date`` and merges
    predictions so a resolved day is never wiped out.
    """
    target = date.fromisoformat(target_date)
    lead_days = (target - date.today()).days
    print(f"\n   🎯 BACKFILL BMA (target: {target_date}, lead_days={lead_days})\n")

    predictions = await run_bma_for_all(analyzer, locations, lead_days=lead_days)

    log_data = _load_log()
    entry = _find_or_create_entry(log_data, target_date)
    entry["phase"] = "daily_bma"
    entry["last_updated"] = _now_utc()
    entry["target_date"] = target_date
    entry["top_5_confidence"] = [p.city for p in select_top_n(predictions, 5)]
    new_preds = _preds_to_dict(predictions, locations, lead_days=lead_days)
    entry["predictions"] = _merge_predictions(entry.get("predictions", {}), new_preds)
    _save_log(log_data)

    print(f"\n  ✅ Backfill BMA fullført — {len(predictions)} predictions for {target_date}\n")


async def daily_bma_mode(target_date: str | None = None) -> None:
    """Run BMA for ALL 51 cities with lead_days=0 (today) + lead_days=1 (tomorrow). Semaphore protects against rate limits."""
    print("╔══════════════════════════════════════════════════╗")
    print("║   MODELLKVALITET — DAGLIG BMA (06:00 UTC)       ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    # Load locations — ALL 51 cities
    lm = LocationManager()
    locations = lm.locations
    print(f"   📊 Running BMA for ALL {len(locations)} cities.\n")

    # Initialize analyzer
    analyzer = WeatherAnalyzer()
    await analyzer.initialize()

    try:
        if target_date:
            await _backfill_bma_for_date(analyzer, locations, target_date)
            return
        today_date = date.today()
        tomorrow_date = today_date + timedelta(days=1)
        today_str = today_date.isoformat()
        tomorrow_str = tomorrow_date.isoformat()

        # ── Day 1: Today (lead_days=0) ──
        print(f"\n   🎯 LEAD_DAYS=0 (target: {today_str} — I DAG)\n")
        predictions_day1 = await run_bma_for_all(analyzer, locations, lead_days=0)
        top5 = select_top_n(predictions_day1, 5)

        print(f"\n  {'─'*60}")
        print(f"  🏆 TOP 5 — I DAG ({today_str}):")
        for i, p in enumerate(top5):
            utc_peak = _local_peak_to_utc(p.tz, p.peak_hour_start, p.peak_hour_end)
            print(f"     {i+1}. {p.city:<30s} spill={p.suggested_spill}°C  "
                  f"μ={p.bma_mean:.1f}°C  conf={p.confidence:.3f}  "
                  f"({p.model_count}/8 modeller)  peak={utc_peak}")

        # ── Day 2: Tomorrow (lead_days=1) ──
        print(f"\n   🎯 LEAD_DAYS=1 (target: {tomorrow_str} — I MORGEN)\n")
        predictions_day2 = await run_bma_for_all(analyzer, locations, lead_days=1)
        top5_day2 = select_top_n(predictions_day2, 5)

        print(f"\n  {'─'*60}")
        print(f"  🏆 TOP 5 — I MORGEN ({tomorrow_str}):")
        for i, p in enumerate(top5_day2):
            utc_peak = _local_peak_to_utc(p.tz, p.peak_hour_start, p.peak_hour_end)
            print(f"     {i+1}. {p.city:<30s} spill={p.suggested_spill}°C  "
                  f"μ={p.bma_mean:.1f}°C  conf={p.confidence:.3f}  "
                  f"({p.model_count}/8 modeller)  peak={utc_peak}")

        # Log — store predictions for today (backward compat) + multi_day
        log_data = _load_log()
        entry = _find_or_create_today_entry(log_data)
        entry["phase"] = "daily_bma"
        entry["last_updated"] = _now_utc()
        entry["target_date"] = today_str
        top5_city_names = [p.city for p in top5]
        entry["top_5_confidence"] = top5_city_names
        entry["predictions"] = _preds_to_dict(predictions_day1, locations, lead_days=0)
        entry["predictions_multi_day"] = {
            "day1": _preds_to_dict(predictions_day1, locations, lead_days=0),
            "day2": _preds_to_dict(predictions_day2, locations, lead_days=1),
        }
        entry["observations"] = {city: [] for city in top5_city_names}

        _save_log(log_data)

        total_cities = len(predictions_day1) + len(predictions_day2)
        print(f"\n  ✅ daily_bma fullført — {total_cities} total predictions (I DAG + I MORGEN)")
        print(f"  🎯 Top 5 (I DAG) valgt for timeovervåking: {', '.join(top5_city_names)}\n")

    finally:
        await analyzer.close()


def _local_peak_to_utc(tz: str, ph_start: int, ph_end: int) -> str:
    """Convert a city's local peak window to UTC representation for display."""
    try:
        tz_obj = ZoneInfo(tz)
        now = datetime.now(tz_obj)
        offset = now.utcoffset()
        if offset is None:
            return f"{ph_start:02d}:00-{ph_end:02d}:00 local"
        offset_h = int(offset.total_seconds() / 3600)
        utc_start = (ph_start - offset_h) % 24
        utc_end = (ph_end - offset_h) % 24
        return f"{utc_start:02d}:00-{utc_end:02d}:00 UTC"
    except Exception:
        return f"{ph_start:02d}:00-{ph_end:02d}:00 local"


# =============================================================================
# Helper: get sigma spill from prediction data
# =============================================================================

def _get_sigma_spill(pdata: dict) -> int:
    """Extract sigma strategy spill from prediction data dict."""
    return pdata.get("strategies", {}).get("sigma", {}).get("spill", 30)


def _get_strategies(pdata: dict) -> dict:
    """Extract strategies dict from prediction data."""
    return pdata.get("strategies", {})


def _merge_predictions(existing: dict, new: dict) -> dict:
    """Merge freshly generated predictions into existing predictions.

    A re-run of hourly_active (or any BMA pass) must NOT clobber an
    already-resolved city. For each city already present, we carry forward its
    resolved ``result``/``actual_peak`` (and other resolution bookkeeping) while
    still refreshing the non-resolution fields from the new prediction.
    """
    merged = dict(existing)
    for city, new_pdata in new.items():
        old_pdata = merged.get(city)
        if not old_pdata:
            merged[city] = new_pdata
            continue

        # Never carry a resolved result/peak across different market days:
        # a day-N resolution must not leak onto a day-N±1 prediction.
        if old_pdata.get("_target_date") != new_pdata.get("_target_date"):
            merged[city] = new_pdata
            continue

        old_strategies = old_pdata.get("strategies", {}) or {}
        new_strategies = new_pdata.get("strategies", {}) or {}
        for sn in ("sigma", "p5", "mean"):
            old_s = old_strategies.get(sn) or {}
            new_s = new_strategies.get(sn) or {}
            if old_s.get("result") in ("WIN", "LOSS"):
                new_s["result"] = old_s["result"]
                new_s["actual_peak"] = old_s.get("actual_peak", new_s.get("actual_peak"))
            elif old_s.get("actual_peak") is not None:
                new_s["actual_peak"] = old_s["actual_peak"]

        # Carry forward resolution bookkeeping that a fresh prediction lacks.
        for key in (
            "peak_detected_at",
            "recommendation",
            "_market_resolved",
            "_market_unit",
            "_market_display",
            "_peak_gap",
            "_verdict",
        ):
            if old_pdata.get(key) is not None:
                new_pdata[key] = old_pdata[key]

        merged[city] = new_pdata
    return merged


def _recompute_cumulative_from_runs(log_data: dict) -> None:
    """Recompute ``log_data["cumulative"]`` idempotently from every run.

    ``total_days`` is the unique ``run_date`` count (single source of truth),
    so re-running never double-counts a day. All per-strategy W/L totals are
    recomputed from scratch from each run's summary + persisted mean pm_result.
    """
    runs = log_data.get("runs", [])
    _c_total_days = len({_run.get("run_date") for _run in runs if _run.get("run_date")})
    _c_total_preds = 0
    _c_sigma_w = _c_sigma_l = 0
    _c_p5_w = _c_p5_l = 0
    _c_mean_w = _c_mean_l = 0
    _c_meanpm_w = _c_meanpm_l = 0
    for _run in runs:
        _c_total_preds += len(_run.get("predictions", {}) or {})
        _s = _run.get("summary", {}) or {}
        _c_sigma_w += _s.get("sigma_wins", 0)
        _c_sigma_l += _s.get("sigma_losses", 0)
        _c_p5_w += _s.get("p5_wins", 0)
        _c_p5_l += _s.get("p5_losses", 0)
        _c_mean_w += _s.get("mean_wins", 0)
        _c_mean_l += _s.get("mean_losses", 0)
        for _p in (_run.get("predictions", {}) or {}).values():
            _pmr = (_p.get("strategies", {}) or {}).get("mean", {}).get("pm_result")
            if _pmr == "WIN":
                _c_meanpm_w += 1
            elif _pmr == "LOSS":
                _c_meanpm_l += 1
    log_data["cumulative"] = {
        "total_days": _c_total_days,
        "total_predictions": _c_total_preds,
        "sigma_wins": _c_sigma_w,
        "sigma_losses": _c_sigma_l,
        "p5_wins": _c_p5_w,
        "p5_losses": _c_p5_l,
        "mean_wins": _c_mean_w,
        "mean_losses": _c_mean_l,
        "mean_pm_wins": _c_meanpm_w,
        "mean_pm_losses": _c_meanpm_l,
        "mean_pm_bets": _c_meanpm_w + _c_meanpm_l,
    }


def _recompute_summary(entry: dict) -> None:
    """Recompute entry["summary"] from entry["predictions"] strategy results.

    Keeps the summary consistent no matter which path (hourly_active,
    rapid monitor, post-peak arbitrage or daily_close) resolved the cities.
    """
    sigma_wins = sigma_losses = 0
    p5_wins = p5_losses = 0
    mean_wins = mean_losses = 0
    unresolved = 0

    for pdata in (entry.get("predictions") or {}).values():
        strategies = pdata.get("strategies", {}) or {}
        for sn in ("sigma", "p5", "mean"):
            res = strategies.get(sn, {}).get("result")
            if res == "WIN":
                if sn == "sigma":
                    sigma_wins += 1
                elif sn == "p5":
                    p5_wins += 1
                else:
                    mean_wins += 1
            elif res == "LOSS":
                if sn == "sigma":
                    sigma_losses += 1
                elif sn == "p5":
                    p5_losses += 1
                else:
                    mean_losses += 1
        if strategies.get("sigma", {}).get("result") not in ("WIN", "LOSS"):
            unresolved += 1

    entry["summary"] = {
        "sigma_wins": sigma_wins,
        "sigma_losses": sigma_losses,
        "p5_wins": p5_wins,
        "p5_losses": p5_losses,
        "mean_wins": mean_wins,
        "mean_losses": mean_losses,
        "unresolved": unresolved,
    }


# =============================================================================
# --mode hourly_check
# =============================================================================

async def hourly_check_mode() -> None:
    """Check current temps for today's top 5, run peak detection if in window."""
    now_utc_dt = datetime.now(timezone.utc)
    utc_hour = now_utc_dt.hour

    print("╔══════════════════════════════════════════════════╗")
    print(f"║   MODELLKVALITET — TIMESJEKK ({utc_hour:02d}:00 UTC)        ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    # Load log
    log_data = _load_log()
    today = _today_iso()

    # Find today's entry
    entry = None
    for e in log_data.get("runs", []):
        if e.get("run_date") == today:
            entry = e
            break

    if entry is None or not entry.get("top_5_confidence"):
        print("  ⚠️ Ingen daily_bma entry for i dag — skipper.")
        print("     (Dette er normalt før 06:00 UTC)\n")
        return

    top5_cities = entry.get("top_5_confidence", [])
    predictions = entry.get("predictions", {})
    observations = entry.setdefault("observations", {})

    print(f"  Overvåker {len(top5_cities)} byer: {', '.join(top5_cities)}\n")

    all_confirmed = True
    newly_confirmed = 0

    for city in top5_cities:
        pdata = predictions.get(city)
        if pdata is None:
            print(f"  ⚠️ {city}: mangler data — hopper over")
            continue

        # Skip if ALL strategies already resolved
        strategies = _get_strategies(pdata)
        sigma_result = strategies.get("sigma", {}).get("result")
        if sigma_result in ("WIN", "LOSS"):
            print(f"  ✅ {city}: allerede ferdig (sigma={sigma_result})")
            continue

        all_confirmed = False

        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        ph_start = pdata.get("_peak_hour_start", 14)
        ph_end = pdata.get("_peak_hour_end", 16)
        suggested_spill = _get_sigma_spill(pdata)

        # Fetch current temp
        current = await fetch_current_temp(lat, lon, tz)
        if current is None:
            print(f"  ⚠️ {city}: kunne ikke hente temperatur")
            continue

        temp_c = current["temp_c"]
        local_dt = current["time_local"]

        # Update observation history
        city_obs = observations.setdefault(city, [])
        city_obs.append({
            "time": local_dt.isoformat(),
            "temp_c": temp_c,
            "peak_state": "unknown",
        })
        # Trim
        if len(city_obs) > MAX_OBS_HISTORY:
            observations[city] = city_obs[-MAX_OBS_HISTORY:]

        # Check peak window
        in_window = _is_in_peak_window(local_dt, ph_start, ph_end)

        if in_window:
            # Build obs_history in the format detect_peak_state expects
            obs_history: list[tuple[datetime, float]] = []
            for o in city_obs:
                try:
                    t = datetime.fromisoformat(o["time"])
                    obs_history.append((t, o["temp_c"]))
                except (ValueError, TypeError):
                    pass

            # Compute today's max
            try:
                target_date_obj = date.fromisoformat(pdata.get("_target_date") or "")
            except (ValueError, TypeError):
                target_date_obj = date.today()
            today_obs = [(dt, t) for dt, t in obs_history if dt.date() == target_date_obj]
            today_max: tuple[float, datetime] | None = None
            if today_obs:
                today_max = (max(t[1] for t in today_obs),
                             max(today_obs, key=lambda x: x[1])[0])

            # Check if already confirmed
            peak_confirmed = None
            if pdata.get("peak_detected_at"):
                # Use sigma strategy's actual_peak if available
                sigma_ap = strategies.get("sigma", {}).get("actual_peak")
                if sigma_ap is not None:
                    try:
                        confirmed_time = datetime.fromisoformat(pdata["peak_detected_at"])
                        peak_confirmed = (float(sigma_ap), confirmed_time)
                    except (ValueError, TypeError):
                        pass

            # Run peak detection
            peak_state = detect_peak_state(
                obs_history=obs_history,
                today_max=today_max,
                peak_hour_start=ph_start,
                peak_hour_end=ph_end,
                local_now=local_dt,
                target_date=target_date_obj,
                peak_confirmed=peak_confirmed,
                suggested_temp=float(suggested_spill),
            )

            status_icon = getattr(peak_state, "emoji", "🌡️")
            status_text = getattr(peak_state, "state_label", peak_state.state)
            trend = getattr(peak_state, "trend", "")
            live_conf = getattr(peak_state, "live_confidence", 0)
            print(f"  {status_icon} {city:<30s} {temp_c:.1f}°C {trend}  "
                  f"{status_text} (live conf: {live_conf:.0f}%)  🔍 IN WINDOW")

            # Update latest observation with peak state
            if city_obs:
                city_obs[-1]["peak_state"] = peak_state.state

            # Check if peak confirmed NOW
            if peak_state.state in ("confirmed", "completed"):
                confirmed_temp = getattr(peak_state, "confirmed_temp", None)
                confirmed_time = getattr(peak_state, "confirmed_time", None)
                if (confirmed_temp is not None and confirmed_time is not None
                        and not pdata.get("peak_detected_at")):
                    pdata["peak_detected_at"] = confirmed_time.isoformat()

                    # Record OUR observed peak (📡) only. WIN/LOSS is decided by
                    # Polymarket resolution, never by round(live peak) == spill.
                    for strat_name in ("sigma", "p5", "mean"):
                        strat = strategies.get(strat_name, {})
                        strat["actual_peak"] = round(confirmed_temp, 1)
                    _resolve_strategies_vs_polymarket(pdata, city)

                    # Generate recommendation based on sigma strategy
                    _update_recommendation(pdata)

                    sigma_result = strategies.get("sigma", {}).get("result", "?")
                    result_icon = (
                        "✅ WIN" if sigma_result == "WIN"
                        else ("❌ LOSS" if sigma_result == "LOSS" else "⏳ ULAVKLART")
                    )
                    print(f"\n  ╔{'═'*58}╗")
                    print(f"  ║  {result_icon}: {city}")
                    print(f"  ║  Spill (sigma): BUY {suggested_spill}°C")
                    print(f"  ║  Faktisk peak: {confirmed_temp:.1f}°C")
                    print(f"  ║  P5 strat: {strategies.get('p5', {}).get('result', '?')} | "
                          f"Mean strat: {strategies.get('mean', {}).get('result', '?')}")
                    print(f"  ║  {pdata.get('recommendation', '')}")
                    print(f"  ║  Bekreftet: {confirmed_time.strftime('%H:%M:%S')}")
                    print(f"  ╚{'═'*58}╝")
                    newly_confirmed += 1
        else:
            # Outside peak window — just log temp
            local_h = local_dt.hour
            print(f"  🌡️ {city:<30s} {temp_c:.1f}°C  "
                  f"(local {local_h:02d}:00, peak={ph_start}-{ph_end})  ⏳ venter")

            if city_obs:
                city_obs[-1]["peak_state"] = "pre_peak" if local_dt.hour < ph_start else "post_peak"

    # ── Detect cities currently in peak window for rapid monitoring ──
    cities_for_rapid: list[str] = []
    for city in top5_cities:
        pdata = predictions.get(city)
        if pdata is None:
            continue
        strategies = _get_strategies(pdata)
        if strategies.get("sigma", {}).get("result") in ("WIN", "LOSS"):
            continue
        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        ph_start = pdata.get("_peak_hour_start", 14)
        ph_end = pdata.get("_peak_hour_end", 16)
        try:
            tz_obj = ZoneInfo(tz) if tz != "UTC" else timezone.utc
            local_dt = datetime.now(tz_obj)
            if _is_in_peak_window(local_dt, ph_start, ph_end):
                cities_for_rapid.append(city)
        except Exception:
            utc_hour_now = datetime.now(timezone.utc).hour
            if 10 <= utc_hour_now <= 18:
                cities_for_rapid.append(city)

    # Save updates
    entry["phase"] = "hourly_check"
    entry["last_updated"] = _now_utc()
    _save_log(log_data)

    if all_confirmed and not newly_confirmed:
        print(f"\n  🎉 ALLE TOP 5 HAR BEKREFTET PEAK — venter på daily_close kl 23:00 UTC\n")
    elif newly_confirmed:
        print(f"\n  🔔 {newly_confirmed} ny(e) peak(er) bekreftet denne runden.\n")
    elif cities_for_rapid:
        print(f"\n  ⚡ {len(cities_for_rapid)} by(er) i peak-vindu — starter 3-min rapid monitor\n")
        await _rapid_peak_monitor(
            entry=entry,
            cities_in_window=cities_for_rapid,
            predictions=predictions,
            observations=observations,
        )
        _save_log(log_data)
    else:
        print(f"\n  ✅ timesjekk fullført — {_now_utc()}\n")


# =============================================================================
# Rapid Peak Window Monitor (3-min polling with ALL edge filters)
# =============================================================================

async def _rapid_peak_monitor(
    entry: dict,
    cities_in_window: list[str],
    predictions: dict,
    observations: dict,
    interval_minutes: int = RAPID_POLL_INTERVAL_MINUTES,
    max_runtime_hours: int = MAX_RAPID_RUNTIME_HOURS,
) -> None:
    """Poll every N minutes while cities are in their peak windows.

    Integrates ALL edge filters at each poll:
      1. Humidity adjustment (>80%: -8%, <40%: +3%)
      2. Cloud cover adjustment (>70%: -5%, <20%: +3%)
      3. UHI adjustment from location config
      4. Live confidence with 4 factors (time, decline, staleness, distance)
      5. Correlation warnings between monitored cities
      6. Kelly criterion for position sizing
      7. Ensemble spread tracking (P5-P95 range)
      8. 5 alert levels (INFO, MOMENTANT_OVER, ADVARSEL, KRITISK, BEKREFTET)

    Logs ALL filter states in _rapid_peak_log.json at each poll.
    """
    import traceback

    start_time = datetime.now(timezone.utc)
    max_end = start_time + timedelta(hours=max_runtime_hours)
    poll_count = 0

    print(f"\n{'='*70}")
    print(f"  ⚡ RAPID PEAK MONITORING ENGAGED — {interval_minutes}-min polling")
    print(f"  Cities in window: {', '.join(cities_in_window)}")
    print(f"  Max runtime: {max_runtime_hours}h (until {max_end.strftime('%H:%M UTC')})")
    print(f"  All edge filters ACTIVE: humidity, cloud, UHI, Kelly, correlation, spread")
    print(f"{'='*70}\n")

    # Load rapid peak log
    rapid_log: list[dict] = []
    if RAPID_PEAK_LOG.exists():
        try:
            rapid_log = json.loads(RAPID_PEAK_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            rapid_log = []

    # Load correlations for cross-city warnings
    correlations: list[dict] = []
    try:
        lm = LocationManager()
        correlations = lm.load_correlations()
    except Exception:
        pass

    while cities_in_window:
        poll_count += 1
        now_utc_dt = datetime.now(timezone.utc)

        # Max runtime check
        if now_utc_dt > max_end:
            print(f"\n  ⏰ Max runtime ({max_runtime_hours}h) exceeded. Exiting rapid monitor.")
            break

        print(f"\n  ── Poll #{poll_count} [{now_utc_dt.strftime('%H:%M:%S UTC')}] ──")

        cities_removed_this_poll: list[str] = []

        for city in cities_in_window:
            pdata = predictions.get(city)
            if pdata is None:
                cities_removed_this_poll.append(city)
                continue

            strategies = _get_strategies(pdata)
            # Skip if already resolved
            if strategies.get("sigma", {}).get("result") in ("WIN", "LOSS"):
                print(f"  ✅ {city}: resolved (sigma={strategies['sigma']['result']}) — removing from monitor")
                cities_removed_this_poll.append(city)
                continue

            lat = pdata.get("_lat", 0)
            lon = pdata.get("_lon", 0)
            tz = pdata.get("_tz", "UTC")
            ph_start = pdata.get("_peak_hour_start", 14)
            ph_end = pdata.get("_peak_hour_end", 16)
            suggested_spill = _get_sigma_spill(pdata)
            bma_mean = pdata.get("bma_mean", 0)
            p5_val = pdata.get("p5", bma_mean)
            p95_val = pdata.get("p95", bma_mean)
            uhi_adj = pdata.get("_uhi_adjustment", 0.0)

            # Fetch current weather (temp + humidity + wind + cloud)
            current = await fetch_current_temp(lat, lon, tz)
            if current is None:
                print(f"  ⚠️ {city}: fetch failed — retrying next poll")
                continue

            temp_c = current["temp_c"]
            humidity = current.get("humidity")
            wind_speed = current.get("wind_speed")
            cloud_cover = current.get("cloud_cover")
            local_dt = current["time_local"]

            # Check if still in peak window
            in_window = _is_in_peak_window(local_dt, ph_start, ph_end)
            if not in_window:
                print(f"  ⏰ {city}: exited peak window (local {local_dt.strftime('%H:%M')})")
                cities_removed_this_poll.append(city)
                continue

            # ── FILTER 1: Humidity adjustment ──
            humidity_adj = 0
            if humidity is not None:
                if humidity > 80:
                    humidity_adj = -8
                elif humidity < 40:
                    humidity_adj = 3

            # ── FILTER 2: Cloud cover adjustment ──
            cloud_adj = 0
            if cloud_cover is not None:
                if cloud_cover > 70:
                    cloud_adj = -5
                elif cloud_cover < 20:
                    cloud_adj = 3

            # ── FILTER 3: UHI adjustment ──
            uhi_adj_val = uhi_adj if uhi_adj > 0 else 0.0
            bma_adjusted = bma_mean + uhi_adj_val

            # ── Build observation history ──
            city_obs = observations.setdefault(city, [])
            city_obs.append({
                "time": local_dt.isoformat(),
                "temp_c": temp_c,
                "peak_state": "in_window_rapid",
            })
            if len(city_obs) > MAX_OBS_HISTORY:
                observations[city] = city_obs[-MAX_OBS_HISTORY:]

            obs_history: list[tuple[datetime, float]] = []
            for o in city_obs:
                try:
                    t = datetime.fromisoformat(o["time"])
                    obs_history.append((t, o["temp_c"]))
                except (ValueError, TypeError):
                    pass

            # Compute today's max
            try:
                target_date_obj = date.fromisoformat(pdata.get("_target_date") or "")
            except (ValueError, TypeError):
                target_date_obj = date.today()
            today_obs = [(dt, t) for dt, t in obs_history if dt.date() == target_date_obj]
            today_max: tuple[float, datetime] | None = None
            if today_obs:
                today_max = (max(t[1] for t in today_obs),
                             max(today_obs, key=lambda x: x[1])[0])

            # Check if already confirmed
            peak_confirmed = None
            if pdata.get("peak_detected_at"):
                sigma_ap = strategies.get("sigma", {}).get("actual_peak")
                if sigma_ap is not None:
                    try:
                        confirmed_time = datetime.fromisoformat(pdata["peak_detected_at"])
                        peak_confirmed = (float(sigma_ap), confirmed_time)
                    except (ValueError, TypeError):
                        pass

            # ── FILTER 4: Run peak detection ──
            peak_state = detect_peak_state(
                obs_history=obs_history,
                today_max=today_max,
                peak_hour_start=ph_start,
                peak_hour_end=ph_end,
                local_now=local_dt,
                target_date=target_date_obj,
                peak_confirmed=peak_confirmed,
                suggested_temp=float(suggested_spill),
            )

            live_conf = getattr(peak_state, "live_confidence", 0)
            alert_level = getattr(peak_state, "alert_level", "none")
            alert_msg = getattr(peak_state, "alert_message", "")
            trend = getattr(peak_state, "trend", "→")
            state_label = getattr(peak_state, "state_label", peak_state.state)

            # ── FILTER 5: Compute adjusted confidence ──
            base_conf = pdata.get("confidence", 0)
            adjusted_conf = base_conf + (humidity_adj + cloud_adj) / 100.0
            adjusted_conf = max(0.01, min(0.99, adjusted_conf))

            # ── FILTER 6: Correlation warnings ──
            corr_warnings = check_correlations(
                [cities_in_window[0]] if len(cities_in_window) == 1 else cities_in_window[:5],
                correlations,
            )
            city_corr_warning = any(city in w for w in corr_warnings)

            # ── FILTER 7: Kelly criterion ──
            kelly_pct = compute_kelly(adjusted_conf, odds=1.39)

            # ── FILTER 8: Ensemble spread ──
            spread = p95_val - p5_val

            # ── Compile filter state log ──
            filter_state: dict[str, Any] = {
                "timestamp": now_utc_dt.isoformat(),
                "city": city,
                "temp_c": round(temp_c, 1),
                "trend": trend,
                "live_confidence": round(live_conf, 1),
                "base_confidence": base_conf,
                "adjusted_confidence": round(adjusted_conf, 3),
                "filters_active": {
                    "humidity_adj": humidity_adj,
                    "cloud_adj": cloud_adj,
                    "uhi_adj": round(uhi_adj_val, 1),
                    "kelly_pct": round(kelly_pct, 1),
                    "correlation_warning": city_corr_warning,
                    "ensemble_spread": round(spread, 1),
                    "bma_adjusted": round(bma_adjusted, 1),
                    "suggested_spill": suggested_spill,
                },
                "peak_state": state_label,
                "alert_level": alert_level,
                "alert_message": alert_msg,
                "humidity": humidity,
                "cloud_cover": cloud_cover,
                "wind_speed": wind_speed,
                "poll_number": poll_count,
            }
            rapid_log.append(filter_state)

            # Print concise status
            alert_icon = {"info": "ℹ️", "advarsel": "⚠️", "kritisk": "🚨",
                          "bekreftet": "🔴", "none": "  "}.get(alert_level, "  ")
            print(f"  {alert_icon} {city:<25s} {temp_c:.1f}°C {trend}  "
                  f"live_conf={live_conf:.0f}%  adj_conf={adjusted_conf:.3f}  "
                  f"hum={humidity_adj:+d}  cloud={cloud_adj:+d}  "
                  f"kelly={kelly_pct:.0f}%  spread={spread:.1f}  [{state_label}]")

            # ── Check if peak confirmed NOW ──
            if peak_state.state in ("confirmed", "completed"):
                confirmed_temp = getattr(peak_state, "confirmed_temp", None)
                confirmed_time = getattr(peak_state, "confirmed_time", None)
                if (confirmed_temp is not None and confirmed_time is not None
                        and not pdata.get("peak_detected_at")):
                    pdata["peak_detected_at"] = confirmed_time.isoformat()

                    # Record OUR observed peak (📡) only. WIN/LOSS is decided by
                    # Polymarket resolution, never by round(live peak) == spill.
                    for strat_name in ("sigma", "p5", "mean"):
                        strat = strategies.get(strat_name, {})
                        strat["actual_peak"] = round(confirmed_temp, 1)
                    _resolve_strategies_vs_polymarket(pdata, city)

                    # Generate recommendation
                    _update_recommendation(pdata)

                    sigma_result = strategies.get("sigma", {}).get("result", "?")
                    if sigma_result == "LOSS":
                        _record_arbitrage_result(
                            entry, city,
                            f"SHORT_{suggested_spill}",
                            confirmed_temp,
                            float(suggested_spill),
                            sigma_result,
                        )
                    elif sigma_result == "WIN":
                        _record_arbitrage_result(
                            entry, city,
                            f"BUY_{suggested_spill}",
                            confirmed_temp,
                            float(suggested_spill),
                            sigma_result,
                        )

                    result_icon = (
                        "✅ WIN" if sigma_result == "WIN"
                        else ("❌ LOSS" if sigma_result == "LOSS" else "⏳ ULAVKLART")
                    )
                    print(f"\n  ╔{'═'*64}╗")
                    print(f"  ║  🎯 RAPID PEAK CONFIRMED: {result_icon} — {city}")
                    print(f"  ║  Spill (sigma): BUY {suggested_spill}°C")
                    print(f"  ║  Actual peak: {confirmed_temp:.1f}°C")
                    print(f"  ║  P5 strat: {strategies.get('p5', {}).get('result', '?')} | "
                          f"Mean strat: {strategies.get('mean', {}).get('result', '?')}")
                    print(f"  ║  {pdata.get('recommendation', '')}")
                    print(f"  ║  Confirmed at: {confirmed_time.strftime('%H:%M:%S')}")
                    print(f"  ║  Filters: hum={humidity_adj:+d} cloud={cloud_adj:+d} "
                          f"uhi={uhi_adj_val:+.1f} kelly={kelly_pct:.0f}%")
                    print(f"  ╚{'═'*64}╝")
                    cities_removed_this_poll.append(city)

        # Remove resolved/exited cities from monitoring list
        for c in cities_removed_this_poll:
            if c in cities_in_window:
                cities_in_window.remove(c)

        # Save progress
        entry["predictions"] = predictions
        _recompute_summary(entry)
        entry["phase"] = "rapid_peak_monitor"
        entry["last_updated"] = _now_utc()
        log_data = _load_log()
        for e in log_data.get("runs", []):
            if e.get("run_date") == entry.get("run_date"):
                e["predictions"] = predictions
                e["observations"] = observations
                e["summary"] = entry["summary"]
                e["phase"] = "rapid_peak_monitor"
                e["last_updated"] = _now_utc()
                break
        _save_log(log_data)

        # Persist rapid peak log
        RAPID_PEAK_LOG.write_text(
            json.dumps(rapid_log, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        if not cities_in_window:
            print(f"\n  🎉 All cities exited peak windows or resolved. "
                  f"Rapid monitor complete after {poll_count} polls.\n")
            break

        print(f"  ⏳ Next poll in {interval_minutes} min. "
              f"{len(cities_in_window)} cities still in window.")
        await asyncio.sleep(interval_minutes * 60)


# =============================================================================
# Recommendation Generator
# =============================================================================

def _update_recommendation(pdata: dict) -> None:
    """Generate flip/hold recommendation based on strategy results.

    - If sigma strategy is WIN → "✅ HOLD — bet vinner"
    - If sigma strategy is LOSS → "🔴 SELG med tap — gå SHORT {spill}°C"
    - Track if flip WOULD have been profitable
    """
    strategies = _get_strategies(pdata)
    sigma = strategies.get("sigma", {})
    p5 = strategies.get("p5", {})
    mean = strategies.get("mean", {})

    sigma_result = sigma.get("result")
    sigma_spill = sigma.get("spill", 0)
    actual_peak = sigma.get("actual_peak")

    if sigma_result == "WIN":
        rounded_actual = round(actual_peak) if actual_peak is not None else "?"
        pdata["recommendation"] = f"✅ HOLD — bet vinner (round({actual_peak}°C) == {sigma_spill}°C)"
    elif sigma_result == "LOSS":
        # Recommend flipping to SHORT
        rounded_actual = round(actual_peak) if actual_peak is not None else "?"
        pdata["recommendation"] = f"🔴 SELG med tap — gå SHORT {sigma_spill}°C (peak={actual_peak}°C → round={rounded_actual} ≠ {sigma_spill})"

        # Check if P5 or mean strategies would also have lost
        p5_result = p5.get("result")
        mean_result = mean.get("result")
        alt_wins = []
        if p5_result == "WIN":
            alt_wins.append(f"P5@{p5.get('spill')}°C")
        if mean_result == "WIN":
            alt_wins.append(f"Mean@{mean.get('spill')}°C")
        if alt_wins:
            pdata["recommendation"] += f" | Alt. ville vunnet: {', '.join(alt_wins)}"
    else:
        # Still pending — check if temp is approaching spill
        if actual_peak is not None:
            gap = sigma_spill - actual_peak
            if gap <= 1.0:
                pdata["recommendation"] = f"⏳ AVVENT — temp nærmer seg spill (gap={gap:.1f}°C)"
            else:
                pdata["recommendation"] = f"⏳ AVVENT — peak={actual_peak}°C, spill={sigma_spill}°C (gap={gap:.1f}°C)"


def _record_arbitrage_result(
    entry: dict,
    city: str,
    action: str,
    actual_peak: float,
    sigma_spill: float,
    sigma_result: str,
) -> None:
    """Record an arbitrage result for a city in the log entry.

    Populates entry["arbitrage_results"][city] with the arbitrage action,
    actual peak, and result (WIN/LOSS).
    """
    arb_results = entry.setdefault("arbitrage_results", {})
    today = _today_iso()

    # Determine if the arbitrage action would have been profitable
    # SHORT: we bet AGAINST the spill bucket → WIN if sigma_result == LOSS (our bet was correct)
    # BUY: we bet ON a different bucket → WIN depends on which bucket
    if action.startswith("SHORT"):
        # SHORT_XX means we bet temperature would NOT round to sigma_spill
        # WIN if sigma_result is LOSS (we correctly bet against the spill)
        arb_result = "WIN" if sigma_result == "LOSS" else "LOSS"
    else:
        # BUY_XX means we bet temperature WOULD round to a specific bucket
        # WIN if actual_peak rounds to that bucket
        target_temp = int(action.split("_")[1]) if "_" in action else 0
        arb_result = "WIN" if round(actual_peak) == target_temp else "LOSS"

    # Compute profit: winning arbitrage nets the price difference
    profit_pct = 0.0
    if arb_result == "WIN":
        # Conservative estimate: ~5% profit on resolution arbitrage
        # (buy at ~95c, collect 100c at resolution = 5.3% return)
        profit_pct = round((100.0 - 95.0) / 95.0 * 100, 1)  # ~5.3%
        if action.startswith("SHORT"):
            # SHORT: sell at ~5c, collect 0c at resolution
            profit_pct = round((5.0 / 95.0) * 100, 1)  # ~5.3% on margin

    arb_results[city] = {
        "date": today,
        "action": action,
        "actual_peak": round(actual_peak, 1),
        "result": arb_result,
        "profit_pct": profit_pct,
    }

    action_icon = "🔴" if action.startswith("SHORT") else "🟢"
    result_icon = "✅" if arb_result == "WIN" else "❌"
    print(f"  💰 ARBITRAGE: {action_icon} {city} {action} "
          f"peak={actual_peak:.1f}°C → {result_icon} {arb_result} ({profit_pct:.1f}%)")


async def _post_peak_arbitrage_check(
    entry: dict,
    predictions: list["CityPrediction"],
    active_locations: list["SavedLocation"],
    log_data: dict,
) -> None:
    """Fetch daily max from archive API for cities past their peak window.

    For cities where:
      1. Active window has ended (peak_end + 2h has passed)
      2. Not yet resolved (no WIN/LOSS)
      3. BMA confidence > 80%

    Fetch archive max, resolve strategies, and record arbitrage results.
    This enables near-real-time arbitrage detection without waiting for daily_close.
    """
    now_utc_dt = datetime.now(timezone.utc)
    preds_dict = entry.get("predictions", {})
    today = _today_iso()

    print(f"\n  {'─'*60}")
    print(f"  💰 POST-PEAK ARBITRAGE CHECK — Archive Fetch")
    print(f"  {'─'*60}")

    arbitrage_count = 0
    for city, pdata in preds_dict.items():
        strategies = _get_strategies(pdata)
        sigma_result = strategies.get("sigma", {}).get("result")

        # Skip already resolved
        if sigma_result in ("WIN", "LOSS"):
            continue

        # Only process high-confidence cities for arbitrage
        confidence = pdata.get("confidence", 0)
        if confidence < 0.80:
            continue

        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        ph_end = pdata.get("_peak_hour_end", 16)

        # Check if active window has ended
        offset = _get_utc_offset_for_city(city, tz)
        local_hour = _compute_city_local_hour(offset, now_utc_dt.hour)
        active_until = ph_end + 2

        if local_hour <= active_until:
            # Still in active window — skip (will be handled by peak detection)
            continue

        # Active window passed — fetch archive max
        city_target = pdata.get("_target_date", today)
        archive_max = await _fetch_daily_max(lat, lon, tz, city_target)

        if archive_max is None:
            continue

        sigma_spill = _get_sigma_spill(pdata)
        print(f"  📡 {city:<30s}: archive max={archive_max:.1f}°C "
              f"(local {local_hour:02d}:00, window ended at {active_until:02d}:00)")

        # Record OUR observed peak (📡) only. WIN/LOSS is decided by
        # Polymarket resolution, never by round(archive) == spill.
        pdata["peak_detected_at"] = _now_utc()
        for strat_name in ("sigma", "p5", "mean"):
            strat = strategies.get(strat_name, {})
            strat["actual_peak"] = round(archive_max, 1)
        _resolve_strategies_vs_polymarket(pdata, city)

        _update_recommendation(pdata)

        # Record arbitrage result
        sigma_res = strategies.get("sigma", {}).get("result", "")
        if sigma_res == "LOSS":
            action = f"SHORT_{sigma_spill}"
        elif sigma_res == "WIN":
            action = f"BUY_{sigma_spill}"
        else:
            continue

        _record_arbitrage_result(
            entry, city, action,
            archive_max, float(sigma_spill), sigma_res,
        )
        arbitrage_count += 1

    if arbitrage_count > 0:
        print(f"\n  💰 {arbitrage_count} arbitrage opportunities recorded.")
    else:
        print(f"  📊 No post-peak arbitrage opportunities found.\n")

    entry["last_updated"] = _now_utc()
    _recompute_summary(entry)
    _save_log(log_data)

    # Cross-reference our peaks with Polymarket resolved outcomes
    await _verify_peaks_vs_market(entry, predictions, log_data)


def _summarize_arbitrage(runs: list[dict]) -> dict:
    """Compute arbitrage win/loss statistics across all runs.

    Returns:
        {
            "short": {"wins": 5, "losses": 2, "rate": 71.4},
            "buy": {"wins": 3, "losses": 1, "rate": 75.0},
            "total": {"wins": 8, "losses": 3, "rate": 72.7},
            "by_city": {...}
        }
    """
    short_wins = short_losses = 0
    buy_wins = buy_losses = 0

    for run in runs:
        arb_results = run.get("arbitrage_results", {})
        for city, arb in arb_results.items():
            action = arb.get("action", "")
            result = arb.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            if action.startswith("SHORT"):
                if result == "WIN":
                    short_wins += 1
                else:
                    short_losses += 1
            elif action.startswith("BUY"):
                if result == "WIN":
                    buy_wins += 1
                else:
                    buy_losses += 1

    short_total = short_wins + short_losses
    buy_total = buy_wins + buy_losses
    total_wins = short_wins + buy_wins
    total_losses = short_losses + buy_losses
    total_all = total_wins + total_losses

    return {
        "short": {
            "wins": short_wins,
            "losses": short_losses,
            "rate": round(short_wins / max(1, short_total) * 100, 1),
        },
        "buy": {
            "wins": buy_wins,
            "losses": buy_losses,
            "rate": round(buy_wins / max(1, buy_total) * 100, 1),
        },
        "total": {
            "wins": total_wins,
            "losses": total_losses,
            "rate": round(total_wins / max(1, total_all) * 100, 1),
        },
    }


# =============================================================================
# --mode daily_close
# =============================================================================

async def daily_close_mode(target_date: str | None = None) -> None:
    """Finalize daily results for ALL 51 cities, compare all 3 strategies, generate report.

    Resolves cities as soon as their peak window has passed (peak_end + 2h local).
    No longer waits until 23:00 UTC — per-city timezone-aware resolution:
      - Asian cities (peak ended ~10:00 UTC) → resolved by 12:00 UTC
      - European cities (peak ended ~17:00 UTC) → resolved by 19:00 UTC
      - American cities (peak ended ~23:00 UTC) → resolved by 01:00 UTC next day

    Can be run at any time; safely skips cities still in their active window.
    """
    print("╔══════════════════════════════════════════════════╗")
    print("║   MODELLKVALITET — DAGLIG AVSLUTNING (PER-CITY) ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    log_data = _load_log()

    # Resolve predictions against archive data. ``target_date`` overrides the
    # date being resolved (used for backfilling missed days); defaults to today.
    today = target_date or date.today().isoformat()
    real_today = date.today().isoformat()

    entry = None
    for e in log_data.get("runs", []):
        if e.get("run_date") == today:
            entry = e
            break

    if entry is None or not entry.get("predictions"):
        print(f"  ⚠️ Ingen daily_bma entry for {today} — kan ikke avslutte.")
        print("     (Dette er normalt hvis daily_bma ikke har kjort ennå)\n")
        return

    predictions = entry.get("predictions", {})
    # Fallback target_date; each city uses its own _target_date from prediction data
    fallback_target = today

    print(f"  Finaliserer {len(predictions)} byer for {fallback_target}\n")

    sigma_wins = sigma_losses = 0
    p5_wins = p5_losses = 0
    mean_wins = mean_losses = 0
    unresolved = 0

    for city, pdata in predictions.items():
        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        # Date-matched resolution: use each city's _target_date from prediction data
        city_target = pdata.get("_target_date", fallback_target)
        strategies = _get_strategies(pdata)

        # ═══════════════════════════════════════════════════════════════
        # PER-CITY PEAK WINDOW GUARD: Resolve each city as soon as its
        # peak window has passed (peak_end + 2h local). No blanket 23:00
        # wait — timezone-aware per-city resolution.
        #   Asia (peak ~10 UTC)   → resolved by 12:00 UTC
        #   Europe (peak ~17 UTC) → resolved by 19:00 UTC
        #   Americas (peak ~23 UTC) → resolved by 01:00 UTC next day
        # ═══════════════════════════════════════════════════════════════
        now_utc_dt = datetime.now(timezone.utc)
        if city_target >= real_today:
            ph_end = pdata.get("_peak_hour_end", 16)
            tz_city = pdata.get("_tz", "UTC")
            offset = _get_utc_offset_for_city(city, tz_city)
            local_hour = _compute_city_local_hour(offset, now_utc_dt.hour)
            peak_end_plus_2 = ph_end + 2

            # City can only be resolved if its local time is past peak_end + 2
            if local_hour <= peak_end_plus_2:
                print(f"  ⏰ {city:<30s}: target={city_target} — too early "
                      f"(local {local_hour:02d}:00, peak ends {ph_end:02d}:00 "
                      f"+ 2h = {peak_end_plus_2:02d}:00), skip")
                unresolved += 1
                continue

        # Skip if ALL strategies already resolved
        all_resolved = all(
            strategies.get(s, {}).get("result") in ("WIN", "LOSS")
            for s in ("sigma", "p5", "mean")
        )
        if all_resolved:
            print(f"  ✅ {city:<30s}: allerede avgjort via hourly_check")

            # Still tally
            for sn in ("sigma", "p5", "mean"):
                s = strategies.get(sn, {})
                if s.get("result") == "WIN":
                    if sn == "sigma": sigma_wins += 1
                    elif sn == "p5": p5_wins += 1
                    elif sn == "mean": mean_wins += 1
                elif s.get("result") == "LOSS":
                    if sn == "sigma": sigma_losses += 1
                    elif sn == "p5": p5_losses += 1
                    elif sn == "mean": mean_losses += 1
            continue

        # Fetch archive max for cities not yet confirmed
        archive_max = await _fetch_daily_max(lat, lon, tz, city_target)
        if archive_max is not None:
            pdata["peak_detected_at"] = _now_utc()

            # Record OUR observed peak (📡) only. WIN/LOSS is decided by
            # Polymarket resolution, never by round(archive) == spill.
            for strat_name in ("sigma", "p5", "mean"):
                strat = strategies.get(strat_name, {})
                strat["actual_peak"] = round(archive_max, 1)
            _resolve_strategies_vs_polymarket(pdata, city)

            # Generate recommendation
            _update_recommendation(pdata)

            # Tally
            sigma_res = strategies.get("sigma", {}).get("result", "")
            p5_res = strategies.get("p5", {}).get("result", "")
            mean_res = strategies.get("mean", {}).get("result", "")

            if sigma_res == "WIN": sigma_wins += 1
            elif sigma_res == "LOSS": sigma_losses += 1
            else: unresolved += 1
            if p5_res == "WIN": p5_wins += 1
            elif p5_res == "LOSS": p5_losses += 1
            if mean_res == "WIN": mean_wins += 1
            elif mean_res == "LOSS": mean_losses += 1

            sigma_spill = strategies.get("sigma", {}).get("spill", "?")
            result_icon = "✅" if sigma_res == "WIN" else "❌"
            print(f"  {result_icon} {city:<30s}: arkiv-maks={archive_max:.1f}°C "
                  f"(sigma={sigma_spill}°C→{sigma_res}, "
                  f"p5={strategies.get('p5',{}).get('spill','?')}°C→{p5_res}, "
                  f"mean={strategies.get('mean',{}).get('spill','?')}°C→{mean_res})")
        else:
            print(f"  ⚠️ {city:<30s}: kunne ikke hente arkivdata — markert som uavgjort")
            unresolved += 1

    # Update summary with per-strategy results
    entry["summary"] = {
        "sigma_wins": sigma_wins,
        "sigma_losses": sigma_losses,
        "p5_wins": p5_wins,
        "p5_losses": p5_losses,
        "mean_wins": mean_wins,
        "mean_losses": mean_losses,
        "unresolved": unresolved,
    }
    entry["phase"] = "daily_close"
    entry["last_updated"] = _now_utc()

    # Cross-reference our peaks with Polymarket resolved outcomes (also logs
    # mean(round)-vs-Polymarket pm_result / mean_pm_winners per city).
    await _verify_peaks_vs_market(entry, predictions, log_data, today)

    # Update cumulative (idempotent — recomputed from every run's summary so a
    # re-run of daily_close never double-counts and the totals always match the
    # predictions-derived report numbers). Mean-vs-PM wins/losses are counted
    # from each city's persisted pm_result.
    _recompute_cumulative_from_runs(log_data)

    _save_log(log_data)

    # Generate daily markdown report
    _generate_markdown_report(log_data, entry)

    # Print summary
    total_sigma = sigma_wins + sigma_losses
    total_p5 = p5_wins + p5_losses
    total_mean = mean_wins + mean_losses

    print(f"\n{'─'*60}")
    print(f"  📊 DAGENS RESULTATER ({len(predictions)} BYER, 3 STRATEGIER):")
    print(f"     Sigma (μ−kσ):  V:{sigma_wins} T:{sigma_losses}  "
          f"({round(sigma_wins/max(1,total_sigma)*100,1)}%)")
    print(f"     P5-basert:      V:{p5_wins} T:{p5_losses}  "
          f"({round(p5_wins/max(1,total_p5)*100,1)}%)")
    print(f"     Mean-basert:    V:{mean_wins} T:{mean_losses}  "
          f"({round(mean_wins/max(1,total_mean)*100,1)}%)")
    if unresolved:
        print(f"     ⚠️ Uoppgjorte: {unresolved}")
    print(f"  📄 Rapport: _quality_report.md")
    print(f"{'─'*60}\n")


# =============================================================================
# --mode full_report
# =============================================================================

def full_report_mode() -> None:
    """Generate comprehensive markdown report from all historical data."""
    log_data = _load_log()
    runs = log_data.get("runs", [])
    cum = log_data.get("cumulative", {})

    print("╔══════════════════════════════════════════════════╗")
    print("║     MODELLKVALITET — FULL RAPPORT               ║")
    print("╚══════════════════════════════════════════════════╝\n")

    if not runs:
        print("  Ingen data i loggen. Kjør --mode daily_bma først.\n")
        return

    total_cities = sum(len(r.get("predictions", {})) for r in runs)

    # Aggregate per-strategy wins
    sigma_wins = sum(r.get("summary", {}).get("sigma_wins", 0) for r in runs)
    sigma_losses = sum(r.get("summary", {}).get("sigma_losses", 0) for r in runs)
    p5_wins = sum(r.get("summary", {}).get("p5_wins", 0) for r in runs)
    p5_losses = sum(r.get("summary", {}).get("p5_losses", 0) for r in runs)
    mean_wins = sum(r.get("summary", {}).get("mean_wins", 0) for r in runs)
    mean_losses = sum(r.get("summary", {}).get("mean_losses", 0) for r in runs)

    sigma_total = sigma_wins + sigma_losses
    p5_total = p5_wins + p5_losses
    mean_total = mean_wins + mean_losses

    print(f"  Dager kjørt:         {len({r.get('run_date') for r in runs if r.get('run_date')})}")
    print(f"  Totalt by-prediksjoner: {total_cities}\n")

    print(f"  📊 PER-STRATEGI RESULTATER:")
    print(f"     Sigma (μ−kσ):  V:{sigma_wins} T:{sigma_losses}  "
          f"({round(sigma_wins/max(1,sigma_total)*100,1)}%)")
    print(f"     P5-basert:      V:{p5_wins} T:{p5_losses}  "
          f"({round(p5_wins/max(1,p5_total)*100,1)}%)")
    print(f"     Mean-basert:    V:{mean_wins} T:{mean_losses}  "
          f"({round(mean_wins/max(1,mean_total)*100,1)}%)")

    # Best strategy
    rates = {
        "Sigma (μ−kσ)": round(sigma_wins / max(1, sigma_total) * 100, 1),
        "P5-basert": round(p5_wins / max(1, p5_total) * 100, 1),
        "Mean-basert": round(mean_wins / max(1, mean_total) * 100, 1),
    }
    best = max(rates, key=lambda k: rates[k])
    print(f"\n  🏆 BESTE STRATEGI: {best} ({rates[best]}%)")

    # Generate full report file
    _generate_markdown_report(log_data, None)

    print(f"\n  📄 Full rapport skrevet til _quality_report.md\n")


def _tz_to_region(tz: str) -> str:
    """Map timezone string to geographic region."""
    if not tz:
        return "Unknown"
    parts = tz.split("/")
    if len(parts) >= 1:
        continent = parts[0]
        mapping = {
            "Asia": "Asia", "Europe": "Europe", "America": "Americas",
            "Africa": "Africa", "Pacific": "Oceania", "Australia": "Oceania",
            "Indian": "Asia", "Atlantic": "Americas",
        }
        return mapping.get(continent, "Other")
    return "Unknown"


def _add_city_win_rate_section(lines: list[str], runs: list) -> None:
    """Add per-city win rate table (sigma strategy) sorted by win rate.

    Rates are only shown once a city has at least MIN_SAMPLE resolved bets;
    below that the row renders "N/A — not enough data". Sample size is always
    shown next to every rate.
    """
    city_wl: dict[str, dict] = {}
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            if city not in city_wl:
                city_wl[city] = {"wins": 0, "losses": 0}
            if result == "WIN":
                city_wl[city]["wins"] += 1
            else:
                city_wl[city]["losses"] += 1

    city_rates = []
    for city, wl in city_wl.items():
        total = wl["wins"] + wl["losses"]
        rate = round(wl["wins"] / total * 100, 1) if total >= MIN_SAMPLE else None
        city_rates.append((city, wl["wins"], wl["losses"], rate, total))
    city_rates.sort(key=lambda x: (x[3] is None, -(x[3] or 0.0), -x[4], x[0]))

    if not city_rates:
        return

    def _rate_cell(rate, total):
        if rate is None:
            return f"N/A — not enough data (n={total})"
        return f"{rate}% (n={total})"

    lines.append("## 🏆 Win Rate Per City — Sigma Strategy")
    lines.append("")
    lines.append("### 🏆 BESTE BYER")
    lines.append("")
    lines.append("| # | City | Record | Win Rate |")
    lines.append("|---|---|---|---|")
    for i, (city, w, l, rate, total) in enumerate(city_rates[:10]):
        lines.append(f"| {i+1} | {city} | {w}W/{l}L | {_rate_cell(rate, total)} |")
    lines.append("")

    lines.append("### 📉 SVESTE BYER")
    lines.append("")
    lines.append("| # | City | Record | Win Rate |")
    lines.append("|---|---|---|---|")
    worst = city_rates[-10:] if len(city_rates) >= 10 else []
    for i, (city, w, l, rate, total) in enumerate(reversed(worst)):
        rank = len(city_rates) - len(worst) + i + 1
        lines.append(f"| {rank} | {city} | {w}W/{l}L | {_rate_cell(rate, total)} |")
    lines.append("")


def _add_model_agreement_section(lines: list[str], runs: list) -> None:
    """Add model agreement vs win rate section."""
    tiers: dict[str, dict] = {
        "8/8 enige": {"lo": 8, "hi": 9, "pos": 0, "wins": 0},
        "7/8 enige": {"lo": 7, "hi": 8, "pos": 0, "wins": 0},
        "6/8 enige": {"lo": 6, "hi": 7, "pos": 0, "wins": 0},
        "<6 enige":   {"lo": 0, "hi": 6, "pos": 0, "wins": 0},
    }
    for run in runs:
        for pdata in run.get("predictions", {}).values():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            mc = pdata.get("models", 0)
            for label, t in tiers.items():
                if t["lo"] <= mc < t["hi"]:
                    t["pos"] += 1
                    if result == "WIN":
                        t["wins"] += 1
                    break

    lines.append("## 📊 Model Agreement & Win Rate")
    lines.append("")
    lines.append("| Agreement | Positions | Record | Win Rate |")
    lines.append("|---|---|---|---|")
    for label, t in tiers.items():
        losses = t["pos"] - t["wins"]
        wr = round(t["wins"] / max(1, t["pos"]) * 100, 1) if t["pos"] > 0 else "N/A"
        wr_str = f"{wr}%" if isinstance(wr, (int, float)) else wr
        lines.append(f"| {label} | {t['pos']} | {t['wins']}W/{losses}L | {wr_str} |")
    lines.append("")


def _add_range_accuracy_section(lines: list[str], runs: list) -> None:
    """Add P5-P95 range size vs accuracy section."""
    tiers = [
        ("Smal (<2°C)", 0, 2),
        ("Medium (2-4°C)", 2, 4),
        ("Bred (>4°C)", 4, 999),
    ]
    tier_data = [{"label": label, "pos": 0, "wins": 0} for label, _, _ in tiers]

    for run in runs:
        for pdata in run.get("predictions", {}).values():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            p5 = pdata.get("p5", 0)
            p95 = pdata.get("p95", 0)
            rng = abs(p95 - p5) if p95 and p5 else 2.0
            for i, (_, lo, hi) in enumerate(tiers):
                if lo <= rng < hi:
                    tier_data[i]["pos"] += 1
                    if result == "WIN":
                        tier_data[i]["wins"] += 1
                    break

    lines.append("## 📏 P5-P95 Range & Accuracy")
    lines.append("")
    lines.append("| Range Size | Positions | Record | Win Rate |")
    lines.append("|---|---|---|---|")
    for td in tier_data:
        losses = td["pos"] - td["wins"]
        wr = round(td["wins"] / max(1, td["pos"]) * 100, 1) if td["pos"] > 0 else "N/A"
        wr_str = f"{wr}%" if isinstance(wr, (int, float)) else wr
        lines.append(f"| {td['label']} | {td['pos']} | {td['wins']}W/{losses}L | {wr_str} |")
    lines.append("")


def _add_optimal_strategy_section(lines: list[str], runs: list) -> None:
    """Add best strategy per confidence level section."""
    conf_tiers = [
        (">80%", 0.8, 1.0, "🟢"),
        ("70-80%", 0.7, 0.8, "🟠"),
        ("60-70%", 0.6, 0.7, "🟡"),
        ("50-60%", 0.5, 0.6, "🔴"),
        ("<50%", 0.0, 0.5, "🔴"),
    ]

    results = []
    for label, lo, hi, icon in conf_tiers:
        results.append({
            "label": label, "icon": icon,
            "sigma_pos": 0, "sigma_wins": 0,
            "p5_pos": 0, "p5_wins": 0,
            "mean_pos": 0, "mean_wins": 0,
        })

    for run in runs:
        for pdata in run.get("predictions", {}).values():
            conf = pdata.get("confidence", 0)
            strategies = pdata.get("strategies", {})
            for i, (_, lo, hi, _) in enumerate(conf_tiers):
                if lo <= conf < hi or (hi == 1.0 and conf >= 0.8):
                    for sn in ("sigma", "p5", "mean"):
                        s = strategies.get(sn, {})
                        r = s.get("result", "")
                        if r in ("WIN", "LOSS"):
                            results[i][f"{sn}_pos"] += 1
                            if r == "WIN":
                                results[i][f"{sn}_wins"] += 1
                    break

    lines.append("## 📊 Optimal Strategy by Confidence Level")
    lines.append("")
    lines.append("| Tier | 🎯 Sigma | 🛡️ P5 | 📊 Mean | 🏆 Best |")
    lines.append("|---|---|---|---|---|")
    for r in results:
        s_wr = round(r["sigma_wins"] / max(1, r["sigma_pos"]) * 100, 1) if r["sigma_pos"] > 0 else 0
        p_wr = round(r["p5_wins"] / max(1, r["p5_pos"]) * 100, 1) if r["p5_pos"] > 0 else 0
        m_wr = round(r["mean_wins"] / max(1, r["mean_pos"]) * 100, 1) if r["mean_pos"] > 0 else 0
        best_name = "Sigma"
        best_rate = s_wr
        if p_wr > best_rate:
            best_name = "P5"
            best_rate = p_wr
        if m_wr > best_rate:
            best_name = "Mean"
            best_rate = m_wr
        lines.append(
            f"| {r['icon']} {r['label']} | {s_wr}% | {p_wr}% | {m_wr}% | **{best_name} ({best_rate}%)** |"
        )
    lines.append("")


def _add_cumulative_edge_section(lines: list[str], runs: list) -> None:
    """Add cumulative edge tracker — betting $100 per sigma rec."""
    days_data = []
    for run in runs:
        rd = run.get("run_date", "?")
        s = run.get("summary", {})
        sw = s.get("sigma_wins", 0)
        sl = s.get("sigma_losses", 0)
        if sw + sl == 0:
            continue
        day_edge = sw * 39 - sl * 100
        days_data.append({"date": rd, "wins": sw, "losses": sl, "edge": day_edge})

    if not days_data:
        return

    lines.append("## 📈 Cumulative Edge Tracker")
    lines.append("")
    lines.append("*Simulates betting $100 on each sigma-recommended position. Wins return $139 (odds ~1.39).*")
    lines.append("")
    lines.append("| Date | Sigma Record | Daily Edge | Cumulative |")
    lines.append("|---|---|---|---|")

    cum = 0
    for d in days_data[-14:]:
        cum += d["edge"]
        lines.append(f"| {d['date']} | {d['wins']}W/{d['losses']}L | {d['edge']:+d} units | {cum:+d} units |")
    lines.append("")


def _add_region_performance_section(lines: list[str], runs: list) -> None:
    """Add timezone/region performance section."""
    region_data: dict[str, dict] = {}
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            tz = pdata.get("_tz", "UTC")
            region = _tz_to_region(tz)
            if region not in region_data:
                region_data[region] = {"pos": 0, "wins": 0}
            region_data[region]["pos"] += 1
            if result == "WIN":
                region_data[region]["wins"] += 1

    sorted_regions = sorted(
        region_data.items(),
        key=lambda kv: kv[1]["wins"] / max(1, kv[1]["pos"]),
        reverse=True,
    )

    lines.append("## 🌍 Region Performance")
    lines.append("")
    lines.append("| Region | Positions | Record | Win Rate |")
    lines.append("|---|---|---|---|")
    for region, data in sorted_regions:
        wr = round(data["wins"] / max(1, data["pos"]) * 100, 1) if data["pos"] > 0 else "N/A"
        wr_str = f"{wr}%" if isinstance(wr, (int, float)) else wr
        lines.append(f"| {region} | {data['pos']} | {data['wins']}W/{data['pos'] - data['wins']}L | {wr_str} |")
    lines.append("")


def _add_arbitrage_stats_section(lines: list[str], runs: list) -> None:
    """Add arbitrage win/loss stats (SHORT vs BUY opportunities)."""
    arb_summary = _summarize_arbitrage(runs)

    short = arb_summary["short"]
    buy = arb_summary["buy"]
    total_arb = arb_summary["total"]

    if total_arb["wins"] + total_arb["losses"] == 0:
        return

    lines.append("## 💰 Arbitrage Stats")
    lines.append("")
    lines.append(f"| Action | Wins | Losses | Win Rate |")
    lines.append(f"|--------|------|--------|----------|")
    lines.append(f"| 🔴 SHORT | {short['wins']} | {short['losses']} | {short['rate']}% |")
    lines.append(f"| 🟢 BUY   | {buy['wins']} | {buy['losses']} | {buy['rate']}% |")
    lines.append(f"| **Total** | **{total_arb['wins']}** | **{total_arb['losses']}** | **{total_arb['rate']}%** |")
    lines.append("")


def _add_uhi_accuracy_section(lines: list[str], runs: list) -> None:
    """Add UHI adjustment accuracy section."""
    high_uhi_errors = []
    low_uhi_errors = []
    for run in runs:
        for pdata in run.get("predictions", {}).values():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result not in ("WIN", "LOSS"):
                continue
            actual = sigma.get("actual_peak")
            bma_mean = pdata.get("bma_mean")
            if actual is None or bma_mean is None:
                continue
            error = abs(float(actual) - float(bma_mean))
            uhi = pdata.get("_uhi_adjustment", 0)
            if uhi >= 1.0:
                high_uhi_errors.append(error)
            elif uhi <= 0.5:
                low_uhi_errors.append(error)

    avg_high = round(sum(high_uhi_errors) / max(1, len(high_uhi_errors)), 2) if high_uhi_errors else "—"
    avg_low = round(sum(low_uhi_errors) / max(1, len(low_uhi_errors)), 2) if low_uhi_errors else "—"

    lines.append("## 🏙️ UHI Adjustment Accuracy")
    lines.append("")
    lines.append("| UHI Category | Count | Avg BMA Error |")
    lines.append("|---|---|---|")
    lines.append(f"| High UHI cities (≥1.0°C) | {len(high_uhi_errors)} | {avg_high}°C |")
    lines.append(f"| Low UHI cities (≤0.5°C) | {len(low_uhi_errors)} | {avg_low}°C |")
    lines.append("")


# =============================================================================
# Edge Impact Analysis — A/B test each feature
# =============================================================================

def _impact_label(impact: float) -> str:
    """Label an edge impact percentage."""
    if impact > 3:
        return "✅ REAL EDGE"
    elif impact >= 1:
        return "🟡 MARGINAL"
    else:
        return "🔴 IMAGINED / NOISE"


def _analyze_edge_impact() -> str:
    """Read all historical runs and compute edge impact analysis.

    Compares feature-on vs feature-off win rates to determine
    whether each edge improvement actually works.

    Returns formatted multiline string.
    """
    log_data = _load_log()
    runs = log_data.get("runs", [])

    # Collect all resolved predictions with their feature flags
    all_preds: list[dict[str, Any]] = []
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result in ("WIN", "LOSS"):
                features = pdata.get("_features", {})
                all_preds.append({
                    "city": city,
                    "result": result,
                    "models": pdata.get("models", 0),
                    "spread": abs(pdata.get("p95", 0) - pdata.get("p5", 0)),
                    "k": pdata.get("strategies", {}).get("sigma", {}).get("k", 0.5),
                    "uhi": pdata.get("_uhi_adjustment", 0),
                    "features": features,
                    "confidence": pdata.get("confidence", 0),
                })

    if len(all_preds) < 5:
        return "  ⚠️ For få resolved prediksjoner til edge impact analyse (<5).\n"

    lines: list[str] = []
    lines.append("")
    lines.append("═" * 60)
    lines.append("📊 EDGE IMPACT ANALYSIS — Real vs Imagined")
    lines.append("═" * 60)
    lines.append("")

    def _rate(group: list[dict]) -> tuple[int, int, float]:
        wins = sum(1 for p in group if p["result"] == "WIN")
        losses = len(group) - wins
        rate = round(wins / max(1, len(group)) * 100, 1)
        return wins, losses, rate

    # ── 1. MODEL WEIGHTING: high model agreement vs low ──
    high_weight = [p for p in all_preds if p["models"] >= 8]
    low_weight = [p for p in all_preds if p["models"] <= 4]

    if high_weight and low_weight:
        hw_w, hw_l, hw_rate = _rate(high_weight)
        lw_w, lw_l, lw_rate = _rate(low_weight)
        impact = round(hw_rate - lw_rate, 1)
        lines.append("🔬 MODEL WEIGHTING:")
        lines.append(f"   With weights (≥8 models):    {hw_w}W/{hw_l}L = {hw_rate}%")
        lines.append(f"   Without (≤4 models):         {lw_w}W/{lw_l}L = {lw_rate}%")
        lines.append(f"   Impact: {impact:+.1f}% → " + _impact_label(impact))
        lines.append("")

    # ── 2. SPREAD FILTERING ──
    narrow = [p for p in all_preds if p["spread"] < 2.0]
    medium = [p for p in all_preds if 2.0 <= p["spread"] < 4.0]
    wide = [p for p in all_preds if p["spread"] >= 4.0]

    lines.append("📏 SPREAD FILTERING:")
    for label, group in [("Narrow (<2°C)", narrow), ("Medium (2-4°C)", medium),
                          ("Wide (>4°C)", wide)]:
        if group:
            w, l, rate = _rate(group)
            lines.append(f"   {label:<20s} {w}W/{l}L = {rate}%")

    all_w, all_l, all_rate = _rate(all_preds)
    if narrow:
        nw_w, nw_l, nw_rate = _rate(narrow)
        impact = round(nw_rate - all_rate, 1)
        lines.append(f"   All spreads:                 {all_w}W/{all_l}L = {all_rate}%")
        lines.append(f"   Narrow Impact: {impact:+.1f}% → " + _impact_label(impact))
    lines.append("")

    # ── 3. DYNAMIC k ──
    high_k = [p for p in all_preds if p["k"] > 0.5]
    low_k = [p for p in all_preds if p["k"] <= 0.5]

    if high_k and low_k:
        hk_w, hk_l, hk_rate = _rate(high_k)
        lk_w, lk_l, lk_rate = _rate(low_k)
        impact = round(hk_rate - lk_rate, 1)
        lines.append("🎯 DYNAMIC k:")
        lines.append(f"   With dynamic k (k>0.5):      {hk_w}W/{hk_l}L = {hk_rate}%")
        lines.append(f"   Conservative k (≤0.5):       {lk_w}W/{lk_l}L = {lk_rate}%")
        lines.append(f"   Impact: {impact:+.1f}% → " + _impact_label(impact))
    lines.append("")

    # ── 4. UHI ADJUSTMENT ──
    uhi_yes = [p for p in all_preds if p["uhi"] > 0.5]
    uhi_no = [p for p in all_preds if p["uhi"] <= 0.5]

    if uhi_yes and uhi_no:
        uy_w, uy_l, uy_rate = _rate(uhi_yes)
        un_w, un_l, un_rate = _rate(uhi_no)
        impact = round(uy_rate - un_rate, 1)
        lines.append("🏙️ UHI ADJUSTMENT:")
        lines.append(f"   UHI adjusted (≥0.5°C):       {uy_w}W/{uy_l}L = {uy_rate}%")
        lines.append(f"   No UHI (<0.5°C):             {un_w}W/{un_l}L = {un_rate}%")
        lines.append(f"   Impact: {impact:+.1f}% → " + _impact_label(impact))
    lines.append("")

    # ── 5. BEST FEATURE COMBOS ──
    lines.append("🏆 BEST FEATURE COMBOS")
    lines.append("─" * 40)
    combos: dict[str, dict[str, int]] = {}
    for p in all_preds:
        has_weights = p["models"] >= 7
        is_narrow = p["spread"] < 2.0
        has_dyn_k = p["k"] > 0.5
        has_uhi = p["uhi"] > 0.5

        parts: list[str] = []
        if has_weights:
            parts.append("Weights")
        if is_narrow:
            parts.append("Narrow spread")
        if has_dyn_k:
            parts.append("Dynamic k")
        if has_uhi:
            parts.append("UHI")

        combo_key = " + ".join(parts) if parts else "Baseline (none)"
        if combo_key not in combos:
            combos[combo_key] = {"wins": 0, "total": 0}
        combos[combo_key]["total"] += 1
        if p["result"] == "WIN":
            combos[combo_key]["wins"] += 1

    sorted_combos = sorted(combos.items(),
                           key=lambda x: x[1]["wins"] / max(1, x[1]["total"]),
                           reverse=True)
    for label, stats in sorted_combos:
        rate = round(stats["wins"] / max(1, stats["total"]) * 100, 1)
        lines.append(f"   {label:<35s} {rate}% ({stats['wins']}W/{stats['total'] - stats['wins']}L)")

    lines.append("")
    return "\n".join(lines)


# =============================================================================
# Markdown Report Generator
# =============================================================================

def _tally_city_3strategy(runs: list) -> dict[str, dict]:
    """Return {city: {strategy: {"wins": n, "losses": n}}} across all runs."""
    tally: dict[str, dict] = {}
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            strategies = pdata.get("strategies", {}) or {}
            rec = tally.setdefault(
                city, {sn: {"wins": 0, "losses": 0} for sn in ("sigma", "p5", "mean")}
            )
            for sn in ("sigma", "p5", "mean"):
                res = strategies.get(sn, {}).get("result")
                if res == "WIN":
                    rec[sn]["wins"] += 1
                elif res == "LOSS":
                    rec[sn]["losses"] += 1
    return tally


def _generate_markdown_report(log_data: dict, today_entry: dict | None) -> None:
    """Generate _quality_report.md from log data.

    If today_entry is provided, generates a daily-focused report.
    Otherwise generates a full historical report.
    """
    runs = log_data.get("runs", [])
    cum = log_data.get("cumulative", {})

    sigma_wins = sum(r.get("summary", {}).get("sigma_wins", 0) for r in runs)
    sigma_losses = sum(r.get("summary", {}).get("sigma_losses", 0) for r in runs)
    p5_wins = sum(r.get("summary", {}).get("p5_wins", 0) for r in runs)
    p5_losses = sum(r.get("summary", {}).get("p5_losses", 0) for r in runs)
    mean_wins = sum(r.get("summary", {}).get("mean_wins", 0) for r in runs)
    mean_losses = sum(r.get("summary", {}).get("mean_losses", 0) for r in runs)

    sigma_total = sigma_wins + sigma_losses
    p5_total = p5_wins + p5_losses
    mean_total = mean_wins + mean_losses

    today = _today_iso()
    lines: list[str] = []
    lines.append("# Model Quality Report")
    lines.append(f"\n**Generated:** {_now_utc()}")
    lines.append(f"**Days tracked:** {len({r.get('run_date') for r in runs if r.get('run_date')})}")
    lines.append("")

    # -- Today's results (if available)
    if today_entry:
        lines.append("## Today's Results — All 3 Strategies")
        lines.append("")
        top5 = today_entry.get("top_5_confidence", [])
        preds = today_entry.get("predictions", {})
        summary = today_entry.get("summary", {})

        lines.append(f"| # | City | Sigma Spill | P5 Spill | Mean Spill | Actual Peak | Sigma | P5 | Mean | Rec |")
        lines.append(f"|---|------|------------|---------|-----------|-------------|-------|----|------|-----|")
        for i, city in enumerate(top5):
            pdata = preds.get(city, {})
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            p5s = strategies.get("p5", {})
            means = strategies.get("mean", {})

            sigma_spill = sigma.get("spill", "?")
            p5_spill = p5s.get("spill", "?")
            mean_spill = means.get("spill", "?")
            actual = sigma.get("actual_peak", "—")
            actual_str = f"{actual:.1f}°C" if isinstance(actual, (int, float)) else str(actual)

            def _res_emoji(r):
                return "✅" if r == "WIN" else ("❌" if r == "LOSS" else "⏳")

            sigma_r = _res_emoji(sigma.get("result", ""))
            p5_r = _res_emoji(p5s.get("result", ""))
            mean_r = _res_emoji(means.get("result", ""))
            rec = pdata.get("recommendation", "—") or "—"
            # Truncate long recs
            if len(rec) > 40:
                rec = rec[:37] + "..."

            lines.append(f"| {i+1} | {city} | {sigma_spill}°C | {p5_spill}°C | {mean_spill}°C | {actual_str} | {sigma_r} | {p5_r} | {mean_r} | {rec} |")

        lines.append("")
        sw = summary.get("sigma_wins", 0)
        sl = summary.get("sigma_losses", 0)
        pw = summary.get("p5_wins", 0)
        pl = summary.get("p5_losses", 0)
        mw = summary.get("mean_wins", 0)
        ml = summary.get("mean_losses", 0)
        lines.append(f"**Today — Sigma:** {sw}W/{sl}L ({round(sw/max(1,sw+sl)*100,1)}%) | "
                     f"**P5:** {pw}W/{pl}L ({round(pw/max(1,pw+pl)*100,1)}%) | "
                     f"**Mean:** {mw}W/{ml}L ({round(mw/max(1,mw+ml)*100,1)}%)")
        lines.append("")

    # -- Cumulative per-strategy stats
    lines.append("## Cumulative Strategy Performance")
    lines.append("")
    lines.append(f"| Strategy | Wins | Losses | Win Rate |")
    lines.append(f"|----------|------|--------|----------|")
    lines.append(f"| 🎯 Sigma (μ−kσ) | {sigma_wins} | {sigma_losses} | {round(sigma_wins/max(1,sigma_total)*100,1)}% |")
    lines.append(f"| 🛡️ P5-Basert | {p5_wins} | {p5_losses} | {round(p5_wins/max(1,p5_total)*100,1)}% |")
    lines.append(f"| 📊 Mean-Basert | {mean_wins} | {mean_losses} | {round(mean_wins/max(1,mean_total)*100,1)}% |")
    lines.append("")

    # -- Per-city 3-strategy W/L with min-sample gating
    lines.append("## Per-City 3-Strategy W/L (Cumulative)")
    lines.append("")
    lines.append("| City | Sigma W/L | Sigma Rate | P5 W/L | P5 Rate | Mean W/L | Mean Rate |")
    lines.append("|------|-----------|------------|--------|---------|----------|-----------|")
    for city, rec in sorted(_tally_city_3strategy(runs).items()):
        cells: list[str] = []
        for sn in ("sigma", "p5", "mean"):
            w = rec[sn]["wins"]
            l = rec[sn]["losses"]
            n = w + l
            rate = f"{round(w / n * 100, 1)}%" if n >= MIN_SAMPLE else "N/A — not enough data"
            cells.append(f"{w}W/{l}L (n={n})")
            cells.append(rate)
        lines.append(
            f"| {city} | {cells[0]} | {cells[1]} | {cells[2]} | {cells[3]} | {cells[4]} | {cells[5]} |"
        )
    lines.append("")

    # Write
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# CLI Entry Point
# =============================================================================

def _extract_date_from_question(question: str) -> str | None:
    """Extract target date from Polymarket question text. Returns ISO date or None."""
    import re as _re
    # Full + abbreviated: "August 11, 2026" / "Aug 11" / "August 11"
    months_full = ["January","February","March","April","May","June",
                   "July","August","September","October","November","December"]
    months_abbr = ["Jan","Feb","Mar","Apr","May","Jun",
                   "Jul","Aug","Sep","Oct","Nov","Dec"]
    month_map = {}
    for i, m in enumerate(months_full):
        month_map[m.lower()] = i + 1
        month_map[months_abbr[i].lower()] = i + 1
    month_pattern = "|".join(months_full + months_abbr)
    m = _re.search(rf'({month_pattern})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?', question, _re.IGNORECASE)
    if m:
        month = month_map.get(m.group(1).lower(), 1)
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else 2026
        return f"{year:04d}-{month:02d}-{day:02d}"
    # Try "YYYY-MM-DD"
    m = _re.search(r'(\d{4})-(\d{2})-(\d{2})', question)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


def _parse_market_question(question: str) -> dict[str, Any] | None:
    """Classify a resolved Polymarket temperature market from its question text.

    Returns None if the question cannot be parsed. Otherwise returns a dict:
      - type: "point" | "threshold"
      - unit: "C" | "F"
      - value: numeric midpoint in native unit (point markets only)
      - bucket: original temperature label (e.g. "86-87°F", "29°C")
      - lower_bound_c: threshold lower bound in °C (threshold markets)
      - lower_bound_f: threshold lower bound in °F (°F threshold markets)
    """
    q = question or ""

    # Threshold markets: "X°C or higher" / "at least" / "or above" / "≥" / "or below".
    # These are binary bounds, not resolved point temperatures — never treat the
    # threshold number as the actual resolved temperature.
    th = re.search(
        r'(\d+(?:\.\d+)?)\s*°\s*([CF])\s*'
        r'(?:or\s+higher|or\s+above|at\s+least|or\s+more|or\s+below|or\s+lower|≥|≤)',
        q, re.IGNORECASE,
    )
    if th:
        val = float(th.group(1))
        unit = th.group(2).upper()
        info: dict[str, Any] = {"type": "threshold", "unit": unit, "bucket": q.strip()}
        if unit == "F":
            info["lower_bound_f"] = val
            info["lower_bound_c"] = round((val - 32) * 5 / 9, 1)
        else:
            info["lower_bound_c"] = val
        return info

    # °F bucket markets: "between 86-87°F" or a single "92°F" bucket.
    if "°F" in q:
        m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°\s*F', q, re.IGNORECASE)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2))
            midpoint = (lo + hi) / 2.0
            return {
                "type": "point", "unit": "F", "value": midpoint,
                "bucket": m.group(0).strip(),
            }
        m = re.search(r'(\d+(?:\.\d+)?)\s*°\s*F', q, re.IGNORECASE)
        if m:
            return {
                "type": "point", "unit": "F", "value": float(m.group(1)),
                "bucket": m.group(0).strip(),
            }

    # °C point markets.
    m = re.search(r'(\d+(?:\.\d+)?)\s*°\s*C', q, re.IGNORECASE)
    if m:
        return {
            "type": "point", "unit": "C", "value": float(m.group(1)),
            "bucket": m.group(0).strip(),
        }
    return None


def _resolved_log_entry_to_info(data: dict[str, Any]) -> dict[str, Any] | None:
    """Normalize a _resolved_markets_log.json entry to a market-info dict.

    Backward compatible: entries without unit/type default to a °C point market.
    Entries whose temp_display/bucket contains °F are inferred as °F point markets
    even when the newer unit/type fields are absent.
    """
    mtype = data.get("type", "point")
    unit = (data.get("unit") or "").upper()
    display = data.get("bucket") or data.get("temp_display") or ""
    if not unit:
        unit = "F" if "°F" in str(display) else "C"

    if mtype == "threshold":
        info: dict[str, Any] = {
            "type": "threshold",
            "unit": unit,
            "bucket": display,
            "lower_bound_c": data.get("lower_bound_c"),
        }
        if data.get("lower_bound_f") is not None:
            info["lower_bound_f"] = data.get("lower_bound_f")
        return info

    # Point market — prefer the generic value field, then unit-specific fields.
    value = data.get("value")
    if value is None:
        value = data.get("temp_f") if unit == "F" else data.get("temp_c")
    if value is None:
        # Infer from a °F temp_display bucket (e.g. "78-79°F") when possible.
        if unit == "F":
            m = re.search(r'(\d+)\s*[-–]\s*(\d+)\s*°\s*F', str(display), re.IGNORECASE)
            if m:
                value = (int(m.group(1)) + int(m.group(2))) / 2.0
    if value is None:
        return None
    info: dict[str, Any] = {
        "type": "point",
        "unit": unit,
        "value": float(value),
        "bucket": display,
    }
    # Propagate native inclusive bucket bounds for °F bucket markets so
    # consumers can compare against the °C range instead of a rounded midpoint.
    if data.get("lo_c") is not None and data.get("hi_c") is not None:
        info["lo_c"] = data.get("lo_c")
        info["hi_c"] = data.get("hi_c")
        if data.get("lo_f") is not None:
            info["lo_f"] = data.get("lo_f")
        if data.get("hi_f") is not None:
            info["hi_f"] = data.get("hi_f")
    return info


def _load_market_resolved_details() -> dict[tuple[str, str], dict[str, Any]]:
    """Extract resolved market outcomes (unit-aware) from all sources.

    Returns {(city, date_iso): market_info}. Point markets carry a native-unit
    numeric ``value``; threshold markets carry ``type == "threshold"`` and a
    ``lower_bound_c`` (°C) bound plus an optional ``lower_bound_f`` (°F) bound.
    Consumers must branch on ``market_info["type"]`` before treating an entry
    as a point temperature — threshold bounds are never point temperatures.

    Source priority (most curated first):
      1. _resolved_markets_log.json — the comprehensive collector with
         explicit unit/type and inclusive bucket bounds.
      2. _market_prices.json — active-market prices; only point markets with a
         resolved YES outcome are used, thresholds are skipped.
      3. _peak_verification_log.json — only unit-bearing entries (legacy
         unit-less entries were °C-rounded and are ignored).
    """
    details: dict[tuple[str, str], dict[str, Any]] = {}

    # 1. Comprehensive resolved-markets collector (most authoritative).
    resolved_log = Path(_SCRIPT_DIR) / "_resolved_markets_log.json"
    if resolved_log.exists():
        try:
            rl = json.loads(resolved_log.read_text(encoding="utf-8"))
            for key_str, data in rl.get("markets", {}).items():
                if "||" not in key_str:
                    continue
                city, date_str = key_str.split("||", 1)
                info = _resolved_log_entry_to_info(data)
                if info is None:
                    continue
                key = (city, date_str)
                if key not in details:
                    details[key] = info
        except Exception:
            pass

    # 2. Active market prices (skip thresholds, never override the collector).
    market_path = Path(_SCRIPT_DIR) / "_market_prices.json"
    if market_path.exists():
        try:
            mp = json.loads(market_path.read_text(encoding="utf-8"))
            markets = mp if isinstance(mp, list) else mp.get("markets", [])
        except Exception:
            markets = []
        for m in markets:
            city = m.get("city", "")
            if not city or city == "Unknown":
                continue
            if m.get("question_type") != "highest":
                continue
            date_str = _extract_date_from_question(m.get("question", ""))
            if not date_str:
                continue
            for o in m.get("outcomes", []):
                price = o.get("price", 0)
                if price is None:
                    price = 0.0
                if float(price) > 0.95 and (o.get("label") or "").lower() == "yes":
                    info = _parse_market_question(m.get("question", ""))
                    if info and info.get("type") != "threshold" and info.get("value") is not None:
                        key = (city, date_str)
                        if key not in details:
                            details[key] = info
                    break

    # 3. Peak-verification log — only unit-bearing entries (post-fix format).
    if PEAK_VERIFICATION_LOG.exists():
        try:
            pv = json.loads(PEAK_VERIFICATION_LOG.read_text(encoding="utf-8"))
            for city_key, entry in pv.get("verifications", {}).items():
                mr = entry.get("market_resolved")
                vdate = entry.get("date") or entry.get("run_date", "")
                if mr is None or not vdate or not entry.get("unit"):
                    continue
                key = (city_key, vdate)
                if key not in details:
                    details[key] = {
                        "type": "point",
                        "unit": (entry.get("unit") or "C").upper(),
                        "value": float(mr),
                        "bucket": entry.get("market_display"),
                    }
        except Exception:
            pass

    return details


def _load_market_resolved_temps() -> dict[tuple[str, str], int]:
    """Legacy wrapper: resolved point markets as whole °C ints.

    Kept for backfill scripts (e.g. _populate_peak_verify.py). °F point markets
    are converted back to °C here for legacy consumers only; the main pipeline
    uses _load_market_resolved_details() so °F markets stay in °F.
    Threshold markets are excluded so they can never masquerade as point temps.
    """
    resolved: dict[tuple[str, str], int] = {}
    for key, info in _load_market_resolved_details().items():
        if info.get("type") != "point" or info.get("value") is None:
            continue
        value = float(info["value"])
        if info.get("unit") == "F":
            value = (value - 32) * 5 / 9
        resolved[key] = int(round(value))
    return resolved


def _log_peak_verification(
    city: str, date_str: str, our_peak: float,
    our_lat: float, our_lon: float,
    market_resolved: float | None, our_station: str = "",
    unit: str = "C", bucket: str | None = None,
) -> None:
    """Log peak verification: our archive peak vs Polymarket resolved outcome.

    Comparison runs in the market's native unit (°C or °F). °F markets are
    compared in °F — our °C peak is converted for the gap and nothing is
    converted back to °C for display.
    """
    unit = (unit or "C").upper()
    if market_resolved is None:
        return
    if unit == "F":
        our_native = float(our_peak) * 9.0 / 5.0 + 32.0
        market_native = float(market_resolved)
        ok_threshold = 1.0 * 9.0 / 5.0     # 1°C tolerance in °F
        minor_threshold = 2.0 * 9.0 / 5.0   # 2°C tolerance in °F
    else:
        our_native = float(our_peak)
        market_native = float(market_resolved)
        ok_threshold = 1.0
        minor_threshold = 2.0
    unit_label = "F" if unit == "F" else "C"

    pv_data = {"last_updated": _now_utc(), "verifications": {}}
    if PEAK_VERIFICATION_LOG.exists():
        try:
            pv_data = json.loads(PEAK_VERIFICATION_LOG.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    verifications = pv_data.setdefault("verifications", {})
    gap = round(our_native - market_native, 1)
    abs_gap = abs(gap)
    if abs_gap <= ok_threshold:
        verdict = "OK"
    elif abs_gap <= minor_threshold:
        verdict = "MINOR"
    else:
        verdict = "STATION_MISMATCH"
    note = ""
    if verdict == "STATION_MISMATCH":
        note = (
            f"Gap {gap:+.1f}{unit_label} suggests different weather stations. "
            f"Our API (lat={our_lat}, lon={our_lon}) may differ from Polymarket. "
            f"Investigate which station Polymarket uses for {city}."
        )
    elif verdict == "MINOR":
        note = f"Small gap of {gap:+.1f}{unit_label} - likely calibration or timing difference."
    else:
        note = f"Within {ok_threshold:.1f}{unit_label} tolerance - station match confirmed."
    entry = {
        "date": date_str, "our_peak": round(our_native, 1),
        "our_station": our_station or f"lat={our_lat},lon={our_lon}",
        "our_lat": our_lat, "our_lon": our_lon,
        "market_resolved": round(market_native, 1),
        "market_display": bucket,
        "gap": gap, "unit": unit_label,
        "verdict": verdict, "note": note, "logged_at": _now_utc(),
    }

    # Accumulate into the persistent daily peak-deviation tracker so the
    # daily-close path also builds history even when the stats script does
    # not run. Exception-safe: peak verification must never crash because of
    # tracker issues.
    try:
        import _peak_deviation_stats  # type: ignore[import-not-found]  # local import — same directory as tracker
        _peak_deviation_stats.upsert_peak_sample(
            city=city,
            date_str=date_str,
            our_peak=round(our_native, 1),
            market_resolved=round(market_native, 1),
            unit=unit_label,
            gap=gap,
            verdict=verdict,
        )
    except Exception:
        pass

    existing = verifications.get(city, {})
    if not (existing.get("date") == date_str and existing.get("our_peak") == round(our_native, 1)):
        verifications[city] = entry
        pv_data["last_updated"] = _now_utc()
        PEAK_VERIFICATION_LOG.write_text(
            json.dumps(pv_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        verdict_icon = {"OK": "OK", "MINOR": "MINOR", "STATION_MISMATCH": "STASJONSFEIL"}.get(verdict, "?")
        print(f"  PEAK VERIFY: {verdict_icon} {city}: var={our_native:.1f}{unit_label} vs "
              f"marked={market_native:.1f}{unit_label} gap={gap:+.1f}{unit_label} [{verdict}]")
    # Inject market_resolved into quality log for dashboard
    log_data = _load_log()
    target_date = date_str if date_str else _today_iso()
    for run in log_data.get("runs", []):
        if run.get("run_date") == target_date or run.get("target_date") == target_date:
            pdata = run.get("predictions", {}).get(city)
            if pdata is not None:
                pdata["_market_resolved"] = round(market_native, 1)
                pdata["_market_unit"] = unit_label
                pdata["_market_display"] = bucket
                pdata["_peak_gap"] = gap
                pdata["_verdict"] = verdict
                _save_log(log_data)
                break


def _spill_vs_polymarket_result(
    spill_c: float | int | None, market_info: dict[str, Any] | None
) -> str | None:
    """Resolve a strategy bucket (stored in °C) against a Polymarket outcome.

    The ONLY valid definition of WIN is: our bucket equals Polymarket's
    resolved bucket. It is never ``round(our live peak) == spill``.

    - °F bucket market (US): WIN iff lo_f <= spill(°F) <= hi_f (native bounds).
    - °C bucket market:       WIN iff lo_c <= spill(°C) <= hi_c.
    - °C point market:        WIN iff round(spill) == round(resolved °C value).
    - °F point market (rare): the resolved °F value is converted to °C first.

    Returns None when there is no resolvable numeric market outcome or no spill
    (the bet stays unresolved — never WIN/LOSS on a missing market).
    """
    if spill_c is None or not market_info or market_info.get("value") is None:
        return None
    lo_c = market_info.get("lo_c")
    hi_c = market_info.get("hi_c")
    lo_f = market_info.get("lo_f")
    hi_f = market_info.get("hi_f")
    if lo_f is not None and hi_f is not None:
        return "WIN" if lo_f <= float(spill_c) * 9.0 / 5.0 + 32.0 <= hi_f else "LOSS"
    if lo_c is not None and hi_c is not None:
        return "WIN" if float(lo_c) <= float(spill_c) <= float(hi_c) else "LOSS"
    unit = (market_info.get("unit") or "C").upper()
    value = float(market_info["value"])
    value_c = value if unit == "C" else (value - 32.0) * 5.0 / 9.0
    return "WIN" if int(round(float(spill_c))) == int(round(value_c)) else "LOSS"


def _mean_pm_result(market_info: dict[str, Any] | None, mean_spill: float | int | None) -> str | None:
    """Backward-compatible wrapper: mean(round) strategy vs Polymarket resolution."""
    return _spill_vs_polymarket_result(mean_spill, market_info)


def _spill_vs_threshold_result(
    spill_c: float | int | None, market_info: dict[str, Any] | None
) -> str | None:
    """Resolve a strategy bucket (°C) against a Polymarket threshold market.

    Threshold markets are binary bounds ("26°C or higher", "75°F or below")
    rather than resolved point temperatures. The market's lower bound is
    converted to °C by the collector, so all comparisons are in °C:

    - "or higher" / "at least" / "or above" → WIN iff round(spill) >= lower_bound_c
    - "or below" / "or lower"               → WIN iff round(spill) <  lower_bound_c

    Returns None when the market has no lower bound, the bucket direction is
    unrecognized, or the spill is missing (the bet stays unresolved).
    """
    if spill_c is None or not market_info:
        return None
    lower_bound_c = market_info.get("lower_bound_c")
    if lower_bound_c is None:
        return None
    try:
        lower_bound_c = float(lower_bound_c)
        rounded_spill = int(round(float(spill_c)))
    except (TypeError, ValueError):
        return None
    direction = str(market_info.get("bucket") or "").lower()
    if any(kw in direction for kw in ("or higher", "at least", "or above", "or more", "≥")):
        return "WIN" if rounded_spill >= lower_bound_c else "LOSS"
    if any(kw in direction for kw in ("or below", "or lower", "≤")):
        return "WIN" if rounded_spill < lower_bound_c else "LOSS"
    return None


def _resolve_strategies_vs_polymarket(
    pdata: dict, city: str, resolved_markets: dict | None = None
) -> None:
    """Set sigma/p5/mean ``result`` + ``pm_result`` from Polymarket resolution.

    WIN/LOSS is written only when a resolvable Polymarket outcome exists for
    (city, target_date); otherwise the strategies stay unresolved. This is the
    single source of truth for market-based resolution and is safe to call
    repeatedly (idempotent).
    """
    if resolved_markets is None:
        resolved_markets = _load_market_resolved_details()
    if not resolved_markets:
        return
    city_target = pdata.get("_target_date") or _today_iso()
    city_base = city.split(",")[0].strip()
    market_info = (
        resolved_markets.get((city, city_target))
        or resolved_markets.get((city_base, city_target))
    )
    if market_info is None:
        return
    is_threshold = market_info.get("type") == "threshold"
    if not is_threshold and market_info.get("value") is None:
        return
    strategies = _get_strategies(pdata)
    for sn in ("sigma", "p5", "mean"):
        strat = strategies.get(sn, {})
        if not strat:
            continue
        if is_threshold:
            res = _spill_vs_threshold_result(strat.get("spill"), market_info)
        else:
            res = _spill_vs_polymarket_result(strat.get("spill"), market_info)
        strat["pm_result"] = res
        if res is not None:
            strat["result"] = res


async def _verify_peaks_vs_market(
    entry: dict, predictions: list, log_data: dict,
    target_date: str | None = None,
) -> None:
    """Cross-reference our resolved peaks with Polymarket outcomes.
    Called after archive max is fetched in daily_close / post-peak.
    """
    resolved_markets = _load_market_resolved_details()
    if not resolved_markets:
        return
    today = target_date or _today_iso()
    preds_dict = entry.get("predictions", {})
    for city, pdata in preds_dict.items():
        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        actual_peak = sigma.get("actual_peak")
        if actual_peak is None:
            continue
        city_target = pdata.get("_target_date", today)
        city_base = city.split(",")[0].strip()
        # Match by (city, date) — strict date matching only
        market_info = (
            resolved_markets.get((city, city_target))
            or resolved_markets.get((city_base, city_target))
        )
        if market_info is None:
            continue
        # Threshold markets are binary bounds ("X°C or higher"), not resolved
        # point temperatures — exclude them from point-value gap comparisons.
        if market_info.get("type") == "threshold":
            continue
        market_value = market_info.get("value")
        if market_value is None:
            continue
        unit = (market_info.get("unit") or "C").upper()
        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        _log_peak_verification(
            city=city, date_str=city_target,
            our_peak=float(actual_peak), our_lat=float(lat), our_lon=float(lon),
            market_resolved=float(market_value),
            unit=unit,
            bucket=market_info.get("bucket"),
        )

    # Strategy-vs-Polymarket — independent of the Open-Meteo peak resolution
    # above, so cities that are predicted but not yet peak-resolved still get a
    # result/pm_result once their Polymarket market has resolved. Resolves all
    # three strategies (sigma/p5/mean) — never round(live peak) == spill.
    for city, pdata in preds_dict.items():
        _resolve_strategies_vs_polymarket(pdata, city, resolved_markets)

        mean = pdata.get("strategies", {}).get("mean", {})
        mean_spill = mean.get("spill")
        city_target = pdata.get("_target_date", today)
        city_base = city.split(",")[0].strip()
        market_info = (
            resolved_markets.get((city, city_target))
            or resolved_markets.get((city_base, city_target))
        )
        if mean_spill is None:
            # Market resolved but the strategy had no spill — the bet still
            # happened, so count it as a LOSS. Leave pm_result unset only when
            # there is genuinely no resolvable market resolution.
            if market_info is not None and market_info.get("value") is not None:
                mean["pm_result"] = "LOSS"
            continue
        pm_result = mean.get("pm_result")
        if pm_result == "WIN" and market_info is not None:
            winners = entry.setdefault("mean_pm_winners", [])
            winner = {
                "date": city_target,
                "city": city,
                "mean_spill": int(mean_spill),
                "pm_value": market_info.get("value"),
                "pm_unit": (market_info.get("unit") or "C").upper(),
                "pm_bucket": market_info.get("bucket"),
            }
            if not any(
                w.get("date") == winner["date"] and w.get("city") == winner["city"]
                for w in winners
            ):
                winners.append(winner)

    _save_log(log_data)


def backfill_mode() -> None:
    """Re-resolve ALL historical runs against the current resolved-markets log.

    Reloads _load_market_resolved_details() (point + threshold markets), re-runs
    _resolve_strategies_vs_polymarket() over every run/city, recomputes each
    run's summary and the cumulative totals, then saves the log. Idempotent:
    re-running overwrites results instead of double-counting.
    """
    print("╔══════════════════════════════════════════════════╗")
    print("║   MODELLKVALITET — BACKFILL (MARKEDSOPPGJØR)    ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    log_data = _load_log()
    runs = log_data.get("runs", [])
    if not runs:
        print("  Ingen runs i loggen — ingenting å backfille.\n")
        return

    resolved_markets = _load_market_resolved_details()
    if not resolved_markets:
        print("  Ingen resolvede markeder funnet — ingenting å gjøre.\n")
        return
    print(f"  Lastet {len(resolved_markets)} resolvede markeder (point + threshold).")

    updated = 0
    for run in runs:
        for city, pdata in (run.get("predictions") or {}).items():
            before = {
                sn: (pdata.get("strategies", {}) or {}).get(sn, {}).get("result")
                for sn in ("sigma", "p5", "mean")
            }
            _resolve_strategies_vs_polymarket(pdata, city, resolved_markets)
            after = {
                sn: (pdata.get("strategies", {}) or {}).get(sn, {}).get("result")
                for sn in ("sigma", "p5", "mean")
            }
            if before != after:
                updated += 1
        _recompute_summary(run)

    _recompute_cumulative_from_runs(log_data)
    _save_log(log_data)

    print(f"  Oppdaterte {updated} by-prediksjoner.")
    print(f"  Dager kjørt (unike): {log_data['cumulative'].get('total_days', 0)}")
    _generate_markdown_report(log_data, None)
    print(f"  Rapport skrevet til _quality_report.md\n")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Model Quality Tracker — BMA ensemble performance monitoring",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["daily_bma", "hourly_check", "hourly_active", "daily_close", "full_report", "backfill"],
        help="Run mode for GitHub Actions pipeline",
    )
    parser.add_argument(
        "--date",
        default=None,
        metavar="YYYY-MM-DD",
        help="Override the target date for daily_bma/daily_close (backfill support)",
    )

    args = parser.parse_args()

    if args.mode == "hourly_active":
        asyncio.run(hourly_active_mode())
    elif args.mode == "daily_bma":
        asyncio.run(daily_bma_mode(target_date=args.date))
    elif args.mode == "hourly_check":
        asyncio.run(hourly_check_mode())
    elif args.mode == "daily_close":
        asyncio.run(daily_close_mode(target_date=args.date))
    elif args.mode == "full_report":
        full_report_mode()
    elif args.mode == "backfill":
        backfill_mode()


if __name__ == "__main__":
    main()
