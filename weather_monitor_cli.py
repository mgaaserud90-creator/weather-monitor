#!/usr/bin/env python3
"""
Weather Monitor CLI — standalone interactive tool for monitoring weather
forecasts with BMA Multi-Model Ensemble confidence analysis.

Reuses the existing weather analysis pipeline from src/strategies/weather/:
  - BMA Multi-Model Ensemble (BMAEnsembleEngine)
  - Open-Meteo forecast client
  - Weather market parser (WeatherMarketParser)
  - Bucket probability calculation (_calc_bucket_probability)

Usage:
    cd polymarket-arb-bot
    python weather_monitor_cli.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]

# ---------------------------------------------------------------------------
# Ensure the polymarket-arb-bot package root is on sys.path so that
# "from src.xxx import yyy" works when running this script directly.
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ---------------------------------------------------------------------------
# Fix Windows cp1252 encoding for emoji / Unicode output
# ---------------------------------------------------------------------------
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Third-party imports
# ---------------------------------------------------------------------------
try:
    import colorama
    from colorama import Fore, Style, init as colorama_init
    colorama_init(autoreset=True)
except ImportError:
    # Fallback: define no-op color/emoji helpers
    class _NoColors:
        def __getattr__(self, name: str) -> str:
            return ""
    Fore = _NoColors()  # type: ignore[assignment]
    Style = _NoColors()  # type: ignore[assignment]
    def colorama_init():
        pass

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from src.clients.openmeteo_client import (
    DailyForecast,
    OpenMeteoClient,
    get_openmeteo_client,
)
from src.clients.gamma_client import GammaClient, get_gamma_client
from src.strategies.weather.ensemble import (
    BMAEnsembleEngine,
    BMAEnsemble,
    MODEL_DEFINITIONS,
)
from src.strategies.weather.strategy import WeatherCalibrationStrategy
from src.strategies.weather.market_parser import (
    WeatherMarketParser,
    WeatherMarket,
    TemperatureBucket,
    LOCATION_MAP,
    get_station_meta,
)
from src.config.constants import (
    WEATHER_MIN_LIQUIDITY,
    WEATHER_MARKET_SCAN_MAX,
    WEATHER_MARKET_PAGE_SIZE,
)

log = __import__("structlog").get_logger(__name__)

# =============================================================================
# Constants
# =============================================================================

LOCATIONS_FILE = Path(_SCRIPT_DIR) / "weather_monitor_locations.json"
DEFAULTS_FILE = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
CURRENT_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# CLI colors
C = Fore
RESET = Style.RESET_ALL
BOLD = Style.BRIGHT if hasattr(Style, "BRIGHT") else ""


def _green(s: str) -> str: return f"{C.GREEN}{BOLD}{s}{RESET}"
def _red(s: str) -> str: return f"{C.RED}{BOLD}{s}{RESET}"
def _yellow(s: str) -> str: return f"{C.YELLOW}{s}{RESET}"
def _cyan(s: str) -> str: return f"{C.CYAN}{s}{RESET}"
def _magenta(s: str) -> str: return f"{C.MAGENTA}{s}{RESET}"
def _white(s: str) -> str: return f"{C.WHITE}{BOLD}{s}{RESET}"


def _wind_deg_to_compass(deg: float | int | None) -> str:
    """Convert wind direction degrees to compass abbreviation (Norwegian)."""
    if deg is None:
        return "—"
    directions = [
        "N", "NNØ", "NØ", "ØNØ", "Ø", "ØSØ", "SØ", "SSØ",
        "S", "SSV", "SV", "VSV", "V", "VNV", "NV", "NNV",
    ]
    idx = int((float(deg) + 11.25) / 22.5) % 16
    return directions[idx]


# =============================================================================
# Edge Optimization Helpers (UHI, Kelly, Correlation, Spread)
# =============================================================================

def compute_kelly(win_prob: float, odds: float) -> float:
    """Compute Kelly Criterion optimal bet fraction.

    f* = (bp - q) / b  where b = odds - 1 (decimal odds minus 1),
    p = win probability, q = 1 - p.
    Returns percentage (0-100) or 0 if negative edge.
    """
    if odds <= 1.0:
        return 0.0
    b = odds - 1.0  # net odds
    q = 1.0 - win_prob
    kelly = (b * win_prob - q) / b
    kelly = max(0.0, kelly)
    return kelly * 100.0


def format_uhi_info(mean_c: float, uhi: float) -> tuple[str, float]:
    """Format UHI-adjusted temperature string and return adjusted mean."""
    if uhi <= 0:
        return (f"🌡️ BMA: {mean_c:.1f}°C", mean_c)
    adjusted = mean_c + uhi
    return (f"🌡️ BMA: {mean_c:.1f}°C (+{uhi:.1f}°C UHI = {adjusted:.1f}°C justert)", adjusted)


def format_station_info(station: str, elev_m: float) -> str:
    """Format station info string."""
    if not station:
        return ""
    s = f"📡 Stasjon: {station}"
    if elev_m:
        s += f" ({elev_m:.0f}m moh.)"
    return s


def format_spread_info(p5_c: float, p95_c: float, ens: Any, mean_c: float) -> tuple[str, float]:
    """Format ensemble spread information string and return spread in °C."""
    spread = p95_c - p5_c
    lines = []
    if spread <= 2.0:
        lines.append(f"📊 Modell-spredning: {spread:.1f}°C (smal = høy konfidens)")
    elif spread > 5.0:
        lines.append(f"📊 Modell-spredning: {spread:.1f}°C ⚠️ Høy spredning — mulig edge hvis du treffer")
    else:
        lines.append(f"📊 Modell-spredning: {spread:.1f}°C")

    # Find lowest and highest individual model
    if ens.individual_models:
        sorted_models = sorted(ens.individual_models.items(), key=lambda x: x[1])
        if sorted_models:
            lo_name, lo_f = sorted_models[0]
            hi_name, hi_f = sorted_models[-1]
            lo_c = (lo_f - 32.0) * 5.0 / 9.0
            hi_c = (hi_f - 32.0) * 5.0 / 9.0
            lines.append(f"   Laveste modell: {lo_c:.1f}°C ({lo_name}) | Høyeste: {hi_c:.1f}°C ({hi_name})")

    return ("\n".join(lines), spread)


def check_correlations(
    monitored_cities: list[str],
    correlations: list[dict[str, Any]],
) -> list[str]:
    """Check for correlated cities among monitored ones and return warnings."""
    warnings: list[str] = []
    city_set = set(monitored_cities)
    for corr in correlations:
        c1 = corr.get("city1", "")
        c2 = corr.get("city2", "")
        r = corr.get("r", 0.0)
        # Check if both cities are in the monitored set (fuzzy match)
        c1_match = any(c1 in c or c in c1 for c in city_set)
        c2_match = any(c2 in c or c in c2 for c in city_set)
        if c1_match and c2_match and r >= 0.55:
            warnings.append(f"⚠️ {c1} og {c2} er korrelerte (r={r:.2f}). Reduser samlet eksponering.")
    return warnings


def format_kelly_info(win_prob: float, odds: float = 1.39) -> str:
    """Format Kelly Criterion recommendation string."""
    kelly_pct = compute_kelly(win_prob, odds)
    if kelly_pct <= 0:
        return ""
    edge_pct = (win_prob * odds - 1.0) * 100
    lines = [
        f"💰 Kelly: Optimal innsats = {kelly_pct:.1f}% av bankroll",
        f"   (Basert på p={win_prob:.2f}, odds={odds:.2f}, edge={edge_pct:+.0f}%)",
    ]
    return "\n".join(lines)


# =============================================================================
# Data Models
# =============================================================================

@dataclass
class SavedLocation:
    """A saved monitoring location."""
    name: str
    lat: float
    lon: float
    tz: str = "UTC"  # IANA timezone string e.g. "Asia/Taipei"
    peak_hour_start: int = 14  # expected daily peak temp start hour (local time)
    peak_hour_end: int = 16    # expected daily peak temp end hour (local time)
    uhi_adjustment: float = 0.0  # Urban Heat Island adjustment in °C
    station_elevation_m: float = 0.0  # official station elevation in meters
    station: str = ""  # ICAO station code (e.g. "WSSS", "KJFK")
    added_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source: str = "manual"  # "manual" or "default"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "lat": self.lat, "lon": self.lon,
            "tz": self.tz,
            "peak_hour_start": self.peak_hour_start,
            "peak_hour_end": self.peak_hour_end,
            "uhi_adjustment": self.uhi_adjustment,
            "station_elevation_m": self.station_elevation_m,
            "station": self.station,
            "added_at": self.added_at, "source": self.source,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SavedLocation":
        return cls(
            name=d["name"], lat=d["lat"], lon=d["lon"],
            tz=d.get("tz", "UTC"),
            peak_hour_start=d.get("peak_hour_start", 14),
            peak_hour_end=d.get("peak_hour_end", 16),
            uhi_adjustment=d.get("uhi_adjustment", 0.0),
            station_elevation_m=d.get("station_elevation_m", 0.0),
            station=d.get("station", ""),
            added_at=d.get("added_at", ""), source=d.get("source", "manual"),
        )

    def local_date_now(self) -> date:
        """Return the current date in this location's timezone."""
        try:
            return datetime.now(ZoneInfo(self.tz)).date()
        except Exception:
            return date.today()

    def local_date_for_lead(self, lead_days: int) -> date:
        """Return the target date in this location's timezone for given lead_days."""
        try:
            return self.local_date_now() + timedelta(days=lead_days)
        except Exception:
            return date.today() + timedelta(days=lead_days)


@dataclass
class AnalysisResult:
    """Result of weather analysis for a single location."""
    location: SavedLocation
    ensemble: BMAEnsemble | None = None
    error: str | None = None
    elapsed_seconds: float = 0.0

    @property
    def is_ok(self) -> bool:
        return self.ensemble is not None and self.error is None


@dataclass
class MarketMatch:
    """A Polymarket market matching a monitored location."""
    location: SavedLocation
    market: WeatherMarket
    raw_data: dict[str, Any]
    liquidity: float | None = None
    volume_24h: float | None = None


@dataclass
class BucketComparison:
    """Comparison between bot probability and market price for a bucket."""
    bucket_label: str
    bucket_min_f: float
    bucket_max_f: float
    is_open_upper: bool = False
    model_prob: float = 0.0  # Bot's calibrated probability
    market_price: float | None = None  # Market's implied probability (mid or ask)
    edge: float = 0.0  # model_prob - market_price
    token_id: str = ""


@dataclass
class ConfidenceBucket:
    """Confidence analysis for a single temperature bucket."""
    label: str           # e.g. "22-24°C"
    lo_c: float
    hi_c: float
    probability: float   # BMA probability of temp in this bucket
    confidence: float    # How much models agree on this bucket (0-1)
    models_agree: int    # Number of models within this bucket's range
    total_models: int    # Total models available
    p5_p95_range: float  # P5-P95 spread for this bucket


@dataclass
class ConfidenceResult:
    """Pure confidence-based analysis for a location (no Polymarket dependency)."""
    location: SavedLocation
    ensemble: BMAEnsemble
    buckets: list[ConfidenceBucket]
    elapsed_seconds: float = 0.0
    overall_confidence: float = 0.0  # Max confidence across buckets
    best_bucket: ConfidenceBucket | None = None


@dataclass
class PeakState:
    """Result of peak detection for a city based on real-time observations."""
    state: str  # "future_date" | "past_date" | "rising" | "peak_window" | "possible_peak" | "confirmed" | "completed"
    state_label: str  # Norwegian label
    emoji: str
    color_hex: str  # e.g. "#2196F3"
    confidence: float  # 0.0 - 1.0 confidence that peak has occurred
    message: str  # Human-readable status
    confirmed_temp: float | None = None  # Confirmed peak temperature (°C)
    confirmed_time: datetime | None = None  # When peak was confirmed (local time)
    today_max_temp: float | None = None  # Highest observed temp so far today
    today_max_time: datetime | None = None  # When today's max was observed
    trend: str = "→"  # ↑ ↓ →
    live_confidence: float = 0.0  # 0-100 continuous live confidence score for trading edge
    suggested_temp: float | None = None  # Suggested bet temperature from BMA analysis
    minutes_since_last_max: int = 0  # Minutes since today's max was observed
    minutes_of_decline: int = 0  # Consecutive minutes of temperature decline
    alert_level: str = "none"  # "none" | "info" | "advarsel" | "kritisk" | "bekreftet"
    alert_message: str = ""  # Popup alert message


# =============================================================================
# Peak Detection Engine (shared between CLI and GUI)
# =============================================================================

def compute_live_confidence(
    obs_history: list[tuple[datetime, float]],
    today_max: tuple[float, datetime] | None,
    peak_hour_start: int,
    peak_hour_end: int,
    local_now: datetime,
    suggested_temp: float | None = None,
) -> tuple[float, int, int, str, str]:
    """Compute the continuous live confidence score (0-100) for early peak detection.

    This is the trading edge: detects that the daily high has peaked BEFORE
    Polymarket markets adjust, giving the user a window to flip positions.

    Returns
    -------
    (confidence_pct, minutes_since_last_max, minutes_of_decline, alert_level, alert_message)
    """
    confidence = 0.0
    current_hour = local_now.hour + local_now.minute / 60.0

    # --- Compute minutes since last max ---
    minutes_since_last_max = 0
    if today_max is not None:
        delta = local_now - today_max[1]
        minutes_since_last_max = int(delta.total_seconds() / 60)

    # --- Compute consecutive minutes of decline ---
    minutes_of_decline = 0
    if len(obs_history) >= 2:
        # Walk backwards through observations counting consecutive declines
        rev_temps = [t for _, t in reversed(obs_history)]
        for i in range(len(rev_temps) - 1):
            if rev_temps[i] <= rev_temps[i - 1]:  # current <= previous (declining or flat)
                # Estimate time between readings
                if len(obs_history) >= 2:
                    avg_interval = abs(
                        (obs_history[-1][0] - obs_history[0][0]).total_seconds()
                        / max(1, len(obs_history) - 1)
                    )
                    minutes_of_decline += max(1, int(avg_interval / 60))
                else:
                    minutes_of_decline += 5  # default 5-min interval
            else:
                break

    # --- Time factor: 0% at peak_hour_start, ramps to 60% at peak_hour_end ---
    peak_window_duration = max(1, peak_hour_end - peak_hour_start)  # hours
    hours_since_peak_start = current_hour - peak_hour_start
    if hours_since_peak_start > 0:
        time_factor = min(60.0, 60.0 * (hours_since_peak_start / peak_window_duration))
    else:
        time_factor = 0.0

    # --- Decline factor: 0-25% based on minutes of continuous decline ---
    decline_factor = min(25.0, minutes_of_decline * 1.0)

    # --- No-new-max factor: 0-15% based on minutes since last max ---
    staleness_factor = min(15.0, minutes_since_last_max * 0.25)

    # --- Temperature distance from suggested bet ---
    distance_bonus = 0.0
    current_temp = obs_history[-1][1] if obs_history else None
    if current_temp is not None and suggested_temp is not None:
        if current_temp < suggested_temp - 1.0:
            distance_bonus = 10.0
        elif current_temp < suggested_temp:
            distance_bonus = 5.0

    confidence = time_factor + decline_factor + staleness_factor + distance_bonus
    confidence = min(98.0, confidence)  # Cap at 98% (never 100% until official)

    # --- Determine alert level ---
    alert_level = "none"
    alert_message = ""

    if confidence > 90 or (current_hour > peak_hour_end and minutes_of_decline >= 15):
        alert_level = "bekreftet"
        if today_max:
            alert_message = (
                f"✅ Peak {today_max[0]:.1f}°C låst. Markedet vil justeres snart."
            )
    elif confidence > 80 and minutes_of_decline >= 10:
        today_max_temp = today_max[0] if today_max else (current_temp or 0)
        if current_temp is not None and today_max_temp - current_temp >= 0.3:
            alert_level = "kritisk"
            alert_message = (
                f"🔥 SNU POSISJON: Peak {today_max_temp:.1f}°C bekreftet! "
                f"{current_temp:.1f}°C synkende. Confidence: {confidence:.0f}%. "
                f"SNU FØR MARKEDET REAGERER!"
            )
    elif confidence > 60 and current_hour >= peak_hour_start:
        alert_level = "advarsel"
        alert_message = (
            f"⚠️ Peak sannsynlig nådd! Live confidence: {confidence:.0f}%. "
            f"{current_temp:.1f}°C ↓. Vurder å snu posisjon."
        )
    elif current_temp is not None and suggested_temp is not None and current_temp > suggested_temp:
        alert_level = "info"
        alert_message = (
            f"🌡️ {current_temp:.1f}°C — over anbefalt spill {suggested_temp:.0f}°C! Vurder å selge."
        )

    return (confidence, minutes_since_last_max, minutes_of_decline, alert_level, alert_message)


def detect_peak_state(
    obs_history: list[tuple[datetime, float]],
    today_max: tuple[float, datetime] | None,
    peak_hour_start: int,
    peak_hour_end: int,
    local_now: datetime,
    target_date: date,
    peak_confirmed: tuple[float, datetime] | None = None,
    suggested_temp: float | None = None,
) -> PeakState:
    """Evaluate the peak detection state for a city based on observation history.

    Parameters
    ----------
    obs_history : list of (timestamp, temp_c) tuples, ordered chronologically
    today_max : (max_temp_c, timestamp_of_max) or None
    peak_hour_start, peak_hour_end : expected daily peak window hours (local time)
    local_now : current datetime in the city's local timezone
    target_date : the analysis target date
    peak_confirmed : if already confirmed, the (temp, time) tuple
    suggested_temp : suggested bet temperature from BMA analysis

    Returns
    -------
    PeakState with state, label, emoji, color, confidence, message, and live_confidence
    """
    today_local = local_now.date()

    # Compute live confidence for trading edge
    live_conf, mins_since_max, mins_decline, alert_level, alert_msg = compute_live_confidence(
        obs_history=obs_history,
        today_max=today_max,
        peak_hour_start=peak_hour_start,
        peak_hour_end=peak_hour_end,
        local_now=local_now,
        suggested_temp=suggested_temp,
    )

    # --- Date-aware: future target ---
    if target_date > today_local:
        days_ahead = (target_date - today_local).days
        return PeakState(
            state="future_date",
            state_label="Venter",
            emoji="⏳",
            color_hex="#9E9E9E",
            confidence=0.0,
            message=f"⏳ Venter på {target_date.isoformat()} — peak deteksjon starter da ({days_ahead} dag(er))",
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend="→",
            live_confidence=0.0,
            suggested_temp=suggested_temp,
        )

    # --- Date-aware: past target ---
    if target_date < today_local:
        return PeakState(
            state="past_date",
            state_label="Fullført",
            emoji="✅",
            color_hex="#4CAF50",
            confidence=1.0,
            message=f"✅ Analyse fullført for {target_date.isoformat()}",
            confirmed_temp=peak_confirmed[0] if peak_confirmed else (today_max[0] if today_max else None),
            confirmed_time=peak_confirmed[1] if peak_confirmed else (today_max[1] if today_max else None),
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend="→",
            live_confidence=100.0,
            suggested_temp=suggested_temp,
        )

    # --- Already confirmed (for today) ---
    if peak_confirmed is not None:
        return PeakState(
            state="confirmed",
            state_label="PEAK BEKREFTET",
            emoji="🔴",
            color_hex="#D32F2F",
            confidence=1.0,
            message=f"🔴 PEAK: {peak_confirmed[0]:.1f}°C (nådd kl {peak_confirmed[1].strftime('%H:%M')}, bekreftet)",
            confirmed_temp=peak_confirmed[0],
            confirmed_time=peak_confirmed[1],
            today_max_temp=today_max[0] if today_max else peak_confirmed[0],
            today_max_time=today_max[1] if today_max else peak_confirmed[1],
            trend="↓",
            live_confidence=100.0,
            suggested_temp=suggested_temp,
        )

    current_hour = local_now.hour + local_now.minute / 60.0
    current_temp = obs_history[-1][1] if obs_history else None

    # Compute trend from last two observations
    trend = "→"
    if len(obs_history) >= 2 and current_temp is not None:
        prev_temp = obs_history[-2][1]
        if prev_temp is not None:
            diff = current_temp - prev_temp
            if diff > 0.3:
                trend = "↑"
            elif diff < -0.3:
                trend = "↓"

    # --- Rule 1: Time-based ---
    past_peak_end = current_hour > peak_hour_end
    before_peak_start = current_hour < peak_hour_start
    in_peak_window = peak_hour_start <= current_hour <= peak_hour_end

    # --- Rule 2: Temperature decline for 30+ min ---
    declining_30min = False
    if len(obs_history) >= 6:  # at least 6 readings for ~30 min at 5-min intervals
        recent_6 = obs_history[-6:]
        temps = [t for _, t in recent_6]
        # Check if each reading ≤ previous (non-increasing)
        declining_30min = all(temps[i] <= temps[i-1] for i in range(1, len(temps)))

    # --- Rule 3: No new max for 60+ min ---
    no_new_max_60min = False
    if today_max is not None and len(obs_history) >= 12:
        max_time = today_max[1]
        no_new_max_60min = (local_now - max_time).total_seconds() >= 3600

    # --- Past peak_end + 2 hours → completed ---
    if past_peak_end and current_hour > peak_hour_end + 2:
        return PeakState(
            state="completed",
            state_label="FULLFØRT",
            emoji="✅",
            color_hex="#4CAF50",
            confidence=1.0,
            message=f"✅ FULLFØRT — peak passert for {target_date.isoformat()}",
            confirmed_temp=peak_confirmed[0] if peak_confirmed else (today_max[0] if today_max else None),
            confirmed_time=peak_confirmed[1] if peak_confirmed else (today_max[1] if today_max else None),
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend=trend,
            live_confidence=live_conf,
            suggested_temp=suggested_temp,
            minutes_since_last_max=mins_since_max,
            minutes_of_decline=mins_decline,
            alert_level=alert_level,
            alert_message=alert_msg,
        )

    # --- Confirmed: past peak_end with declining temps or no new max ---
    if past_peak_end and (declining_30min or no_new_max_60min):
        conf = 0.80
        if declining_30min:
            conf += 0.20
        if no_new_max_60min:
            conf += 0.10
        conf = min(0.99, conf)
        return PeakState(
            state="confirmed",
            state_label="PEAK BEKREFTET",
            emoji="🔴",
            color_hex="#D32F2F",
            confidence=conf,
            message=f"🔴 PEAK: {today_max[0]:.1f}°C (nådd kl {today_max[1].strftime('%H:%M')}, bekreftet kl {local_now.strftime('%H:%M')})" if today_max else "🔴 PEAK BEKREFTET",
            confirmed_temp=today_max[0] if today_max else None,
            confirmed_time=today_max[1] if today_max else None,
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend=trend,
            live_confidence=live_conf,
            suggested_temp=suggested_temp,
            minutes_since_last_max=mins_since_max,
            minutes_of_decline=mins_decline,
            alert_level=alert_level,
            alert_message=alert_msg,
        )

    # --- Possible peak: past peak_hour_start, declining but <30 min ---
    if current_hour > peak_hour_start and declining_30min and not past_peak_end:
        return PeakState(
            state="possible_peak",
            state_label="MULIG PEAK",
            emoji="🟠",
            color_hex="#FF9800",
            confidence=0.50,
            message="🟠 MULIG PEAK — temp synkende, men <30 min bekreftelse",
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend=trend,
            live_confidence=live_conf,
            suggested_temp=suggested_temp,
            minutes_since_last_max=mins_since_max,
            minutes_of_decline=mins_decline,
            alert_level=alert_level,
            alert_message=alert_msg,
        )

    # --- Past peak_hour_start, no decline yet, but no new max for 60+ min ---
    if current_hour > peak_hour_start and no_new_max_60min and not declining_30min:
        return PeakState(
            state="possible_peak",
            state_label="MULIG PEAK",
            emoji="🟠",
            color_hex="#FF9800",
            confidence=0.40,
            message="🟠 MULIG PEAK — ingen ny max på 60+ min",
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend=trend,
            live_confidence=live_conf,
            suggested_temp=suggested_temp,
            minutes_since_last_max=mins_since_max,
            minutes_of_decline=mins_decline,
            alert_level=alert_level,
            alert_message=alert_msg,
        )

    # --- In peak window ---
    if in_peak_window:
        return PeakState(
            state="peak_window",
            state_label="PEAK-VINDU",
            emoji="🟡",
            color_hex="#FFC107",
            confidence=0.0,
            message=f"🟡 NÅ I PEAK-VINDU — temp kan fortsatt stige",
            today_max_temp=today_max[0] if today_max else None,
            today_max_time=today_max[1] if today_max else None,
            trend=trend,
            live_confidence=live_conf,
            suggested_temp=suggested_temp,
            minutes_since_last_max=mins_since_max,
            minutes_of_decline=mins_decline,
            alert_level=alert_level,
            alert_message=alert_msg,
        )

    # --- Rising (before peak window) ---
    return PeakState(
        state="rising",
        state_label="STIGER",
        emoji="🔵",
        color_hex="#2196F3",
        confidence=0.0,
        message="🔵 STIGER — temp øker, før peak-vindu",
        today_max_temp=today_max[0] if today_max else None,
        today_max_time=today_max[1] if today_max else None,
        trend=trend,
        live_confidence=live_conf,
        suggested_temp=suggested_temp,
        minutes_since_last_max=mins_since_max,
        minutes_of_decline=mins_decline,
        alert_level=alert_level,
        alert_message=alert_msg,
    )


# =============================================================================
# Forecast Cache
# =============================================================================

class ForecastCache:
    """Simple TTL cache for weather forecast results."""

    def __init__(self, ttl_seconds: int = 900):  # 15 min default
        self._cache: dict[str, tuple[float, Any]] = {}
        self._ttl = ttl_seconds

    def _make_key(self, lat: float, lon: float, lead_days: int) -> str:
        return f"{lat:.3f}_{lon:.3f}_{lead_days}"

    def get(self, lat: float, lon: float, lead_days: int) -> Any | None:
        key = self._make_key(lat, lon, lead_days)
        if key in self._cache:
            ts, val = self._cache[key]
            if time.time() - ts < self._ttl:
                return val
            del self._cache[key]
        return None

    def set(self, lat: float, lon: float, lead_days: int, value: Any) -> None:
        key = self._make_key(lat, lon, lead_days)
        self._cache[key] = (time.time(), value)

    def clear(self) -> None:
        self._cache.clear()

    @property
    def size(self) -> int:
        return len(self._cache)


# =============================================================================
# Location Manager
# =============================================================================

class LocationManager:
    """Manages saved locations with JSON persistence."""

    MAX_LOCATIONS = 100

    def __init__(self, path: Path = LOCATIONS_FILE, defaults_path: Path | None = None) -> None:
        self._path = path
        self._defaults_path = defaults_path or DEFAULTS_FILE
        self._locations: list[SavedLocation] = []
        self._load()

    # -------------------------------------------------------------------------
    # Persistence
    # -------------------------------------------------------------------------

    def _load(self) -> None:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                self._locations = [SavedLocation.from_dict(d) for d in data]
            except (json.JSONDecodeError, KeyError):
                self._locations = []

        # Auto-populate from defaults on first load
        if not self._locations:
            self._load_defaults()
        else:
            # Migrate: enrich timezone / peak data from defaults for old save files
            self._enrich_from_defaults()

    def _load_defaults(self) -> None:
        """Populate locations from the default city database."""
        if self._defaults_path.exists():
            try:
                data = json.loads(self._defaults_path.read_text(encoding="utf-8"))
                defaults = data.get("default_locations", [])
                self._locations = [
                    SavedLocation(
                        name=d["name"], lat=d["lat"], lon=d["lon"],
                        tz=d.get("tz", "UTC"),
                        peak_hour_start=d.get("peak_hour_start", 14),
                        peak_hour_end=d.get("peak_hour_end", 16),
                        uhi_adjustment=d.get("uhi_adjustment", 0.0),
                        station_elevation_m=d.get("station_elevation_m", 0.0),
                        station=d.get("station", ""),
                        source="default",
                    )
                    for d in defaults
                ]
                self._save()
            except (json.JSONDecodeError, KeyError):
                pass

    def _enrich_from_defaults(self) -> None:
        """Migrate: fill in missing timezone/peak/UHI/station data from the defaults file."""
        if not self._defaults_path.exists():
            return
        try:
            data = json.loads(self._defaults_path.read_text(encoding="utf-8"))
            defaults = data.get("default_locations", [])
            tz_map: dict[str, str] = {}
            peak_map: dict[str, tuple[int, int]] = {}
            uhi_map: dict[str, float] = {}
            elev_map: dict[str, float] = {}
            station_map: dict[str, str] = {}
            for d in defaults:
                name = d["name"]
                if d.get("tz"):
                    tz_map[name] = d["tz"]
                ph_start = d.get("peak_hour_start", 14)
                ph_end = d.get("peak_hour_end", 16)
                peak_map[name] = (ph_start, ph_end)
                if d.get("uhi_adjustment"):
                    uhi_map[name] = d["uhi_adjustment"]
                if d.get("station_elevation_m"):
                    elev_map[name] = d["station_elevation_m"]
                if d.get("station"):
                    station_map[name] = d["station"]

            changed = False
            for loc in self._locations:
                # Enrich timezone
                if loc.tz == "UTC" and loc.name in tz_map and tz_map[loc.name] != "UTC":
                    loc.tz = tz_map[loc.name]
                    changed = True
                # Enrich peak hours (if still at default 14-16 and defaults have diff)
                if loc.name in peak_map:
                    dflt_start, dflt_end = peak_map[loc.name]
                    if (loc.peak_hour_start, loc.peak_hour_end) != (dflt_start, dflt_end):
                        loc.peak_hour_start = dflt_start
                        loc.peak_hour_end = dflt_end
                        changed = True
                # Enrich UHI adjustment
                if loc.uhi_adjustment == 0.0 and loc.name in uhi_map:
                    loc.uhi_adjustment = uhi_map[loc.name]
                    changed = True
                # Enrich station elevation
                if loc.station_elevation_m == 0.0 and loc.name in elev_map:
                    loc.station_elevation_m = elev_map[loc.name]
                    changed = True
                # Enrich station code
                if not loc.station and loc.name in station_map:
                    loc.station = station_map[loc.name]
                    changed = True

            if changed:
                self._save()
        except Exception:
            pass

    def load_correlations(self) -> list[dict[str, Any]]:
        """Load city correlation pairs from the defaults file."""
        if not self._defaults_path.exists():
            return []
        try:
            data = json.loads(self._defaults_path.read_text(encoding="utf-8"))
            return data.get("city_correlations", [])
        except (json.JSONDecodeError, KeyError):
            return []

    def reset_to_defaults(self) -> int:
        """Replace all locations with the default database. Returns count."""
        self._locations.clear()
        self._load_defaults()
        return len(self._locations)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps([loc.to_dict() for loc in self._locations], indent=2),
            encoding="utf-8",
        )

    # -------------------------------------------------------------------------
    # Operations
    # -------------------------------------------------------------------------

    @property
    def locations(self) -> list[SavedLocation]:
        return list(self._locations)

    @property
    def count(self) -> int:
        return len(self._locations)

    def add(self, name: str, lat: float, lon: float) -> SavedLocation:
        if self.count >= self.MAX_LOCATIONS:
            raise ValueError(f"Maximum {self.MAX_LOCATIONS} locations reached. Remove one first.")
        loc = SavedLocation(name=name, lat=lat, lon=lon)
        self._locations.append(loc)
        self._save()
        return loc

    def remove(self, index: int) -> SavedLocation:
        if not 0 <= index < self.count:
            raise IndexError(f"Invalid index {index}. Valid: 0-{self.count - 1}")
        loc = self._locations.pop(index)
        self._save()
        return loc

    def clear(self) -> int:
        count = len(self._locations)
        self._locations.clear()
        self._save()
        return count

    def get(self, index: int) -> SavedLocation:
        if not 0 <= index < self.count:
            raise IndexError(f"Invalid index {index}")
        return self._locations[index]


# =============================================================================
# Geocoding Helper
# =============================================================================

async def geocode_city(city_name: str) -> tuple[str, float, float] | None:
    """Geocode a city name to lat/lon via Open-Meteo Geocoding API.

    Returns (display_name, lat, lon) or None if not found.
    """
    if httpx is None:
        raise RuntimeError("httpx is required for geocoding. Install with: pip install httpx")

    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.get(
                GEOCODING_URL,
                params={"name": city_name, "count": 5, "language": "en", "format": "json"},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise RuntimeError(f"Geocoding API request failed: {exc}")

        results = data.get("results", [])
        if not results:
            return None

        # Prefer results with admin1 (state/province)
        scored = []
        for r in results:
            score = 0
            if r.get("admin1"):
                score += 2
            if r.get("country_code"):
                score += 1
            if r.get("population", 0) > 0:
                score += 1
            scored.append((score, r))
        scored.sort(key=lambda x: -x[0])
        best = scored[0][1]

        name_parts = [best.get("name", "")]
        if best.get("admin1"):
            name_parts.append(best["admin1"])
        name_parts.append(best.get("country_code", ""))
        display_name = ", ".join(p for p in name_parts if p)

        return (display_name, float(best["latitude"]), float(best["longitude"]))


# =============================================================================
# Weather Analyzer (enhanced with caching + confidence analysis)
# =============================================================================

class WeatherAnalyzer:
    """Wraps the BMA ensemble engine + Open-Meteo client for weather analysis.

    Enhanced with:
      - Forecast caching (15 min TTL)
      - Confidence-based analysis per temperature bucket
      - Timing information
    """

    # Standard temperature buckets in °C for analysis
    DEFAULT_BUCKETS_C: list[tuple[float, float]] = [
        (-99.0, 0.0), (0.0, 5.0), (5.0, 10.0), (10.0, 12.0), (12.0, 14.0), (14.0, 16.0),
        (16.0, 18.0), (18.0, 20.0), (20.0, 22.0), (22.0, 24.0), (24.0, 26.0), (26.0, 28.0),
        (28.0, 30.0), (30.0, 32.0), (32.0, 34.0), (34.0, 36.0), (36.0, 38.0), (38.0, 40.0), (40.0, 200.0),
    ]

    def __init__(self, cache_ttl: int = 900) -> None:
        self._openmeteo: OpenMeteoClient | None = None
        self._bma: BMAEnsembleEngine | None = None
        self._initialized = False
        self._cache = ForecastCache(ttl_seconds=cache_ttl)
        self._quick_scan_mode: bool = False  # If True, use only 3-4 models

    @property
    def quick_scan_mode(self) -> bool:
        return self._quick_scan_mode

    @quick_scan_mode.setter
    def quick_scan_mode(self, val: bool) -> None:
        self._quick_scan_mode = val

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._openmeteo = get_openmeteo_client()
        await self._openmeteo.initialize()
        self._bma = BMAEnsembleEngine(openmeteo=self._openmeteo)
        self._initialized = True

    async def close(self) -> None:
        if self._openmeteo:
            await self._openmeteo.close()
            self._openmeteo = None
        self._bma = None
        self._initialized = False

    # -----------------------------------------------------------------------
    # Real-Time Current Temperature (Open-Meteo Current Weather API)
    # -----------------------------------------------------------------------

    async def get_current_temp(
        self, lat: float, lon: float, tz: str = "UTC"
    ) -> dict[str, Any] | None:
        """Fetch current observed temperature + conditions from Open-Meteo's free API.

        Returns dict with keys: temp_c, time_utc, time_local, humidity, wind_speed,
        wind_direction, wind_dir_compass, cloud_cover, or None on failure.
        """
        if httpx is None:
            return None
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    CURRENT_WEATHER_URL,
                    params={
                        "latitude": lat,
                        "longitude": lon,
                        "current": "temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,cloud_cover",
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
                # Parse local time
                local_dt = None
                try:
                    local_dt = datetime.fromisoformat(time_str)
                except (ValueError, TypeError):
                    local_dt = datetime.now(ZoneInfo(tz)) if tz != "UTC" else datetime.now(timezone.utc)

                # --- Wind direction to compass ---
                wind_dir = current.get("wind_direction_10m")
                wind_dir_compass = _wind_deg_to_compass(wind_dir) if wind_dir is not None else None

                return {
                    "temp_c": float(temp_c),
                    "time_utc": time_str,
                    "time_local": local_dt,
                    "humidity": current.get("relative_humidity_2m"),  # % (None if unavailable)
                    "wind_speed": current.get("wind_speed_10m"),      # km/h
                    "wind_direction": wind_dir,                        # degrees
                    "wind_dir_compass": wind_dir_compass,              # "NØ", "SV", etc.
                    "cloud_cover": current.get("cloud_cover"),         # %
                }
        except Exception as exc:
            log.warning("get_current_temp failed", lat=lat, lon=lon, error=str(exc))
            return None

    async def get_current_conditions(
        self, lat: float, lon: float, tz: str = "UTC"
    ) -> dict[str, Any] | None:
        """Fetch current conditions only (no temperature). Convenience wrapper."""
        return await self.get_current_temp(lat, lon, tz)

    async def analyze(self, loc: SavedLocation, lead_days: int = 0) -> AnalysisResult:
        """Run full BMA ensemble analysis for a location.

        Uses the location's local timezone for date calculation and passes
        timezone to the Open-Meteo API for local-time forecasts.
        """
        t0 = time.perf_counter()
        if not self._initialized or self._bma is None:
            return AnalysisResult(location=loc, error="Analyzer not initialized")

        # Check cache
        cached = self._cache.get(loc.lat, loc.lon, lead_days)
        if cached is not None:
            elapsed = time.perf_counter() - t0
            return AnalysisResult(location=loc, ensemble=cached, elapsed_seconds=elapsed)

        # Use location's local timezone for date calculation
        target_date = loc.local_date_for_lead(lead_days)

        try:
            ensemble = await self._bma.fetch_all_models(
                lat=loc.lat,
                lon=loc.lon,
                location=loc.name,
                lead_days=lead_days,
                target_date=target_date.isoformat(),
            )
            # Cache the result
            self._cache.set(loc.lat, loc.lon, lead_days, ensemble)
            elapsed = time.perf_counter() - t0
            return AnalysisResult(location=loc, ensemble=ensemble, elapsed_seconds=elapsed)
        except Exception as exc:
            elapsed = time.perf_counter() - t0
            return AnalysisResult(location=loc, error=str(exc), elapsed_seconds=elapsed)

    @staticmethod
    def compute_bucket_prob(
        bma_mean_f: float,
        bma_std_f: float,
        bucket_min_f: float,
        bucket_max_f: float,
        is_open_upper: bool = False,
    ) -> float:
        """Compute P(temp ∈ bucket) using the strategy's CDF method."""
        return WeatherCalibrationStrategy._calc_bucket_probability(
            bma_mean_f=bma_mean_f,
            bma_std_f=bma_std_f,
            bucket_min_f=bucket_min_f,
            bucket_max_f=bucket_max_f,
            is_open_upper=is_open_upper,
        )

    @staticmethod
    def f_to_c(val_f: float) -> float:
        """Fahrenheit to Celsius."""
        return (val_f - 32.0) * 5.0 / 9.0

    @staticmethod
    def c_to_f(val_c: float) -> float:
        """Celsius to Fahrenheit."""
        return val_c * 9.0 / 5.0 + 32.0

    def analyze_confidence(
        self, loc: SavedLocation, lead_days: int = 1,
        buckets_c: list[tuple[float, float]] | None = None,
    ) -> ConfidenceResult:
        """Run BMA ensemble and compute per-bucket confidence (synchronous wrapper)."""
        return asyncio.get_event_loop().run_until_complete(
            self.analyze_confidence_async(loc, lead_days, buckets_c)
        )

    async def analyze_confidence_async(
        self, loc: SavedLocation, lead_days: int = 1,
        buckets_c: list[tuple[float, float]] | None = None,
    ) -> ConfidenceResult:
        """Run BMA ensemble and compute per-bucket confidence.

        Returns a ConfidenceResult with per-bucket probability, confidence,
        and model agreement statistics.
        """
        t0 = time.perf_counter()

        # Get ensemble analysis
        analysis = await self.analyze(loc, lead_days=lead_days)
        if analysis.error or analysis.ensemble is None:
            ens = BMAEnsemble(
                location=loc.name,
                target_date=loc.local_date_for_lead(lead_days).isoformat(),
                lead_days=lead_days,
                mean_temp_f=70.0, std_temp_f=10.0,
                median_temp_f=70.0,
                p05_temp_f=53.5, p10_temp_f=57.2, p90_temp_f=82.8, p95_temp_f=86.5,
                model_count=0, confidence=0.1,
            )
        else:
            ens = analysis.ensemble

        if buckets_c is None:
            buckets_c = self.DEFAULT_BUCKETS_C

        mean_c = self.f_to_c(ens.mean_temp_f)
        p5_c = self.f_to_c(ens.p05_temp_f)
        p95_c = self.f_to_c(ens.p95_temp_f)
        range_c = p95_c - p5_c

        # Compute per-bucket confidence
        confidence_buckets: list[ConfidenceBucket] = []
        overall_confidence = 0.0
        best_bucket: ConfidenceBucket | None = None

        for lo_c, hi_c in buckets_c:
            # Skip buckets far from mean (3+ std away)
            if hi_c < mean_c - 3 * (ens.std_temp_f * 5.0 / 9.0):
                continue
            if lo_c > mean_c + 3 * (ens.std_temp_f * 5.0 / 9.0):
                continue

            # Convert bucket bounds to °F for probability calculation
            lo_f = self.c_to_f(lo_c) if lo_c > -99 else float('-inf')
            hi_f = self.c_to_f(hi_c) if hi_c < 199 else float('inf')

            prob = self.compute_bucket_prob(
                bma_mean_f=ens.mean_temp_f,
                bma_std_f=ens.std_temp_f,
                bucket_min_f=lo_f if lo_f != float('-inf') else -100.0,
                bucket_max_f=hi_f if hi_f != float('inf') else 200.0,
                is_open_upper=(hi_c >= 199),
            )

            # Model agreement: how many models predict within this bucket
            models_in_bucket = 0
            if ens.individual_models:
                for model_temp_f in ens.individual_models.values():
                    model_temp_c = self.f_to_c(model_temp_f)
                    if lo_c <= model_temp_c < hi_c:
                        models_in_bucket += 1

            total_models = max(1, ens.model_count)

            # Bucket confidence combines:
            #   (1) ensemble confidence × (2) model agreement ratio × (3) narrowness bonus
            model_agree_ratio = models_in_bucket / total_models
            narrowness_bonus = 1.0 / (1.0 + max(0, (hi_c - lo_c) / 4.0))
            bucket_confidence = ens.confidence * (0.4 + 0.6 * model_agree_ratio) * min(1.0, 1.0 + narrowness_bonus * 0.3)
            bucket_confidence = min(0.99, max(0.1, bucket_confidence))

            # P5-P95 range for this bucket
            bucket_range = range_c

            cb = ConfidenceBucket(
                label=f"{lo_c}-{hi_c}°C" if lo_c > -99 else f"<{hi_c}°C",
                lo_c=lo_c, hi_c=hi_c,
                probability=prob,
                confidence=bucket_confidence,
                models_agree=models_in_bucket,
                total_models=total_models,
                p5_p95_range=bucket_range,
            )
            confidence_buckets.append(cb)

            if bucket_confidence > overall_confidence:
                overall_confidence = bucket_confidence
                best_bucket = cb

        elapsed = time.perf_counter() - t0
        return ConfidenceResult(
            location=loc,
            ensemble=ens,
            buckets=confidence_buckets,
            elapsed_seconds=elapsed,
            overall_confidence=overall_confidence,
            best_bucket=best_bucket,
        )

    async def bulk_confidence_analysis(
        self, locations: list[SavedLocation], lead_days: int = 1,
        progress_callback: Any = None,
    ) -> list[dict[str, Any]]:
        """Run confidence analysis on all locations in parallel, rank by confidence.

        Returns a list sorted by overall_confidence descending.
        """
        t0 = time.perf_counter()

        async def _analyze_one(i: int, loc: SavedLocation) -> dict[str, Any] | None:
            try:
                cr = await self.analyze_confidence_async(loc, lead_days=lead_days)
                ens = cr.ensemble
                mean_c = self.f_to_c(ens.mean_temp_f)
                p5_c = self.f_to_c(ens.p05_temp_f)
                p95_c = self.f_to_c(ens.p95_temp_f)
                range_c = p95_c - p5_c

                entry: dict[str, Any] = {
                    "city": loc.name,
                    "lat": loc.lat,
                    "lon": loc.lon,
                    "tz": loc.tz,
                    "lead_days": lead_days,
                    "target_date": loc.local_date_for_lead(lead_days).isoformat(),
                    "mean_c": round(mean_c, 1),
                    "p5_c": round(p5_c, 1),
                    "p95_c": round(p95_c, 1),
                    "range_c": round(range_c, 2),
                    "overall_confidence": round(cr.overall_confidence, 3),
                    "model_count": ens.model_count,
                    "elapsed": round(cr.elapsed_seconds, 2),
                    "best_bucket": None,
                    "buckets": [],
                }

                if cr.best_bucket:
                    entry["best_bucket"] = {
                        "label": cr.best_bucket.label,
                        "probability": round(cr.best_bucket.probability, 3),
                        "confidence": round(cr.best_bucket.confidence, 3),
                        "models_agree": cr.best_bucket.models_agree,
                        "total_models": cr.best_bucket.total_models,
                    }

                for b in cr.buckets:
                    if b.probability > 0.01:
                        entry["buckets"].append({
                            "label": b.label,
                            "probability": round(b.probability, 3),
                            "confidence": round(b.confidence, 3),
                            "models_agree": b.models_agree,
                            "total_models": b.total_models,
                        })

                if progress_callback:
                    progress_callback(i + 1, len(locations), loc.name)

                return entry

            except Exception as exc:
                if progress_callback:
                    progress_callback(i + 1, len(locations), loc.name)
                return {
                    "city": loc.name,
                    "error": str(exc),
                    "overall_confidence": 0.0,
                }

        # Run all analyses concurrently
        tasks = [_analyze_one(i, loc) for i, loc in enumerate(locations)]
        raw_results = await asyncio.gather(*tasks)
        results = [r for r in raw_results if r is not None]

        # Sort by overall_confidence descending
        results.sort(key=lambda x: x.get("overall_confidence", 0), reverse=True)
        return results


# =============================================================================
# Market Discovery
# =============================================================================

class MarketDiscovery:
    """Scans Polymarket Gamma API for weather markets matching monitored locations."""

    def __init__(self) -> None:
        self._gamma: GammaClient | None = None
        self._parser = WeatherMarketParser()
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        self._gamma = get_gamma_client()
        await self._gamma.initialize()
        self._initialized = True

    async def close(self) -> None:
        if self._gamma:
            await self._gamma.close()
            self._gamma = None
        self._initialized = False

    @property
    def gamma(self) -> GammaClient:
        if self._gamma is None:
            raise RuntimeError("MarketDiscovery not initialized")
        return self._gamma

    async def scan_for_locations(
        self,
        locations: list[SavedLocation],
        max_scan: int = WEATHER_MARKET_SCAN_MAX,
        page_size: int = WEATHER_MARKET_PAGE_SIZE,
        min_liquidity: float = WEATHER_MIN_LIQUIDITY,
    ) -> list[MarketMatch]:
        """Scan Polymarket for markets matching any of the given locations."""
        matches: list[MarketMatch] = []
        seen_ids: set[str] = set()

        # Build search terms from location names
        loc_terms: dict[str, SavedLocation] = {}
        for loc in locations:
            # Normalize: extract city name and add variants
            parts = loc.name.lower().split(",")[0].strip().split()
            loc_terms[loc.name.lower()] = loc
            for part in parts:
                if len(part) > 2:
                    loc_terms[part.lower()] = loc

        # Also add known station names from LOCATION_MAP
        station_to_loc: dict[str, SavedLocation] = {}
        for loc in locations:
            for station_key in LOCATION_MAP:
                if loc.name.lower() in station_key or station_key in loc.name.lower():
                    station_to_loc[station_key] = loc

        # Scan /markets endpoint (paginated, liquidity-sorted)
        offset = 0
        total_scanned = 0
        while total_scanned < max_scan:
            try:
                batch = await self.gamma.get_markets(
                    limit=page_size,
                    offset=offset,
                    active=True,
                    order="liquidityNum",
                    ascending=False,
                )
            except Exception:
                break

            if not batch:
                break

            for m in batch:
                question = (m.get("question", "") or "")
                description = (m.get("description", "") or "")
                combined = (question + " " + description).lower()

                # Quick weather filter
                if not self._is_weather_market(combined):
                    continue

                market_id = str(m.get("id", ""))
                if market_id in seen_ids:
                    continue
                seen_ids.add(market_id)

                # Check if this market mentions any of our locations
                matched_loc = self._find_matching_location(combined, loc_terms, station_to_loc)
                if matched_loc is None:
                    continue

                # Liquidity filter
                liquidity = self._safe_float(m.get("liquidityNum"))
                if liquidity is not None and liquidity < min_liquidity:
                    continue

                # Parse into WeatherMarket
                outcomes = self._parse_json_array(m.get("outcomes"))
                parsed = self._parser.parse_question(question, outcomes=outcomes)
                if parsed is None:
                    continue

                parsed.market_id = market_id
                clob_ids = self._parse_json_array(m.get("clobTokenIds"))
                parsed.token_ids = [str(t) for t in clob_ids]
                for i, bucket in enumerate(parsed.buckets):
                    if i < len(clob_ids):
                        bucket.token_id = str(clob_ids[i])

                matches.append(MarketMatch(
                    location=matched_loc,
                    market=parsed,
                    raw_data=m,
                    liquidity=liquidity,
                    volume_24h=self._safe_float(m.get("volume24hr")),
                ))

            total_scanned += len(batch)
            if len(batch) < page_size:
                break
            offset += page_size

        return matches

    @staticmethod
    def _is_weather_market(text: str) -> bool:
        """Quick check if a market is weather-related."""
        strong = [
            "temperature", "°c", "°f", "degrees", "fahrenheit", "celsius",
            "hottest", "coldest", "warmest", "heatwave", "heat wave",
            "heat index", "wind chill", "record high", "record low",
            "record temperature", "precipitation", "snowfall", "rainfall",
            "humidity", "tornado", "cyclone", "typhoon", "blizzard",
            "drought", "frost", "monsoon", "hurricane",
        ]
        if any(kw in text for kw in strong):
            return True
        ambiguous = ["weather", "storm", "rain", "snow", "wind", "flood", "hail"]
        if any(re.search(rf"\b{re.escape(kw)}\b", text) for kw in ambiguous):
            # Exclude sports contexts
            sports = ["nhl", "nba", "nfl", "mlb", "playoff", "beat the", "vs ", "vs."]
            if not any(ind in text for ind in sports):
                return True
        return False

    @staticmethod
    def _find_matching_location(
        text: str,
        loc_terms: dict[str, SavedLocation],
        station_to_loc: dict[str, SavedLocation],
    ) -> SavedLocation | None:
        """Check if any of our locations appear in the market text."""
        # Check station keys first (more specific)
        for station_key, loc in station_to_loc.items():
            if station_key in text:
                return loc
        # Check location terms
        for term, loc in loc_terms.items():
            if term in text:
                return loc
        return None

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _parse_json_array(value: Any) -> list[Any]:
        if value is None:
            return []
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value.strip())
                return parsed if isinstance(parsed, list) else []
            except (json.JSONDecodeError, ValueError):
                return []
        return []


# =============================================================================
# Main CLI Application
# =============================================================================

class WeatherMonitorCLI:
    """Interactive REPL for weather market monitoring."""

    BANNER = f"""
{_cyan("╔══════════════════════════════════════════════════════╗")}
{_cyan("║")}     {_white("WEATHER MONITOR CLI")}                              {_cyan("║")}
{_cyan("║")}     {_green("BMA Multi-Model Ensemble + Confidence Analysis")} {_cyan("║")}
{_cyan("╚══════════════════════════════════════════════════════╝")}
"""

    def __init__(self) -> None:
        self._loc_mgr = LocationManager()
        self._analyzer = WeatherAnalyzer()
        self._discovery = MarketDiscovery()
        self._running = False
        self._last_matches: list[MarketMatch] = []
        self._last_analyses: dict[int, AnalysisResult] = {}
        self._last_confidence: dict[int, ConfidenceResult] = {}
        self._default_lead_days: int = 1  # default: tomorrow

    # =========================================================================
    # Main Entry Point
    # =========================================================================

    async def run(self) -> None:
        print(self.BANNER)
        print(f"  Locations file: {_yellow(str(LOCATIONS_FILE))}")
        print(f"  Saved locations: {self._loc_mgr.count}/{LocationManager.MAX_LOCATIONS}")
        print()

        # Initialize sub-systems
        print("Initializing...")
        try:
            await self._analyzer.initialize()
            print(f"  {_green('✓')} Weather analyzer (BMA Ensemble Engine)")
        except Exception as exc:
            print(f"  {_red('✗')} Weather analyzer: {exc}")

        try:
            await self._discovery.initialize()
            print(f"  {_green('✓')} Market discovery (Gamma API)")
        except Exception as exc:
            print(f"  {_red('✗')} Market discovery: {exc}")

        print()
        print(f"Type {_cyan('help')} for commands, {_cyan('quit')} to exit.")
        print()

        self._running = True
        while self._running:
            try:
                cmd_line = await self._read_input()
                if cmd_line is None:
                    break
                await self._dispatch(cmd_line.strip())
            except KeyboardInterrupt:
                print()
                self._running = False
            except EOFError:
                self._running = False

        await self._cleanup()

    async def _read_input(self) -> str | None:
        """Read a line from stdin (async-compatible via asyncio.to_thread)."""
        try:
            prompt = f"{_green('weather>')} "
            return await asyncio.to_thread(input, prompt)
        except (KeyboardInterrupt, EOFError):
            return None

    async def _cleanup(self) -> None:
        print(f"\n{_cyan('Shutting down...')}")
        try:
            await self._analyzer.close()
        except Exception:
            pass
        try:
            await self._discovery.close()
        except Exception:
            pass
        print(f"{_green('Goodbye!')}")

    # =========================================================================
    # Command Dispatcher
    # =========================================================================

    async def _dispatch(self, line: str) -> None:
        if not line:
            return

        parts = line.split()
        cmd = parts[0].lower()
        args = parts[1:]

        handlers: dict[str, Any] = {
            "add": self._cmd_add,
            "remove": self._cmd_remove,
            "list": self._cmd_list,
            "clear": self._cmd_clear,
            "scan": self._cmd_scan,
            "analyze": self._cmd_analyze,
            "confidence": self._cmd_confidence,
            "bulk": self._cmd_bulk,
            "monitor": self._cmd_monitor,
            "date": self._cmd_date,
            "reset_defaults": self._cmd_reset_defaults,
            "help": self._cmd_help,
            "quit": self._cmd_quit,
            "exit": self._cmd_quit,
            "q": self._cmd_quit,
        }

        handler = handlers.get(cmd)
        if handler is None:
            print(f"{_red('Unknown command:')} {cmd}")
            print(f"Type {_cyan('help')} to see available commands.")
            return

        try:
            await handler(args)
        except Exception as exc:
            print(f"{_red('Error:')} {exc}")

    # =========================================================================
    # add <city> | add coords <lat> <lon> <name>
    # =========================================================================

    async def _cmd_add(self, args: list[str]) -> None:
        if not args:
            print(f"{_yellow('Usage:')} add <city name>  OR  add coords <lat> <lon> <name>")
            print(f"  Examples: add Oslo, NO")
            print(f"            add coords 40.7128 -74.0060 New York, US")
            return

        if args[0].lower() == "coords":
            await self._cmd_add_coords(args[1:])
        else:
            await self._cmd_add_city(" ".join(args))

    async def _cmd_add_city(self, city_name: str) -> None:
        if self._loc_mgr.count >= LocationManager.MAX_LOCATIONS:
            print(f"{_red('Maximum 10 locations reached.')} Remove one first with 'remove <index>'.")
            return

        print(f"{_cyan('Geocoding')} '{city_name}'...")
        try:
            result = await geocode_city(city_name)
        except Exception as exc:
            print(f"{_red('Geocoding failed:')} {exc}")
            return

        if result is None:
            print(f"{_red('City not found:')} {city_name}")
            print(f"  Try adding country code: 'Oslo, NO', 'New York, US'")
            return

        display_name, lat, lon = result
        try:
            loc = self._loc_mgr.add(display_name, lat, lon)
            print(f"{_green('✓ Added')} [{self._loc_mgr.count - 1}] {loc.name} ({loc.lat:.4f}, {loc.lon:.4f})")
        except ValueError as exc:
            print(f"{_red('Error:')} {exc}")

    async def _cmd_add_coords(self, args: list[str]) -> None:
        if len(args) < 3:
            print(f"{_yellow('Usage:')} add coords <lat> <lon> <name>")
            return

        try:
            lat = float(args[0])
            lon = float(args[1])
        except ValueError:
            print(f"{_red('Invalid coordinates.')} Lat and lon must be numbers.")
            return

        name = " ".join(args[2:])
        if not name:
            name = f"{lat:.4f}, {lon:.4f}"

        try:
            loc = self._loc_mgr.add(name, lat, lon)
            print(f"{_green('✓ Added')} [{self._loc_mgr.count - 1}] {loc.name} ({loc.lat:.4f}, {loc.lon:.4f})")
        except ValueError as exc:
            print(f"{_red('Error:')} {exc}")

    # =========================================================================
    # remove <index>
    # =========================================================================

    async def _cmd_remove(self, args: list[str]) -> None:
        if not args:
            print(f"{_yellow('Usage:')} remove <index>")
            return
        try:
            idx = int(args[0])
            loc = self._loc_mgr.remove(idx)
            print(f"{_green('✓ Removed')} {loc.name}")
        except (ValueError, IndexError) as exc:
            print(f"{_red('Error:')} {exc}")

    # =========================================================================
    # list
    # =========================================================================

    async def _cmd_list(self, args: list[str]) -> None:
        locations = self._loc_mgr.locations
        if not locations:
            print(f"{_yellow('No locations saved.')} Use 'add <city>' to add one.")
            return

        print(f"\n{_white('Saved Locations')} ({len(locations)}/{LocationManager.MAX_LOCATIONS}):")
        print(f"  {'─' * 60}")
        for i, loc in enumerate(locations):
            source_icon = "🔄" if loc.source == "default" else "📍"
            source_label = _cyan("(default)") if loc.source == "default" else ""
            station_icon = "📡" if any(k == loc.name.lower() for k in LOCATION_MAP) else source_icon
            print(f"  [{_cyan(str(i))}] {station_icon} {_white(loc.name)} {source_label}")
            print(f"      {loc.lat:.4f}, {loc.lon:.4f}  |  added {loc.added_at[:10]}")
        print()

    # =========================================================================
    # clear
    # =========================================================================

    async def _cmd_clear(self, args: list[str]) -> None:
        count = self._loc_mgr.clear()
        print(f"{_green('✓ Cleared')} {count} location(s).")

    # =========================================================================
    # scan
    # =========================================================================

    async def _cmd_scan(self, args: list[str]) -> None:
        locations = self._loc_mgr.locations
        if not locations:
            print(f"{_yellow('No locations to scan for.')} Use 'add <city>' first.")
            return

        print(f"\n{_cyan('Scanning Polymarket for weather markets...')}")
        print(f"  Locations: {len(locations)}")
        print(f"  Max scan: {WEATHER_MARKET_SCAN_MAX} markets, min liquidity: ${WEATHER_MIN_LIQUIDITY:,.0f}")
        print()

        try:
            matches = await self._discovery.scan_for_locations(locations)
        except Exception as exc:
            print(f"{_red('Scan failed:')} {exc}")
            return

        self._last_matches = matches

        if not matches:
            print(f"{_yellow('No weather markets found for your locations.')}")
            print(f"  This may mean Polymarket currently has no active temperature markets")
            print(f"  for your cities, or they're below the liquidity threshold.")
            return

        print(f"{_green('Found')} {len(matches)} market(s):\n")
        for i, mm in enumerate(matches):
            mkt = mm.market
            liq_str = f"${mm.liquidity:,.0f}" if mm.liquidity else "N/A"
            vol_str = f"${mm.volume_24h:,.0f}" if mm.volume_24h else "N/A"

            print(f"  [{_cyan(str(i))}] {_white(mkt.question[:100])}")
            print(f"      Location: {mm.location.name}  |  Date: {mkt.target_date}")
            print(f"      Liquidity: {liq_str}  |  24h Vol: {vol_str}")
            print(f"      Buckets: {len(mkt.buckets)}  |  Market ID: {mkt.market_id}")
            if mkt.buckets:
                bucket_strs = [b.label for b in mkt.buckets]
                print(f"      {', '.join(bucket_strs)}")
            print()

    # =========================================================================
    # analyze <index> [date]
    # =========================================================================

    def _parse_date_arg(self, date_str: str) -> int | None:
        """Parse a date argument to lead_days (0=today, 1=tomorrow, ...).

        Returns None if invalid.
        """
        if not date_str:
            return None
        date_str = date_str.lower().strip()
        if date_str == "today":
            return 0
        if date_str == "tomorrow":
            return 1
        if date_str.startswith("+") and date_str[1:].isdigit():
            n = int(date_str[1:])
            if 0 <= n <= 6:
                return n
            return None
        # Try YYYY-MM-DD
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
            today = date.today()
            delta = (target - today).days
            if 0 <= delta <= 6:
                return delta
            if delta < 0:
                print(f"{_yellow('⚠ Date is in the past.')}")
                return None
            print(f"{_yellow('⚠ Date is more than 6 days ahead, using +6.')}")
            return 6
        except ValueError:
            pass
        return None

    async def _cmd_analyze(self, args: list[str]) -> None:
        if not args:
            print(f"{_yellow('Usage:')} analyze <location_index> [date]")
            print(f"  date: today, tomorrow, +N (0-6), or YYYY-MM-DD")
            print(f"  Default: tomorrow")
            print(f"  Use 'list' to see your saved locations.")
            return

        try:
            idx = int(args[0])
            loc = self._loc_mgr.get(idx)
        except (ValueError, IndexError) as exc:
            print(f"{_red('Error:')} {exc}")
            return

        # Parse optional date argument
        lead_days = self._default_lead_days
        if len(args) >= 2:
            parsed = self._parse_date_arg(args[1])
            if parsed is None:
                print(f"{_red('Invalid date:')} {args[1]}")
                print(f"  Valid: today, tomorrow, +0..+6, YYYY-MM-DD")
                return
            lead_days = parsed

        target_date_label = (date.today() + timedelta(days=lead_days)).isoformat()

        print(f"\n{_cyan('Running BMA ensemble analysis for')} {_white(loc.name)}...")
        print(f"  Coordinates: {loc.lat:.4f}, {loc.lon:.4f}")
        print(f"  Target Date: {_yellow(target_date_label)} (lead_days={lead_days})")
        # Station info
        station = getattr(loc, "station", "")
        elev = getattr(loc, "station_elevation_m", 0.0)
        if station:
            print(f"  {_cyan(format_station_info(station, elev))}")
        uhi = getattr(loc, "uhi_adjustment", 0.0)
        if uhi > 0:
            print(f"  UHI-justering: +{uhi:.1f}°C (urban heat island)")
        print()

        result = await self._analyzer.analyze(loc, lead_days=lead_days)
        self._last_analyses[idx] = result

        if result.error:
            print(f"{_red('Analysis failed:')} {result.error}")
            return

        ens = result.ensemble
        if ens is None:
            print(f"{_red('No ensemble data returned.')}")
            return

        print(f"{_white('═══ BMA Ensemble Forecast ═══')}")
        print(f"  Location:      {ens.location}")
        try:
            local_now = datetime.now(ZoneInfo(loc.tz))
            print(f"  {_cyan('🕐 Lokal tid:')}   {local_now.strftime('%H:%M %Z (%Y-%m-%d)')}")
        except Exception:
            pass
        print(f"  Target Date:   {ens.target_date or 'today'}")
        print(f"  Lead Days:     {ens.lead_days}")
        print(f"  Models Used:   {ens.model_count}")
        print(f"  Time:          {result.elapsed_seconds:.1f}s")
        print()
        print(f"  {_white('Temperature Distribution:')}")
        print(f"    Mean (BMA):  {_green(f'{ens.mean_temp_f:.1f}°F')}  ({DailyForecast.f_to_c(ens.mean_temp_f):.1f}°C)")
        print(f"    Std Dev:     {ens.std_temp_f:.2f}°F")
        print(f"    Median:      {ens.median_temp_f:.1f}°F")
        print(f"    P5-P95:      {ens.p05_temp_f:.1f}°F – {ens.p95_temp_f:.1f}°F")
        print(f"    P10-P90:     {ens.p10_temp_f:.1f}°F – {ens.p90_temp_f:.1f}°F")
        print(f"    Confidence:  {ens.confidence:.3f}")
        print()

        if ens.weights_snapshot:
            print(f"  {_white('Model Weights:')}")
            for model, w in sorted(ens.weights_snapshot.items(), key=lambda x: -x[1]):
                bar = "█" * int(w * 40)
                print(f"    {model:<20s} {w:.3f}  {bar}")
            print()

        if ens.individual_models:
            print(f"  {_white('Individual Model Forecasts:')}")
            for model, temp_f in ens.individual_models.items():
                diff = temp_f - ens.mean_temp_f
                sign = "+" if diff >= 0 else ""
                print(f"    {model:<20s} {temp_f:.1f}°F  ({sign}{diff:.1f}°F vs BMA)")
            print()

        # If we have matching markets from a scan, compute bucket probabilities
        matching = [mm for mm in self._last_matches if mm.location.name.lower() == loc.name.lower()]
        _bucket_data: list[dict[str, Any]] = []
        if matching:
            print(f"  {_white('Bucket Probabilities vs Matched Markets:')}")
            for mm in matching:
                mkt = mm.market
                print(f"    Market: {mkt.question[:80]}")
                outcome_prices = self._discovery._parse_json_array(mm.raw_data.get("outcomePrices"))
                for bucket in mkt.buckets:
                    prob = self._analyzer.compute_bucket_prob(
                        bma_mean_f=ens.mean_temp_f,
                        bma_std_f=ens.std_temp_f,
                        bucket_min_f=bucket.min_val,
                        bucket_max_f=bucket.max_val,
                        is_open_upper=bucket.is_open_upper,
                    )
                    bucket_idx = mkt.buckets.index(bucket)
                    mkt_price = None
                    if bucket_idx < len(outcome_prices):
                        try:
                            mkt_price = float(outcome_prices[bucket_idx])
                        except (ValueError, TypeError):
                            pass
                    _bucket_data.append({
                        "label": bucket.label,
                        "prob": prob,
                        "mkt_price": mkt_price,
                    })
                    mkt_str = f"  P(mkt)={mkt_price:.1%}" if mkt_price is not None else ""
                    print(f"      {bucket.label:<15s}  P(bot)={_cyan(f'{prob:.1%}')} {mkt_str}")
            print()

        # --- 🎯 Prediksjon & Anbefaling ---
        if matching and _bucket_data:
            self._print_prediction(ens, loc, target_date_label, _bucket_data)

        # --- 🔴 LIVE PEAK STATUS ---
        if lead_days <= 1:  # Only for today/tomorrow
            print(f"  {_yellow('═══════════════════════════════════════════')}")
            print(f"  {_white('🔴 LIVE PEAK STATUS')}")
            print(f"  {_yellow('═══════════════════════════════════════════')}")
            try:
                temp_data = await self._analyzer.get_current_temp(loc.lat, loc.lon, loc.tz)
            except Exception:
                temp_data = None

            if temp_data and temp_data.get("temp_c") is not None:
                cur_temp = temp_data["temp_c"]
                cur_time = temp_data.get("time_local") or datetime.now()

                # Compute suggested temp
                mean_c = DailyForecast.f_to_c(ens.mean_temp_f)
                uhi = getattr(loc, "uhi_adjustment", 0.0)
                adj_mean = mean_c + uhi
                suggested_temp = float(int(round(adj_mean if uhi > 0 else mean_c)))

                # Build simple obs history
                obs_history: list[tuple[datetime, float]] = [(cur_time, cur_temp)]
                today_max_tuple = (cur_temp, cur_time)

                # Local time
                try:
                    local_now = datetime.now(ZoneInfo(loc.tz))
                except Exception:
                    local_now = datetime.now()

                peak_start = getattr(loc, "peak_hour_start", 14)
                peak_end = getattr(loc, "peak_hour_end", 16)
                target_date_obj = date.today() + timedelta(days=lead_days)

                peak_state = detect_peak_state(
                    obs_history=obs_history,
                    today_max=today_max_tuple,
                    peak_hour_start=peak_start,
                    peak_hour_end=peak_end,
                    local_now=local_now,
                    target_date=target_date_obj,
                    peak_confirmed=None,
                    suggested_temp=suggested_temp,
                )

                live_conf, mins_since_max, mins_decline, alert_level, alert_msg = (
                    compute_live_confidence(
                        obs_history=obs_history,
                        today_max=today_max_tuple,
                        peak_hour_start=peak_start,
                        peak_hour_end=peak_end,
                        local_now=local_now,
                        suggested_temp=suggested_temp,
                    )
                )

                trend = peak_state.trend
                tmax_time_str = cur_time.strftime("%H:%M") if hasattr(cur_time, "strftime") else "—"

                # Color-code live confidence
                if live_conf >= 80:
                    live_color = _red
                elif live_conf >= 60:
                    live_color = _yellow
                elif live_conf >= 30:
                    live_color = _yellow
                else:
                    live_color = _cyan

                # Display
                print(f"  🌡️ Nå: {_green(f'{cur_temp:.1f}°C')} {trend} | Dagens maks: {cur_temp:.1f}°C ({tmax_time_str})")
                print(f"  ⚡ Peak confidence: {live_color(f'{live_conf:.0f}%')} — {peak_state.emoji} {peak_state.state_label}")
                if mins_decline > 0:
                    print(f"  📉 Synkende i {mins_decline} min | ⏱️ Siden siste rekord: {mins_since_max} min")

                # Dagspeak summary
                in_peak = peak_start <= local_now.hour < peak_end
                peak_window_str = f"{peak_start:02d}:00-{peak_end:02d}:00"
                now_str = local_now.strftime("%H:%M")
                if peak_state.state == "possible_peak":
                    print(f"  🎯 Dagspeak: Sannsynlig nådd ({live_conf:.0f}% konfidens)")
                elif peak_state.state == "confirmed":
                    print(f"  🎯 Dagspeak: Bekreftet — {cur_temp:.1f}°C")
                elif peak_state.state == "peak_window":
                    print(f"  🎯 Dagspeak: I peak-vindu — kan fortsatt stige")
                print(f"     Forventet peak-vindu: {peak_window_str} | Nå: {now_str}" +
                      (" (inne i vindu)" if in_peak else " (utenfor vindu)"))
                print(f"  Status: {peak_state.emoji} {peak_state.message}")
            else:
                print(f"  {_yellow('  ⚠️ Kunne ikke hente nåværende temperatur')}")
            print()

    # =========================================================================
    # confidence <index> [date] — NEW: pure confidence analysis
    # =========================================================================

    async def _cmd_confidence(self, args: list[str]) -> None:
        """Run confidence analysis for a location."""
        if not args:
            print(f"{_yellow('Usage:')} confidence <location_index> [date]")
            print(f"  Performs pure confidence analysis with per-bucket breakdown.")
            return

        try:
            idx = int(args[0])
            loc = self._loc_mgr.get(idx)
        except (ValueError, IndexError) as exc:
            print(f"{_red('Error:')} {exc}")
            return

        lead_days = self._default_lead_days
        if len(args) >= 2:
            parsed = self._parse_date_arg(args[1])
            if parsed is None:
                print(f"{_red('Invalid date:')} {args[1]}")
                return
            lead_days = parsed

        target_date_label = (date.today() + timedelta(days=lead_days)).isoformat()
        print(f"\n{_cyan('Running confidence analysis for')} {_white(loc.name)}...")

        cr = await self._analyzer.analyze_confidence_async(loc, lead_days=lead_days)
        self._last_confidence[idx] = cr

        ens = cr.ensemble
        mean_c = WeatherAnalyzer.f_to_c(ens.mean_temp_f)
        p5_c = WeatherAnalyzer.f_to_c(ens.p05_temp_f)
        p95_c = WeatherAnalyzer.f_to_c(ens.p95_temp_f)
        range_c = p95_c - p5_c

        print()
        print(f"{_white('═══ CONFIDENCE ANALYSE ═══')}")
        print(f"  {_white(f'🎯 ANALYSE — {loc.name} — {target_date_label}')}")
        print()
        print(f"  🌡️ BMA Ensemble: {_green(f'{mean_c:.1f}°C')} (P5: {p5_c:.1f}°C, P95: {p95_c:.1f}°C)")
        print(f"     Range: {range_c:.1f}°C | Confidence: {ens.confidence:.0%} | Models: {ens.model_count}")
        print()
        print(f"  📊 {_white('Temperatursannsynligheter:')}")

        for b in sorted(cr.buckets, key=lambda x: x.confidence, reverse=True):
            if b.probability < 0.01:
                continue
            star = " ⭐ HØYEST" if b == cr.best_bucket else ""
            print(f"    {b.label:<12s} {b.probability:.1%} (confidence: {b.confidence:.0%})  "
                  f"{b.models_agree}/{b.total_models} modeller{star}")

        print()
        print(f"  💡 {_white('Generelle råd:')}")
        if cr.best_bucket:
            print(f"    ✅ Sikreste prediksjon: {cr.best_bucket.label} "
                  f"({cr.best_bucket.confidence:.0%} confidence, "
                  f"{cr.best_bucket.models_agree} av {cr.best_bucket.total_models} modeller enige)")

        # Find bucket with largest uncertainty
        uncertain = min(cr.buckets, key=lambda x: x.confidence) if cr.buckets else None
        if uncertain and uncertain != cr.best_bucket and uncertain.probability > 0.01:
            print(f"    ⚠️ Størst usikkerhet: {uncertain.label} "
                  f"({uncertain.confidence:.0%} confidence, kun {uncertain.models_agree} modeller innenfor)")

        print(f"    📉 Analyse fullført på {cr.elapsed_seconds:.1f}s")
        print()

    def _print_prediction(
        self,
        ens: Any,
        loc: Any,
        target_date_str: str,
        bucket_data: list[dict[str, Any]],
    ) -> None:
        """Print the 🎯 Prediksjon & Anbefaling block (confidence-based)."""
        mean_c = DailyForecast.f_to_c(ens.mean_temp_f)
        p5_c = DailyForecast.f_to_c(ens.p05_temp_f)
        p95_c = DailyForecast.f_to_c(ens.p95_temp_f)
        total_models = max(1, ens.model_count)

        # UHI adjustment
        uhi = getattr(loc, "uhi_adjustment", 0.0)
        uhi_str, adj_mean = format_uhi_info(mean_c, uhi)

        # Station info
        station = getattr(loc, "station", "")
        elev = getattr(loc, "station_elevation_m", 0.0)
        station_str = format_station_info(station, elev)

        # Spread info
        spread_str, spread_val = format_spread_info(p5_c, p95_c, ens, mean_c)

        print(f"  {_yellow('═══════════════════════════════════════════')}")
        print(f"  {_white('🎯 PREDIKSJON')} — {loc.name} — {target_date_str}")
        print(f"  {_yellow('═══════════════════════════════════════════')}")
        print()
        if station_str:
            print(f"  {_cyan(station_str)}")
        print(f"  {uhi_str}")
        print(f"     P5: {p5_c:.1f}°C – P95: {p95_c:.1f}°C | Ensemble konfidens: {ens.confidence:.0%} | Modeller: {ens.model_count}")
        for line in spread_str.split("\n"):
            if line.strip():
                print(f"  {line}")
        print()

        # P5-P95 explanation
        print(f"  💡 P5-P95 = 90% konfidensintervall. "
              f"P5={p5_c:.1f} betyr 95% sjanse for ≥{p5_c:.1f}°C. "
              f"P95={p95_c:.1f} betyr 95% sjanse for ≤{p95_c:.1f}°C.")
        print()

        # Compute per-bucket confidence (without printing yet)
        bucket_entries: list[dict[str, Any]] = []
        for d in bucket_data:
            prob = d["prob"]
            label = d["label"]
            mkt_price = d.get("mkt_price")

            # Model agreement for this bucket
            models_in = 0
            if ens.individual_models:
                for model_temp_f in ens.individual_models.values():
                    model_temp_c = DailyForecast.f_to_c(model_temp_f)
                    # Parse bucket label
                    m = re.match(r"(\d+)\s*[-–]\s*(\d+)", label)
                    if m:
                        lo, hi = float(m.group(1)), float(m.group(2))
                        if lo <= model_temp_c < hi:
                            models_in += 1
                    elif label.startswith("<"):
                        nums = re.findall(r"[\d.]+", label)
                        if nums and model_temp_c < float(nums[0]):
                            models_in += 1
                    elif ">" in label or "+" in label:
                        nums = re.findall(r"[\d.]+", label)
                        if nums and model_temp_c >= float(nums[0]):
                            models_in += 1

            agree_ratio = models_in / total_models if total_models > 0 else 0
            range_width = p95_c - p5_c
            narrow_bonus = 1.0 / (1.0 + max(0, range_width / 8.0))
            bucket_conf = ens.confidence * (0.4 + 0.6 * agree_ratio) * min(1.0, 1.0 + narrow_bonus * 0.3)
            bucket_conf = min(0.99, max(0.05, bucket_conf))

            bucket_entries.append({"label": label, "prob": prob, "confidence": bucket_conf, "models_agree": models_in, "total_models": total_models, "mkt_price": mkt_price})

        # --- 🎯 FORESLÅTT SPILL ---
        if bucket_entries:
            # Use UHI-adjusted mean for suggested temp
            use_mean = adj_mean if uhi > 0 else mean_c
            suggested_temp = int(round(use_mean))
            best_conf_bucket = max(bucket_entries, key=lambda d: d["confidence"])
            best_label = best_conf_bucket["label"]

            # Extract representative temperature from best bucket label
            _m = re.match(r"(\d+)\s*[-–]\s*(\d+)", best_label)
            if _m:
                best_bucket_temp = int(round((float(_m.group(1)) + float(_m.group(2))) / 2.0))
            elif best_label.startswith("<"):
                _nums = re.findall(r"[\d.]+", best_label)
                best_bucket_temp = int(float(_nums[0])) - 2 if _nums else suggested_temp
            elif ">" in best_label or "+" in best_label:
                _nums = re.findall(r"[\d.]+", best_label)
                best_bucket_temp = int(float(_nums[0])) + 2 if _nums else suggested_temp
            else:
                _nums = re.findall(r"[\d.]+", best_label)
                best_bucket_temp = int(round(float(_nums[0]))) if _nums else suggested_temp

            bucket_differs = best_bucket_temp != suggested_temp

            print(f"  {_white('🎯 FORESLÅTT SPILL:')} {_green(f'{suggested_temp}°C')}" + (f" (UHI-justert fra {mean_c:.1f}°C)" if uhi > 0 else ""))
            if bucket_differs and best_conf_bucket["confidence"] >= 0.5:
                print(f"     BMA snitt: {mean_c:.1f}°C → nærmeste: {suggested_temp}°C")
                print(f"     Høyest bucket-konfidens: {best_label} "
                      f"(konfidens {best_conf_bucket['confidence']:.0%}, "
                      f"{best_conf_bucket['models_agree']}/{best_conf_bucket['total_models']} modeller)")
                print(f"     {_yellow('⚠️ Merk:')} bucket {best_label} har høyere konfidens enn avrundet snitt")
            else:
                print(f"     P5-P95 interval: {p5_c:.1f}°C – {p95_c:.1f}°C")
                print(f"     90% sannsynlig at temperaturen havner i dette området")
                print(f"     Beste enkeltverdi: {suggested_temp}°C "
                      f"(nærmest BMA-snitt på {mean_c:.1f}°C med høyest bucket-sannsynlighet)")
            print()

        # Now print bucket table
        print(f"  {_white('📊 Bucket-sannsynligheter (BMA konfidens):')}")
        for be in bucket_entries:
            conf = be["confidence"]
            prob = be["prob"]
            label = be["label"]
            models_agree = be["models_agree"]
            total_models = be["total_models"]
            mkt_price = be.get("mkt_price")

            if conf >= 0.85:
                indicator = "🟢"
                conf_color = _green
            elif conf >= 0.70:
                indicator = "🟡"
                conf_color = _yellow
            elif conf >= 0.50:
                indicator = "🟠"
                conf_color = _yellow
            else:
                indicator = "🔴"
                conf_color = _red

            mkt_str = f"  |  Marked: {mkt_price:.1%}" if mkt_price is not None else ""
            print(f"     {label:<12s} → Bot: {prob:.1%}{mkt_str}  |  {conf_color(f'Konfidens: {conf:.0%}')}  {indicator}  ({models_agree}/{total_models} modeller)")

        print()
        print(f"  {_white('💡 ANBEFALING:')}")

        if not bucket_entries:
            print(f"     ⚪ INGEN DATA — kan ikke gi anbefaling")
        else:
            best = max(bucket_entries, key=lambda d: d["confidence"])
            conf_val = best["confidence"]

            if conf_val > 0.85:
                rec = f"✅ SIKKER — \"{best['label']}\" ({best['confidence']:.0%} konfidens, {best['models_agree']}/{best['total_models']} modeller)"
                rec_color = _green
                strength = "SIKKER"
            elif conf_val >= 0.70:
                rec = f"🟡 MODERAT — \"{best['label']}\" ({best['confidence']:.0%} konfidens, {best['models_agree']}/{best['total_models']} modeller)"
                rec_color = _yellow
                strength = "MODERAT"
            else:
                rec = f"🔴 USIKKER — \"{best['label']}\" ({best['confidence']:.0%} konfidens, kun {best['models_agree']}/{best['total_models']} modeller)"
                rec_color = _red
                strength = "USIKKER"

            print(f"     {rec_color(rec)}")
            print(f"     Konfidens: {conf_val:.0%} — {strength}")

            # Kelly Criterion
            kelly_str = format_kelly_info(conf_val)
            if kelly_str:
                for line in kelly_str.split("\n"):
                    print(f"     {_magenta(line)}")
            else:
                if conf_val > 0.85:
                    print(f"     Anbefalt posisjon: 2-5% av bankroll")
                elif conf_val >= 0.70:
                    print(f"     Anbefalt posisjon: 1-3% av bankroll")
                else:
                    print(f"     Ingen handel anbefalt — modellene er for uenige")

            print(f"     {_yellow('⚠️ Sjekk motpart før handel!')}")
        print()

    # =========================================================================
    # bulk [date]
    # =========================================================================

    async def _cmd_bulk(self, args: list[str]) -> None:
        """Run confidence analysis on ALL locations, rank by confidence, show top 5."""
        locations = self._loc_mgr.locations
        if not locations:
            print(f"{_yellow('No locations.')} Use 'add <city>' or 'reset_defaults' first.")
            return

        # Parse optional date
        lead_days = self._default_lead_days
        if args:
            parsed = self._parse_date_arg(args[0])
            if parsed is None:
                print(f"{_red('Invalid date:')} {args[0]}")
                return
            lead_days = parsed

        target_date_label = (date.today() + timedelta(days=lead_days)).isoformat()

        print(f"\n{_white('═══ BULK ANALYSE — Topp 5 Høyest Confidence ═══')}")
        print(f"  Dato: {_yellow(target_date_label)}  |  Byer: {len(locations)}")
        print(f"  {'─' * 55}\n")

        def progress_cb(done: int, total: int, name: str) -> None:
            print(f"\r  Analyserer [{done}/{total}] {name}...", end="", flush=True)

        t_start = time.perf_counter()
        results = await self._analyzer.bulk_confidence_analysis(
            locations, lead_days=lead_days, progress_callback=progress_cb,
        )
        elapsed = time.perf_counter() - t_start

        print("\r" + " " * 60 + "\r", end="")  # Clear progress line
        print(f"  ⏱️ Analyse fullført på {elapsed:.1f}s")
        print()

        # Filter out errors
        valid = [r for r in results if "error" not in r]
        if not valid:
            print(f"{_yellow('Ingen byer kunne analyseres.')}\n")
            return

        top5 = valid[:5]
        medals = ["🥇", "🥈", "🥉", "⭐", "⭐"]

        # Load correlation data for warnings
        correlations = self._loc_mgr.load_correlations()

        print(f"\n{_white('🏆 TOP 5 — HØYEST CONFIDENCE')}")
        print(f"  {'═' * 55}\n")

        # ---- Correlation Warning Check ----
        top5_city_names = [c["city"] for c in top5]
        corr_warnings = check_correlations(top5_city_names, correlations)
        if corr_warnings:
            for w in corr_warnings:
                print(f"  {_yellow(w)}")
            print()

        # Build location lookup for UHI/station info
        loc_lookup: dict[str, SavedLocation] = {}
        for loc in locations:
            loc_lookup[loc.name] = loc

        for rank, c in enumerate(top5):
            medal = medals[rank]
            conf_pct = c["overall_confidence"] * 100
            mean_c = c["mean_c"]
            p5_c = c["p5_c"]
            p95_c = c["p95_c"]
            range_c = c["range_c"]
            city_name = c["city"]

            # Get UHI and station info
            loc = loc_lookup.get(city_name)
            uhi = getattr(loc, "uhi_adjustment", 0.0) if loc else 0.0
            station = getattr(loc, "station", "") if loc else ""
            elev = getattr(loc, "station_elevation_m", 0.0) if loc else 0.0

            # UHI-adjusted mean
            uhi_str, adj_mean = format_uhi_info(mean_c, uhi)

            # Color code confidence
            if conf_pct >= 85:
                conf_color = _green
                conf_icon = "🟢"
            elif conf_pct >= 70:
                conf_color = _yellow
                conf_icon = "🟡"
            elif conf_pct >= 50:
                conf_color = _yellow
                conf_icon = "🟠"
            else:
                conf_color = _red
                conf_icon = "🔴"

            print(f"  #{rank+1} {medal} {_white(city_name)} — Confidence: {conf_color(f'{conf_pct:.0f}%')} {conf_icon}")
            try:
                tz = c.get("tz", "UTC")
                local_now = datetime.now(ZoneInfo(tz))
                print(f"      {_cyan('🕐 Lokal tid:')} {local_now.strftime('%H:%M %Z (%Y-%m-%d)')}")
            except Exception:
                pass

            # Station info
            if station:
                station_str = format_station_info(station, elev)
                if station_str:
                    print(f"      {_cyan(station_str)}")

            # UHI-adjusted BMA
            print(f"      {uhi_str}")
            print(f"      P5: {p5_c:.1f}°C – P95: {p95_c:.1f}°C — Range: {range_c:.1f}°C")

            # Ensemble spread signal
            if c.get("model_count", 0) > 0:
                ens_models = c.get("model_count", 0)
                spread_str = f"      📊 Modell-spredning: {range_c:.1f}°C"
                if range_c <= 2.0:
                    spread_str += " (smal = høy konfidens)"
                elif range_c > 5.0:
                    spread_str += " ⚠️ Høy spredning — mulig edge hvis du treffer"
                print(spread_str)

            suggested_temp = int(round(adj_mean if uhi > 0 else mean_c))
            print(f"   🎯 Foreslått spill: {suggested_temp}°C (BMA justert: {adj_mean:.1f}°C)" if uhi > 0 else f"   🎯 Foreslått spill: {suggested_temp}°C (snitt {mean_c:.1f}°C)")

            if c.get("best_bucket"):
                bb = c["best_bucket"]
                print(f"      Best bucket: {bb['label']} ({bb['confidence']:.0%} confidence, "
                      f"{bb['models_agree']}/{bb['total_models']} modeller)")

            # Kelly Criterion
            win_prob = conf_pct / 100.0
            if win_prob > 0.5:
                kelly_str = format_kelly_info(win_prob)
                if kelly_str:
                    for line in kelly_str.split("\n"):
                        print(f"      {_magenta(line)}")

            print()

        print(f"  Analyserte {len(locations)} byer — {len(valid)} vellykket.")
        total_elapsed = sum(r.get("elapsed", 0) for r in valid)
        print(f"  Total tid: {total_elapsed:.1f}s\n")

    # =========================================================================
    # monitor [date] — live monitoring with current temp + peak detection
    # =========================================================================

    async def _cmd_monitor(self, args: list[str]) -> None:
        """Run bulk analysis, fetch current temps, and show peak detection status."""
        locations = self._loc_mgr.locations
        if not locations:
            print(f"{_yellow('No locations.')} Use 'add <city>' or 'reset_defaults' first.")
            return

        lead_days = self._default_lead_days
        if args:
            parsed = self._parse_date_arg(args[0])
            if parsed is None:
                print(f"{_red('Invalid date:')} {args[0]}")
                return
            lead_days = parsed

        target_date = date.today() + timedelta(days=lead_days)
        target_date_label = target_date.isoformat()

        print(f"\n{_white('═══ LIVE OVERVÅKNING — ') + _cyan(target_date_label) + _white(' ═══')}")
        print(f"  Lead days: {lead_days}  |  Byer: {len(locations)}")
        print()

        # 1. Run quick bulk confidence analysis
        print(f"  {_cyan('Kjører BMA ensemble...')}")
        t_start = time.perf_counter()
        results = await self._analyzer.bulk_confidence_analysis(locations, lead_days=lead_days)
        elapsed = time.perf_counter() - t_start
        print(f"  {_green('✓')} BMA fullført på {elapsed:.1f}s")
        print()

        valid = [r for r in results if "error" not in r]
        if not valid:
            print(f"{_yellow('Ingen byer kunne analyseres.')}\n")
            return

        top5 = valid[:5]
        today_local = date.today()

        # 2. Fetch current temps for top 5
        print(f"  {_cyan('Henter nåværende temperaturer...')}")
        current_temps: dict[str, dict[str, Any] | None] = {}
        for c in top5:
            name = c["city"]
            lat = c["lat"]
            lon = c["lon"]
            tz = c.get("tz", "UTC")
            # Small delay to be polite to API
            await asyncio.sleep(0.5)
            temp_data = await self._analyzer.get_current_temp(lat, lon, tz)
            current_temps[name] = temp_data
        print()

        # 3. Display top 5 with live monitoring data
        medals = ["🥇", "🥈", "🥉", "⭐", "⭐"]

        for rank, c in enumerate(top5):
            name = c["city"]
            medal = medals[rank]
            conf_pct = c["overall_confidence"] * 100
            mean_c = c["mean_c"]
            p5_c = c["p5_c"]
            p95_c = c["p95_c"]
            tz = c.get("tz", "UTC")

            # Current temp
            cur_data = current_temps.get(name)
            cur_temp = cur_data["temp_c"] if cur_data else None
            cur_time = cur_data.get("time_local") if cur_data else None

            # Local time
            try:
                local_now = datetime.now(ZoneInfo(tz))
                local_str = local_now.strftime("%H:%M %Z")
            except Exception:
                local_now = datetime.now()
                local_str = local_now.strftime("%H:%M")

            # Peak detection (date-aware)
            peak_start = 14
            peak_end = 16
            for loc in locations:
                if loc.name == name:
                    peak_start = loc.peak_hour_start
                    peak_end = loc.peak_hour_end
                    break

            # Build simple obs history for peak detection
            obs_history: list[tuple[datetime, float]] = []
            if cur_temp is not None and cur_time is not None:
                obs_history.append((cur_time, cur_temp))

            today_max_tuple = None
            if cur_temp is not None and cur_time is not None:
                today_max_tuple = (cur_temp, cur_time)

            peak_state = detect_peak_state(
                obs_history=obs_history,
                today_max=today_max_tuple,
                peak_hour_start=peak_start,
                peak_hour_end=peak_end,
                local_now=local_now,
                target_date=target_date,
                peak_confirmed=None,
            )

            # Trend arrow
            trend = peak_state.trend

            # Color coding
            if conf_pct >= 85:
                conf_color = _green
                conf_icon = "🟢"
            elif conf_pct >= 70:
                conf_color = _yellow
                conf_icon = "🟡"
            elif conf_pct >= 50:
                conf_color = _yellow
                conf_icon = "🟠"
            else:
                conf_color = _red
                conf_icon = "🔴"

            print(f"  #{rank+1} {medal} {_white(name)} — {peak_state.emoji} {peak_state.state_label}")
            print(f"      {_cyan('🕐 Lokal tid:')} {local_str} ({target_date_label})")
            if cur_temp is not None:
                print(f"      🌡️ Nå: {_green(f'{cur_temp:.1f}°C')} {trend}  (BMA: {mean_c:.1f}°C)")
            else:
                print(f"      🌡️ Nå: N/A  (BMA: {mean_c:.1f}°C)")
            print(f"      📊 BMA: {mean_c:.1f}°C (P5: {p5_c:.1f}, P95: {p95_c:.1f}) — Confidence: {conf_color(f'{conf_pct:.0f}%')} {conf_icon}")
            print(f"      ⏳ Forventet peak: {peak_start:02d}:00-{peak_end:02d}:00 {tz}")
            print(f"      Status: {peak_state.emoji} {peak_state.message}")
            print()

        print(f"  {_yellow('⚠️')} Overvåkning: BMA hvert 15. min, nåværende temp hvert 5. min (GUI).")
        print(f"  Bruk 'monitor' på nytt for oppdatert status.\n")

    # =========================================================================
    # reset_defaults
    # =========================================================================

    async def _cmd_reset_defaults(self, args: list[str]) -> None:
        """Reset all locations to the default database."""
        print(f"{_yellow('Tilbakestiller til standardbyer...')}")
        count = self._loc_mgr.reset_to_defaults()
        self._last_matches = []
        self._last_analyses = {}
        self._last_confidence = {}
        print(f"{_green('✓')} Lastet {count} standardbyer fra databasen.")
        print(f"  Bruk 'list' for å se alle, 'confidence <index>' for analyse.")

    # =========================================================================
    # date [value]
    # =========================================================================

    async def _cmd_date(self, args: list[str]) -> None:
        if not args:
            today = date.today()
            target = today + timedelta(days=self._default_lead_days)
            print(f"  Standard analysedato: {_cyan(target.isoformat())} (lead_days={self._default_lead_days})")
            print(f"  Bruk 'date tomorrow', 'date +3', 'date YYYY-MM-DD' for å endre.")
            return

        parsed = self._parse_date_arg(args[0])
        if parsed is None:
            print(f"{_red('Invalid date:')} {args[0]}")
            print(f"  Valid: today, tomorrow, +0..+6, YYYY-MM-DD")
            return

        self._default_lead_days = parsed
        target = date.today() + timedelta(days=parsed)
        print(f"  {_green('✓')} Standard analysedato satt til: {_cyan(target.isoformat())} (lead_days={parsed})")

    # =========================================================================
    # help
    # =========================================================================

    async def _cmd_help(self, args: list[str]) -> None:
        print(f"""
{_white('═══ Weather Monitor CLI Commands ═══')}

{_cyan('Location Management:')}
  {_green('add <city>')}              Add location by city name (e.g., "add Oslo, NO")
  {_green('add coords <lat> <lon> <name>')}  Add location by coordinates
  {_green('remove <index>')}          Remove location by index
  {_green('list')}                   List all saved locations (shows default/manual)
  {_green('clear')}                  Remove all saved locations
  {_green('reset_defaults')}         Reset all locations to the 51-city default database

{_cyan('Weather Analysis:')}
  {_green('analyze <index> [date]')}    Run BMA ensemble analysis for a single location
  {_green('confidence <index> [date]')}  Run confidence analysis with per-bucket breakdown
  {_green('bulk [date]')}               Run confidence analysis on ALL locations, rank by confidence
  {_green('monitor [date]')}            Live monitoring: BMA + current temp + peak detection for top 5
  {_green('date [value]')}              Show/set default analysis date (today, tomorrow, +N, YYYY-MM-DD)

{_cyan('Market Discovery:')}
  {_green('scan')}                   Scan Polymarket for weather markets matching your locations

{_cyan('General:')}
  {_green('help')}                   Show this help
  {_green('quit / exit / q')}        Exit the CLI
""")

    # =========================================================================
    # quit
    # =========================================================================

    async def _cmd_quit(self, args: list[str]) -> None:
        self._running = False


# =============================================================================
# Entry Point
# =============================================================================

async def main() -> None:
    cli = WeatherMonitorCLI()
    await cli.run()


if __name__ == "__main__":
    asyncio.run(main())
