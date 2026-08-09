#!/usr/bin/env python3
"""
Model Quality Tracker — automated BMA ensemble quality monitoring.

Designed for GitHub Actions multi-run pipeline:
    --mode daily_bma     06:00 UTC — Run BMA for ALL 51 cities,
                                     lead_days=1 AND lead_days=2.
                                     Semaphore(5) protects against rate limits.
    --mode hourly_check  07:00-22:00 UTC — Check top 5 temps, detect peaks
    --mode daily_close   23:00 UTC — Finalize ALL 51 cities, compare 3 strategies
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
MAX_LOG_DAYS = 90  # Keep last 90 days in log
MAX_OBS_HISTORY = 144  # Max observations per city (~12 hours at 5-min, ample for GH)
MAX_RAPID_RUNTIME_HOURS = 4  # Max runtime for rapid polling (fits GH 6h limit)
RAPID_POLL_INTERVAL_MINUTES = 3  # Poll every 3 min during peak windows

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


def _compute_optimal_spill(
    mean_c: float, std_c: float, confidence: float, p5_c: float
) -> dict[str, float | int]:
    """Compute optimal bet levels using BMA statistics.

    For Polymarket "Highest temp ≥ T?" markets:
      P(win) = 1 - Φ((T - μ)/σ)  assuming normal distribution.

    Strategy: Sigma-Adjusted Bet Level
      suggested_spill = int(μ - k × σ)

    Where k is the risk-adjustment factor, dynamically set by confidence:

      | k   | Win Prob | Style                          |
      |-----|----------|--------------------------------|
      | 0   | 50%      | At mean — balanced, 50/50      |
      | 0.3 | 62%      | Aggressive (high conf)         |
      | 0.5 | 69%      | Conservative — good risk/reward|
      | 0.7 | 76%      | Cautious (low conf)            |
      | 0.84| 80%      | Safe — high confidence         |
      | 1.0 | 84%      | Very safe — 1σ below mean      |

    Also computes P5-based (ultra-conservative ~95%) and mean-based (50%) for comparison.
    """
    # Dynamic k based on confidence
    if confidence > 0.80:
        k = 0.3   # High confidence → aggressive
    elif confidence > 0.70:
        k = 0.5   # Medium → balanced
    else:
        k = 0.7   # Low confidence → conservative

    sigma_spill = int(mean_c - k * std_c)
    p5_spill = int(round(p5_c))
    mean_spill = int(round(mean_c))

    # Win probability under normal approximation: P(temp ≥ T) = 1 - Φ((T-μ)/σ)
    def win_prob(t: float, mu: float, sigma: float) -> float:
        if sigma <= 0:
            return 0.5
        return round(0.5 * (1 + math.erf((mu - t) / (sigma * 1.4142135623730951))), 3)

    return {
        "recommended": sigma_spill,
        "k_used": round(k, 2),
        "sigma_spill": sigma_spill,
        "p5_spill": p5_spill,
        "mean_spill": mean_spill,
        "sigma_win_prob": win_prob(sigma_spill, mean_c, std_c),
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


def _find_or_create_today_entry(log_data: dict) -> dict:
    """Find today's run entry in the log, or create a new one.

    Returns the entry dict (mutated in place within log_data).
    """
    today = _today_iso()
    runs = log_data.setdefault("runs", [])

    # Look for an existing entry for today
    for entry in runs:
        if entry.get("run_date") == today:
            return entry

    # Create new entry: predict TODAY → resolve TODAY
    entry = {
        "run_date": today,
        "target_date": today,
        "phase": "daily_bma",
        "run_started": _now_utc(),
        "last_updated": _now_utc(),
        "top_5_confidence": [],
        "predictions": {},
        "observations": {},
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
        optimal = _compute_optimal_spill(mean_c, std_c, conf, p5_c)
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
# --mode daily_bma
# =============================================================================

def _preds_to_dict(predictions: list[CityPrediction], locations: list[SavedLocation]) -> dict[str, dict]:
    """Convert CityPrediction list to log-ready dict with 3 strategies per city.

    New structure:
    {
      "Madrid, ES": {
        "bma_mean": 35.4, "bma_std": 0.6, "p5": 34.4, "p95": 36.4,
        "confidence": 0.82, "models": 8,
        "strategies": {
          "sigma": {"spill": 35, "k": 0.3, "win_prob": 0.74, "result": null, "actual_peak": null},
          "p5":    {"spill": 34, "k": null, "win_prob": 0.99, "result": null, "actual_peak": null},
          "mean":  {"spill": 35, "k": 0.0, "win_prob": 0.74, "result": null, "actual_peak": null}
        },
        "peak_detected_at": null,
        "recommendation": null,
        "_lat": ..., "_lon": ..., "_tz": ..., "_peak_hour_start": ..., "_peak_hour_end": ...,
        "_target_date": ..., "_uhi_adjustment": ...
      }
    }
    """
    loc_map = {l.name: l for l in locations}
    preds_dict: dict[str, dict] = {}
    for p in predictions:
        loc = loc_map.get(p.city)
        uhi = getattr(loc, "uhi_adjustment", 0.0) if loc else 0.0
        preds_dict[p.city] = {
            "bma_mean": p.bma_mean,
            "bma_std": p.bma_std,
            "p5": p.p5,
            "p95": p.p95,
            "confidence": p.confidence,
            "models": p.model_count,
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


async def daily_bma_mode() -> None:
    """Run BMA for ALL 51 cities with lead_days=0 (predict TODAY). Semaphore protects against rate limits."""
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
        target_date = date.today().isoformat()
        print(f"\n   🎯 LEAD_DAYS=0 (target: {target_date} — I DAG)\n")

        predictions = await run_bma_for_all(analyzer, locations, lead_days=0)
        top5 = select_top_n(predictions, 5)

        print(f"\n  {'─'*60}")
        print(f"  🏆 TOP 5 — I DAG ({target_date}):")
        for i, p in enumerate(top5):
            utc_peak = _local_peak_to_utc(p.tz, p.peak_hour_start, p.peak_hour_end)
            print(f"     {i+1}. {p.city:<30s} spill={p.suggested_spill}°C  "
                  f"μ={p.bma_mean:.1f}°C  conf={p.confidence:.3f}  "
                  f"({p.model_count}/8 modeller)  peak={utc_peak}")

        # Log — store predictions for today
        log_data = _load_log()
        entry = _find_or_create_today_entry(log_data)
        entry["phase"] = "daily_bma"
        entry["last_updated"] = _now_utc()
        entry["target_date"] = target_date
        top5_city_names = [p.city for p in top5]
        entry["top_5_confidence"] = top5_city_names
        entry["predictions"] = _preds_to_dict(predictions, locations)
        entry["observations"] = {city: [] for city in top5_city_names}

        _save_log(log_data)

        print(f"\n  ✅ daily_bma fullført — {len(predictions)} cities (lead_days=0)")
        print(f"  🎯 Top 5 valgt for timeovervåking: {', '.join(top5_city_names)}\n")

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

                    # Resolve ALL 3 strategies against the actual peak
                    # Polymarket resolves to the EXACT rounded temperature bucket
                    for strat_name in ("sigma", "p5", "mean"):
                        strat = strategies.get(strat_name, {})
                        spill = strat.get("spill", 0)
                        is_win = round(confirmed_temp) == spill
                        strat["result"] = "WIN" if is_win else "LOSS"
                        strat["actual_peak"] = round(confirmed_temp, 1)

                    # Generate recommendation based on sigma strategy
                    _update_recommendation(pdata)

                    sigma_result = strategies.get("sigma", {}).get("result", "?")
                    result_icon = "✅ WIN" if sigma_result == "WIN" else "❌ LOSS"
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

                    # Resolve ALL 3 strategies against the actual peak
                    # Polymarket resolves to the EXACT rounded temperature bucket
                    for strat_name in ("sigma", "p5", "mean"):
                        strat = strategies.get(strat_name, {})
                        spill = strat.get("spill", 0)
                        is_win = round(confirmed_temp) == spill
                        strat["result"] = "WIN" if is_win else "LOSS"
                        strat["actual_peak"] = round(confirmed_temp, 1)

                    # Generate recommendation
                    _update_recommendation(pdata)

                    sigma_result = strategies.get("sigma", {}).get("result", "?")
                    result_icon = "✅ WIN" if sigma_result == "WIN" else "❌ LOSS"
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
        entry["phase"] = "rapid_peak_monitor"
        entry["last_updated"] = _now_utc()
        log_data = _load_log()
        for e in log_data.get("runs", []):
            if e.get("run_date") == entry.get("run_date"):
                e["predictions"] = predictions
                e["observations"] = observations
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


# =============================================================================
# --mode daily_close
# =============================================================================

async def daily_close_mode() -> None:
    """Finalize daily results for ALL 51 cities, compare all 3 strategies, generate report.

    Runs at 23:00 UTC. Resolves TODAY's BMA predictions against TODAY's
    actual archive data. Simple: predict Aug 9 → resolve Aug 9.
    """
    print("╔══════════════════════════════════════════════════╗")
    print("║   MODELLKVALITET — DAGLIG AVSLUTNING (23:00)     ║")
    print("╚══════════════════════════════════════════════════╝")
    print(f"   Start: {_now_utc()}")

    log_data = _load_log()

    # Resolve TODAY's predictions against TODAY's archive data
    today = date.today().isoformat()

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
    target_date = today

    print(f"  Finaliserer {len(predictions)} byer for {target_date}\n")

    sigma_wins = sigma_losses = 0
    p5_wins = p5_losses = 0
    mean_wins = mean_losses = 0
    unresolved = 0

    for city, pdata in predictions.items():
        lat = pdata.get("_lat", 0)
        lon = pdata.get("_lon", 0)
        tz = pdata.get("_tz", "UTC")
        # Always use today's date for resolution (same-day model)
        city_target = target_date
        strategies = _get_strategies(pdata)

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

            # Resolve ALL 3 strategies using Polymarket rounding rule
            for strat_name in ("sigma", "p5", "mean"):
                strat = strategies.get(strat_name, {})
                spill = strat.get("spill", 0)
                is_win = round(archive_max) == spill
                strat["result"] = "WIN" if is_win else "LOSS"
                strat["actual_peak"] = round(archive_max, 1)

            # Generate recommendation
            _update_recommendation(pdata)

            # Tally
            sigma_res = strategies.get("sigma", {}).get("result", "")
            p5_res = strategies.get("p5", {}).get("result", "")
            mean_res = strategies.get("mean", {}).get("result", "")

            if sigma_res == "WIN": sigma_wins += 1
            else: sigma_losses += 1
            if p5_res == "WIN": p5_wins += 1
            else: p5_losses += 1
            if mean_res == "WIN": mean_wins += 1
            else: mean_losses += 1

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

    # Update cumulative
    cum = log_data.setdefault("cumulative", {})
    cum["total_days"] = len([r for r in log_data.get("runs", []) if r.get("phase") == "daily_close"])
    cum["sigma_wins"] = cum.get("sigma_wins", 0) + sigma_wins
    cum["sigma_losses"] = cum.get("sigma_losses", 0) + sigma_losses
    cum["p5_wins"] = cum.get("p5_wins", 0) + p5_wins
    cum["p5_losses"] = cum.get("p5_losses", 0) + p5_losses
    cum["mean_wins"] = cum.get("mean_wins", 0) + mean_wins
    cum["mean_losses"] = cum.get("mean_losses", 0) + mean_losses

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

    print(f"  Dager kjørt:         {len(runs)}")
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

    # Per-city stats across all strategies
    city_stats: dict[str, dict] = {}
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            if city not in city_stats:
                city_stats[city] = {"sigma": {"wins": 0, "losses": 0},
                                     "p5": {"wins": 0, "losses": 0},
                                     "mean": {"wins": 0, "losses": 0}}
            strategies = pdata.get("strategies", {})
            for sn in ("sigma", "p5", "mean"):
                s = strategies.get(sn, {})
                if s.get("result") == "WIN":
                    city_stats[city][sn]["wins"] += 1
                elif s.get("result") == "LOSS":
                    city_stats[city][sn]["losses"] += 1

    # Cities where one strategy clearly outperforms
    print(f"\n  🔍 BYER MED STRATEGI-FORSKJELLER:")
    strategy_diff_found = False
    for city, stats in sorted(city_stats.items()):
        sigma_t = stats["sigma"]["wins"] + stats["sigma"]["losses"]
        p5_t = stats["p5"]["wins"] + stats["p5"]["losses"]
        mean_t = stats["mean"]["wins"] + stats["mean"]["losses"]
        if sigma_t < 2:
            continue
        sigma_r = stats["sigma"]["wins"] / sigma_t * 100
        p5_r = stats["p5"]["wins"] / p5_t * 100 if p5_t > 0 else 0
        mean_r = stats["mean"]["wins"] / mean_t * 100 if mean_t > 0 else 0
        max_r = max(sigma_r, p5_r, mean_r)
        min_r = min(sigma_r, p5_r, mean_r)
        if max_r - min_r > 20:  # 20pp difference
            strategy_diff_found = True
            best_s = "Sigma" if sigma_r == max_r else ("P5" if p5_r == max_r else "Mean")
            print(f"     {city:<30s} sigma={sigma_r:.0f}% p5={p5_r:.0f}% mean={mean_r:.0f}% → {best_s} best")
    if not strategy_diff_found:
        print(f"     Ingen signifikante forskjeller funnet.")

    # Generate full report file
    _generate_markdown_report(log_data, None)

    print(f"\n  📄 Full rapport skrevet til _quality_report.md\n")


# =============================================================================
# Markdown Report Generator
# =============================================================================

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
    lines.append(f"**Days tracked:** {len([r for r in runs if r.get('phase') == 'daily_close'])}")
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

    # -- Confidence tier analysis (sigma only for now)
    high_conf = {"pos": 0, "wins": 0}
    mid_conf = {"pos": 0, "wins": 0}
    low_conf = {"pos": 0, "wins": 0}

    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            conf = pdata.get("confidence", 0)
            if result in ("WIN", "LOSS"):
                if conf >= 0.8:
                    high_conf["pos"] += 1
                    if result == "WIN":
                        high_conf["wins"] += 1
                elif conf >= 0.7:
                    mid_conf["pos"] += 1
                    if result == "WIN":
                        mid_conf["wins"] += 1
                else:
                    low_conf["pos"] += 1
                    if result == "WIN":
                        low_conf["wins"] += 1

    lines.append("## Sigma Strategy by Confidence Tier")
    lines.append("")
    lines.append(f"| Tier | Positions | Wins | Win Rate |")
    lines.append(f"|------|-----------|------|----------|")
    for label, stats in [("🟢 >80%", high_conf), ("🟠 70-80%", mid_conf), ("🔴 <70%", low_conf)]:
        wr = round(stats["wins"] / max(1, stats["pos"]) * 100, 1) if stats["pos"] > 0 else "N/A"
        wr_str = f"{wr}%" if isinstance(wr, (int, float)) else wr
        lines.append(f"| {label} | {stats['pos']} | {stats['wins']} | {wr_str} |")
    lines.append("")

    # -- Recent daily entries
    lines.append("## Recent Daily Results")
    lines.append("")
    recent = [r for r in runs if r.get("phase") == "daily_close"][-10:]
    if recent:
        lines.append(f"| Date | Sigma W/L | P5 W/L | Mean W/L |")
        lines.append(f"|------|-----------|--------|----------|")
        for r in recent:
            s = r.get("summary", {})
            sw2 = s.get("sigma_wins", 0)
            sl2 = s.get("sigma_losses", 0)
            pw2 = s.get("p5_wins", 0)
            pl2 = s.get("p5_losses", 0)
            mw2 = s.get("mean_wins", 0)
            ml2 = s.get("mean_losses", 0)
            lines.append(f"| {r['run_date']} | {sw2}/{sl2} | {pw2}/{pl2} | {mw2}/{ml2} |")
        lines.append("")

    # -- Flip recommendations summary
    flip_count = 0
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            rec = pdata.get("recommendation", "")
            if rec and "SELG" in str(rec):
                flip_count += 1
    if flip_count > 0:
        lines.append(f"**Total flip recommendations (SHORT):** {flip_count}")
        lines.append("")

    # Write
    REPORT_FILE.write_text("\n".join(lines), encoding="utf-8")


# =============================================================================
# CLI Entry Point
# =============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Model Quality Tracker — BMA ensemble performance monitoring",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=["daily_bma", "hourly_check", "daily_close", "full_report"],
        help="Run mode for GitHub Actions pipeline",
    )

    args = parser.parse_args()

    if args.mode == "daily_bma":
        asyncio.run(daily_bma_mode())
    elif args.mode == "hourly_check":
        asyncio.run(hourly_check_mode())
    elif args.mode == "daily_close":
        asyncio.run(daily_close_mode())
    elif args.mode == "full_report":
        full_report_mode()


if __name__ == "__main__":
    main()
