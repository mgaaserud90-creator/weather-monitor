#!/usr/bin/env python3
"""
Værmonitor Backtest — 30-Day Historical Validation Script
==========================================================

Validates the BMA Multi-Model Ensemble predictions against real historical
temperature data from the Open-Meteo Archive API.

Approach:
  1. Fetch actual daily max temperatures for the last 30 days per city
  2. Run BMA ensemble for tomorrow (lead_days=1) for each city
  3. Compare: how often did actual temps fall within P5-P95? (calibration)
  4. Compare: how often was actual within ±2°C of BMA mean? (stability)
  5. Compute suggested_spill hit rate against historical data
  6. Output formatted tables + JSON results

Usage:
    cd polymarket-arb-bot
    python _backtest_30days.py
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the polymarket-arb-bot package root is on sys.path
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
    class _NoColors:
        def __getattr__(self, name: str) -> str:
            return ""
    Fore = _NoColors()  # type: ignore[assignment]
    Style = _NoColors()  # type: ignore[assignment]
    def colorama_init():
        pass

import httpx

# ---------------------------------------------------------------------------
# Project imports
# ---------------------------------------------------------------------------
from weather_monitor_cli import (
    SavedLocation,
    WeatherAnalyzer,
)

# =============================================================================
# Constants
# =============================================================================

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"
DEFAULTS_FILE = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
OUTPUT_FILE = Path(_SCRIPT_DIR) / "_backtest_results.json"
BACKTEST_DAYS = 30
CONCURRENT_FETCHES = 10  # Max parallel HTTP requests
CONCURRENT_BMA = 5       # Max parallel BMA analyses

C = Fore
RESET = Style.RESET_ALL
BOLD = Style.BRIGHT if hasattr(Style, "BRIGHT") else ""

# =============================================================================
# Color/Format Helpers
# =============================================================================

def _green(s: str) -> str: return f"{C.GREEN}{BOLD}{s}{RESET}"
def _red(s: str) -> str: return f"{C.RED}{BOLD}{s}{RESET}"
def _yellow(s: str) -> str: return f"{C.YELLOW}{s}{RESET}"
def _cyan(s: str) -> str: return f"{C.CYAN}{s}{RESET}"
def _white(s: str) -> str: return f"{C.WHITE}{BOLD}{s}{RESET}"
def _magenta(s: str) -> str: return f"{C.MAGENTA}{s}{RESET}"


def _conf_emoji(conf: float) -> str:
    """Return emoji indicator for confidence level."""
    if conf > 0.80:
        return "🟢"
    elif conf >= 0.70:
        return "🟠"
    else:
        return "🔴"


def _conf_pct_str(conf: float) -> str:
    """Return confidence percentage with color."""
    pct = conf * 100
    if conf > 0.80:
        return _green(f"{pct:.0f}%")
    elif conf >= 0.70:
        return _yellow(f"{pct:.0f}%")
    else:
        return _red(f"{pct:.0f}%")


def _truncate(s: str, maxlen: int = 18) -> str:
    """Truncate a string to maxlen characters."""
    if len(s) <= maxlen:
        return s
    return s[:maxlen - 1] + "…"


# =============================================================================
# Data Loading
# =============================================================================

def load_default_cities(path: Path = DEFAULTS_FILE) -> list[SavedLocation]:
    """Load all 51 default cities from weather_monitor_defaults.json."""
    if not path.exists():
        print(f"{_red('ERROR:')} Defaults file not found: {path}")
        sys.exit(1)

    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = data.get("default_locations", [])
    locations = [
        SavedLocation(
            name=d["name"],
            lat=d["lat"],
            lon=d["lon"],
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
    return locations


# =============================================================================
# Historical Data Fetching
# =============================================================================

async def fetch_historical(
    client: httpx.AsyncClient,
    loc: SavedLocation,
    days: int = BACKTEST_DAYS,
) -> dict[str, Any]:
    """Fetch actual daily max temperatures for the last N days from Open-Meteo Archive.

    Returns dict with keys: city, lat, lon, tz, dates, temps_max_c, error.
    """
    today = date.today()
    start_date = today - timedelta(days=days)
    end_date = today - timedelta(days=1)  # up to yesterday

    try:
        resp = await client.get(
            ARCHIVE_API_URL,
            params={
                "latitude": loc.lat,
                "longitude": loc.lon,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "daily": "temperature_2m_max",
                "timezone": loc.tz if loc.tz != "UTC" else "UTC",
            },
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()

        daily = data.get("daily", {})
        dates_list = daily.get("time", [])
        temps_raw = daily.get("temperature_2m_max", [])

        # Filter out None values
        temps_clean: list[float] = []
        dates_clean: list[str] = []
        for i, t in enumerate(temps_raw):
            if t is not None:
                temps_clean.append(float(t))
                if i < len(dates_list):
                    dates_clean.append(dates_list[i])

        return {
            "city": loc.name,
            "lat": loc.lat,
            "lon": loc.lon,
            "tz": loc.tz,
            "dates": dates_clean,
            "temps_max_c": temps_clean,
            "num_days": len(temps_clean),
            "error": None,
        }
    except Exception as exc:
        return {
            "city": loc.name,
            "lat": loc.lat,
            "lon": loc.lon,
            "tz": loc.tz,
            "dates": [],
            "temps_max_c": [],
            "num_days": 0,
            "error": str(exc),
        }


async def fetch_all_historical(
    locations: list[SavedLocation],
    concurrent: int = CONCURRENT_FETCHES,
) -> list[dict[str, Any]]:
    """Fetch historical data for all cities with a concurrency limit."""
    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(concurrent)

    async def _fetch_one(loc: SavedLocation, idx: int, total: int) -> None:
        async with semaphore:
            async with httpx.AsyncClient() as client:
                result = await fetch_historical(client, loc)
                status = _green("✓") if result["error"] is None else _red("✗")
                bar = _progress_bar(idx + 1, total)
                print(f"\r  {bar} [{idx+1:2d}/{total}] {status} {_truncate(loc.name, 20)}", end="", flush=True)
                results.append(result)

    total = len(locations)
    print(f"\n  {_cyan('Fetching historical data')} ({total} cities, {BACKTEST_DAYS}d each)...")
    tasks = [_fetch_one(loc, i, total) for i, loc in enumerate(locations)]
    await asyncio.gather(*tasks)
    print()  # newline after progress

    # Sort by original order
    name_order = {loc.name: i for i, loc in enumerate(locations)}
    results.sort(key=lambda r: name_order.get(r["city"], 999))

    ok = sum(1 for r in results if r["error"] is None)
    fail = total - ok
    print(f"  {_green(f'✓ {ok} fetched')}" + (f"  {_red(f'✗ {fail} failed')}" if fail else ""))
    return results


# =============================================================================
# BMA Ensemble
# =============================================================================

async def run_bma_for_all(
    analyzer: WeatherAnalyzer,
    locations: list[SavedLocation],
    concurrent: int = CONCURRENT_BMA,
) -> list[dict[str, Any]]:
    """Run BMA ensemble for all cities with a concurrency limit."""
    results: list[dict[str, Any]] = []
    semaphore = asyncio.Semaphore(concurrent)

    async def _analyze_one(loc: SavedLocation, idx: int, total: int) -> None:
        async with semaphore:
            try:
                analysis = await analyzer.analyze(loc, lead_days=1)
                if analysis.error or analysis.ensemble is None:
                    results.append({
                        "city": loc.name,
                        "lat": loc.lat,
                        "lon": loc.lon,
                        "tz": loc.tz,
                        "uhi_adjustment": loc.uhi_adjustment,
                        "error": analysis.error or "no ensemble",
                        "mean_c": 0,
                        "p5_c": 0,
                        "p95_c": 0,
                        "confidence": 0,
                        "model_count": 0,
                        "suggested_spill": 0,
                        "individual_models": {},
                    })
                else:
                    ens = analysis.ensemble
                    mean_c = WeatherAnalyzer.f_to_c(ens.mean_temp_f)
                    p5_c = WeatherAnalyzer.f_to_c(ens.p05_temp_f)
                    p95_c = WeatherAnalyzer.f_to_c(ens.p95_temp_f)
                    adj_mean = mean_c + loc.uhi_adjustment
                    suggested_spill = int(round(adj_mean))

                    results.append({
                        "city": loc.name,
                        "lat": loc.lat,
                        "lon": loc.lon,
                        "tz": loc.tz,
                        "uhi_adjustment": loc.uhi_adjustment,
                        "error": None,
                        "mean_c": round(mean_c, 1),
                        "p5_c": round(p5_c, 1),
                        "p95_c": round(p95_c, 1),
                        "range_c": round(p95_c - p5_c, 2),
                        "confidence": round(ens.confidence, 3),
                        "model_count": ens.model_count,
                        "suggested_spill": suggested_spill,
                        "std_c": round(WeatherAnalyzer.f_to_c(ens.std_temp_f), 2),
                        "individual_models": {
                            m: round(WeatherAnalyzer.f_to_c(t), 1)
                            for m, t in (ens.individual_models or {}).items()
                        },
                    })

                bar = _progress_bar(idx + 1, total)
                status = _green("✓") if results[-1]["error"] is None else _red("✗")
                print(f"\r  {bar} [{idx+1:2d}/{total}] {status} {_truncate(loc.name, 20)}", end="", flush=True)

            except Exception as exc:
                results.append({
                    "city": loc.name,
                    "lat": loc.lat,
                    "lon": loc.lon,
                    "tz": loc.tz,
                    "uhi_adjustment": loc.uhi_adjustment,
                    "error": str(exc),
                    "mean_c": 0,
                    "p5_c": 0,
                    "p95_c": 0,
                    "confidence": 0,
                    "model_count": 0,
                    "suggested_spill": 0,
                    "individual_models": {},
                })
                bar = _progress_bar(idx + 1, total)
                print(f"\r  {bar} [{idx+1:2d}/{total}] {_red('✗')} {_truncate(loc.name, 20)}", end="", flush=True)

    total = len(locations)
    print(f"\n  {_cyan('Running BMA ensemble')} ({total} cities, lead_days=1)...")
    tasks = [_analyze_one(loc, i, total) for i, loc in enumerate(locations)]
    await asyncio.gather(*tasks)
    print()  # newline

    # Sort by original order
    name_order = {loc.name: i for i, loc in enumerate(locations)}
    results.sort(key=lambda r: name_order.get(r["city"], 999))

    ok = sum(1 for r in results if r["error"] is None)
    fail = total - ok
    print(f"  {_green(f'✓ {ok} analyzed')}" + (f"  {_red(f'✗ {fail} failed')}" if fail else ""))
    return results


# =============================================================================
# Comparison / Calibration Logic
# =============================================================================

def compute_comparison(
    historical: dict[str, Any],
    bma_result: dict[str, Any],
) -> dict[str, Any]:
    """Compare BMA prediction against historical actual data.

    Returns a dict with calibration and stability metrics.
    """
    temps = historical.get("temps_max_c", [])
    dates = historical.get("dates", [])

    if not temps or bma_result.get("error"):
        return {
            "city": historical["city"],
            "error": "no historical data" if not temps else bma_result.get("error"),
            "num_days": len(temps),
            "within_p5p95": 0,
            "within_p5p95_pct": 0,
            "within_2c": 0,
            "within_2c_pct": 0,
            "actual_mean": 0,
            "actual_std": 0,
            "actual_min": 0,
            "actual_max": 0,
            "spill_hits_30d": 0,
            "spill_hits_30d_pct": 0,
            "spill_hits_7d": 0,
            "spill_hits_7d_pct": 0,
            "last_7d": [],
            "verdict": "N/A",
        }

    p5_c = bma_result["p5_c"]
    p95_c = bma_result["p95_c"]
    mean_c = bma_result["mean_c"]
    suggested_spill = bma_result["suggested_spill"]

    # Calibration: how many days within P5-P95?
    within_p5p95 = sum(1 for t in temps if p5_c <= t <= p95_c)

    # Stability: how many days within ±2°C of BMA mean?
    within_2c = sum(1 for t in temps if abs(t - mean_c) <= 2.0)

    # Spill hit rate: actual max >= suggested_spill
    spill_hits_30d = sum(1 for t in temps if t >= suggested_spill)

    # Last 7 days for spill backtest
    last_7d_temps = temps[-7:] if len(temps) >= 7 else temps
    last_7d_dates = dates[-7:] if len(dates) >= 7 else dates
    spill_hits_7d = sum(1 for t in last_7d_temps if t >= suggested_spill)

    # Daily hit/miss for last 7 days
    last_7d_detail: list[dict[str, Any]] = []
    for i, (d, t) in enumerate(zip(last_7d_dates, last_7d_temps)):
        last_7d_detail.append({
            "date": d,
            "actual": round(t, 1),
            "hit": t >= suggested_spill,
        })

    actual_mean = sum(temps) / len(temps) if temps else 0
    actual_std = math.sqrt(sum((t - actual_mean) ** 2 for t in temps) / len(temps)) if temps else 0

    # Verdict
    within_pct = within_p5p95 / len(temps) if temps else 0
    if within_pct >= 0.80:
        verdict = "✅"
    elif within_pct >= 0.60:
        verdict = "⚠️"
    else:
        verdict = "❌"

    return {
        "city": historical["city"],
        "error": None,
        "num_days": len(temps),
        "within_p5p95": within_p5p95,
        "within_p5p95_pct": round(within_pct, 3),
        "within_2c": within_2c,
        "within_2c_pct": round(within_2c / len(temps), 3) if temps else 0,
        "actual_mean": round(actual_mean, 1),
        "actual_std": round(actual_std, 2),
        "actual_min": round(min(temps), 1) if temps else 0,
        "actual_max": round(max(temps), 1) if temps else 0,
        "spill_hits_30d": spill_hits_30d,
        "spill_hits_30d_pct": round(spill_hits_30d / len(temps), 3) if temps else 0,
        "spill_hits_7d": spill_hits_7d,
        "spill_hits_7d_pct": round(spill_hits_7d / len(last_7d_temps), 3) if last_7d_temps else 0,
        "last_7d": last_7d_detail,
        "verdict": verdict,
    }


# =============================================================================
# Output Formatting
# =============================================================================

def _progress_bar(current: int, total: int, width: int = 30) -> str:
    """Simple ASCII progress bar."""
    filled = int(width * current / total)
    bar = "█" * filled + "░" * (width - filled)
    return f"|{bar}|"


def print_backtest_table(
    comparisons: list[dict[str, Any]],
    bma_results: list[dict[str, Any]],
) -> None:
    """Print the main calibration backtest table."""
    # Build lookup
    bma_by_city = {r["city"]: r for r in bma_results}

    valid = [c for c in comparisons if c.get("error") is None and c["num_days"] > 0]
    errors = [c for c in comparisons if c.get("error") is not None or c["num_days"] == 0]

    print()
    print(_white("╔" + "═" * 77 + "╗"))
    print(_white("║") + _white("                    VÆRMONITOR BACKTEST — SISTE 30 DAGER                    ").center(75) + _white("║"))
    print(_white("╚" + "═" * 77 + "╝"))
    print()

    # Header
    header = (
        f"  {'By':<20s} │ {'BMA Nå':>6s} │ {'P5-P95':>11s} │ {'Konf':>5s} │ "
        f"{'30d Range':>14s} │ {'Innenfor':>8s} │ {'V/T':>3s}"
    )
    sep = (
        f"  {'─'*20}─┼─{'─'*6}─┼─{'─'*11}─┼─{'─'*5}─┼─"
        f"{'─'*14}─┼─{'─'*8}─┼─{'─'*3}"
    )
    print(_cyan(header))
    print(_cyan(sep))

    for comp in valid:
        city = comp["city"]
        bma = bma_by_city.get(city, {})
        mean_c = bma.get("mean_c", 0)
        p5_c = bma.get("p5_c", 0)
        p95_c = bma.get("p95_c", 0)
        conf = bma.get("confidence", 0)
        conf_str = _conf_pct_str(conf)
        emoji = _conf_emoji(conf)

        actual_min = comp["actual_min"]
        actual_max = comp["actual_max"]
        range_str = f"{actual_min:.1f}-{actual_max:.1f}°C"
        within_str = f"{comp['within_p5p95']}/{comp['num_days']}"
        verdict = comp["verdict"]

        print(
            f"  {city:<20s} │ {mean_c:>5.1f}°C │ "
            f"{p5_c:>4.1f}-{p95_c:<4.1f} │ {emoji}{conf_str} │ "
            f"{range_str:>14s} │ {within_str:>8s} │ {verdict}"
        )

    if errors:
        print()
        for err in errors:
            err_city = err["city"]
            err_msg = err.get("error", "unknown")
            print(f"  {_red(f'{err_city:<20s} │ --- ERROR: {err_msg}')}")

    print()

    # Summary section
    print(_white("SAMMENDRAG:"))
    high_conf = [c for c in valid if bma_by_city.get(c["city"], {}).get("confidence", 0) > 0.80]
    mid_conf = [c for c in valid if 0.70 <= bma_by_city.get(c["city"], {}).get("confidence", 0) <= 0.80]
    low_conf = [c for c in valid if bma_by_city.get(c["city"], {}).get("confidence", 0) < 0.70]

    def _avg_hit_rate(items: list[dict[str, Any]]) -> float:
        if not items:
            return 0.0
        return sum(c["within_p5p95_pct"] for c in items) / len(items) * 100

    print(f"   🟢 Høy konfidens (>80%):  {len(high_conf):2d} byer — "
          f"treffrate: {_avg_hit_rate(high_conf):.0f}% (innenfor P5-P95)")
    print(f"   🟠 Medium konfidens (70-80%): {len(mid_conf):2d} byer — "
          f"treffrate: {_avg_hit_rate(mid_conf):.0f}%")
    print(f"   🔴 Lav konfidens (<70%):  {len(low_conf):2d} byer — "
          f"treffrate: {_avg_hit_rate(low_conf):.0f}%")
    print()
    total_hit_pct = sum(c["within_p5p95_pct"] for c in valid) / len(valid) * 100 if valid else 0
    print(f"   Totalt: {len(valid)} byer — {total_hit_pct:.0f}% innenfor P5-P95 siste {BACKTEST_DAYS} dager")
    print()


def print_spill_backtest_table(
    comparisons: list[dict[str, Any]],
    bma_results: list[dict[str, Any]],
) -> None:
    """Print the suggested bet backtest table."""
    bma_by_city = {r["city"]: r for r in bma_results}
    valid = [c for c in comparisons if c.get("error") is None and c["num_days"] > 0]

    print(_white("🎯 FORESLÅTT SPILL — BACKTEST (spill = BMA avrundet)"))
    print()

    header = (
        f"   {'By':<20s} │ {'Spill':>5s} │ {'Siste 7d treff':>15s} │ "
        f"{'Siste 30d treff':>16s} │ {'Edge?':>4s}"
    )
    sep = (
        f"   {'─'*20}─┼─{'─'*5}─┼─{'─'*15}─┼─"
        f"{'─'*16}─┼─{'─'*4}"
    )
    print(_cyan(header))
    print(_cyan(sep))

    for comp in valid:
        city = comp["city"]
        bma = bma_by_city.get(city, {})
        spill = bma.get("suggested_spill", 0)

        hits_7 = comp["spill_hits_7d"]
        total_7 = len(comp["last_7d"])
        hits_30 = comp["spill_hits_30d"]
        total_30 = comp["num_days"]

        pct_7 = comp["spill_hits_7d_pct"] * 100
        pct_30 = comp["spill_hits_30d_pct"] * 100

        s7 = f"{hits_7}/{total_7} ({pct_7:.0f}%)"
        s30 = f"{hits_30}/{total_30} ({pct_30:.0f}%)"

        # Edge check: spill hit rate > 50% means positive edge
        if pct_30 >= 75:
            edge = _green("✅")
        elif pct_30 >= 60:
            edge = _yellow("⚠️")
        else:
            edge = _red("❌")

        print(f"   {city:<20s} │ {spill:>4d}°C │ {s7:>15s} │ {s30:>16s} │ {edge:>4s}")

    print()
    total_avg = sum(c["spill_hits_30d_pct"] for c in valid) / len(valid) * 100 if valid else 0
    print(f"   Gjennomsnittlig spill-treffrate (30d): {total_avg:.0f}%")
    print()


def print_weekly_detail(
    comparisons: list[dict[str, Any]],
    bma_results: list[dict[str, Any]],
    top_n: int = 10,
) -> None:
    """Print the last 7 days detail table for top-N by confidence."""
    bma_by_city = {r["city"]: r for r in bma_results}

    # Sort by confidence descending, take top N
    valid = [c for c in comparisons if c.get("error") is None and c["num_days"] > 0]
    valid.sort(key=lambda c: bma_by_city.get(c["city"], {}).get("confidence", 0), reverse=True)
    top = valid[:top_n]

    if not top:
        return

    print(_white("🎯 ANBEFALT SPILL vs FAKTISK (siste 7 dager)"))
    print()

    # We need at most 7 days. Get the day names from the first valid city.
    day_names = ["Man", "Tir", "Ons", "Tor", "Fre", "Lør", "Søn"]

    header = (
        f"   {'By':<20s} │ {'Spill':>5s} │ {'Man':>5s} │ {'Tir':>5s} │ "
        f"{'Ons':>5s} │ {'Tor':>5s} │ {'Fre':>5s} │ {'Lør':>5s} │ {'Søn':>5s} │ {'V/T':>4s}"
    )
    sep = (
        f"   {'─'*20}─┼─{'─'*5}─┼─{'─'*5}─┼─{'─'*5}─┼─"
        f"{'─'*5}─┼─{'─'*5}─┼─{'─'*5}─┼─{'─'*5}─┼─{'─'*5}─┼─{'─'*4}"
    )
    print(_cyan(header))
    print(_cyan(sep))

    for comp in top:
        city = comp["city"]
        bma = bma_by_city.get(city, {})
        spill = bma.get("suggested_spill", 0)

        last_7d = comp["last_7d"]
        hits = sum(1 for d in last_7d if d["hit"])

        # Format daily cells
        day_cells: list[str] = []
        for day in last_7d:
            actual = day["actual"]
            hit = day["hit"]
            marker = _green("✅") if hit else _red("❌")
            day_cells.append(f"{actual:.1f}{marker}")

        # Pad to 7 columns
        while len(day_cells) < 7:
            day_cells.append("  —  ")

        vt_str = f"{hits}/{len(last_7d)}"
        print(
            f"   {city:<20s} │ {spill:>4d}°C │ "
            f"{day_cells[0]:>8s}│{day_cells[1]:>8s}│{day_cells[2]:>8s}│"
            f"{day_cells[3]:>8s}│{day_cells[4]:>8s}│{day_cells[5]:>8s}│{day_cells[6]:>8s}│ {vt_str:>4s}"
        )

    print()


# =============================================================================
# Results Export
# =============================================================================

def save_results(
    comparisons: list[dict[str, Any]],
    bma_results: list[dict[str, Any]],
    historical: list[dict[str, Any]],
    elapsed: float,
    path: Path = OUTPUT_FILE,
) -> None:
    """Save full backtest results to JSON."""
    output = {
        "metadata": {
            "backtest_days": BACKTEST_DAYS,
            "run_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": round(elapsed, 1),
            "num_cities_total": len(comparisons),
            "num_cities_valid": sum(1 for c in comparisons if c.get("error") is None and c["num_days"] > 0),
        },
        "bma_results": bma_results,
        "historical": [
            {
                "city": h["city"],
                "num_days": h["num_days"],
                "dates": h.get("dates", []),
                "temps_max_c": h.get("temps_max_c", []),
                "error": h.get("error"),
            }
            for h in historical
        ],
        "comparisons": comparisons,
        "summary": _compute_summary(comparisons, bma_results),
    }

    path.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  {_green('✓')} Results saved to {_cyan(str(path))}")


def _compute_summary(
    comparisons: list[dict[str, Any]],
    bma_results: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute aggregate summary statistics."""
    bma_by_city = {r["city"]: r for r in bma_results}
    valid = [c for c in comparisons if c.get("error") is None and c["num_days"] > 0]

    def _avg(items, key):
        return sum(c[key] for c in items) / len(items) if items else 0

    high = [c for c in valid if bma_by_city.get(c["city"], {}).get("confidence", 0) > 0.80]
    mid = [c for c in valid if 0.70 <= bma_by_city.get(c["city"], {}).get("confidence", 0) <= 0.80]
    low = [c for c in valid if bma_by_city.get(c["city"], {}).get("confidence", 0) < 0.70]

    return {
        "total_cities": len(comparisons),
        "valid_cities": len(valid),
        "overall_p5p95_hit_rate": round(_avg(valid, "within_p5p95_pct") * 100, 1),
        "overall_spill_hit_rate_30d": round(_avg(valid, "spill_hits_30d_pct") * 100, 1),
        "overall_spill_hit_rate_7d": round(_avg(valid, "spill_hits_7d_pct") * 100, 1),
        "by_confidence_tier": {
            "high_conf_gt_80": {
                "count": len(high),
                "p5p95_hit_rate": round(_avg(high, "within_p5p95_pct") * 100, 1),
                "spill_hit_rate_30d": round(_avg(high, "spill_hits_30d_pct") * 100, 1),
            },
            "mid_conf_70_80": {
                "count": len(mid),
                "p5p95_hit_rate": round(_avg(mid, "within_p5p95_pct") * 100, 1),
                "spill_hit_rate_30d": round(_avg(mid, "spill_hits_30d_pct") * 100, 1),
            },
            "low_conf_lt_70": {
                "count": len(low),
                "p5p95_hit_rate": round(_avg(low, "within_p5p95_pct") * 100, 1),
                "spill_hit_rate_30d": round(_avg(low, "spill_hits_30d_pct") * 100, 1),
            },
        },
    }


# =============================================================================
# Main
# =============================================================================

async def main() -> None:
    """Run the full backtest pipeline."""
    t_start = time.perf_counter()

    print()
    print(_white("╔" + "═" * 60 + "╗"))
    print(_white("║") + _white("     VÆRMONITOR BACKTEST — 30-DAY HISTORICAL VALIDATION     ").center(58) + _white("║"))
    print(_white("╚" + "═" * 60 + "╝"))
    print()
    print(f"  Date: {_cyan(date.today().isoformat())}")
    print(f"  Backtest window: {_cyan(f'{BACKTEST_DAYS} days')}")
    print()

    # ---- Step 1: Load cities ----
    print(_white("[1/4]") + f" Loading default cities...")
    locations = load_default_cities()
    print(f"  {_green('✓')} Loaded {len(locations)} cities")
    print()

    # ---- Step 2: Fetch historical data ----
    print(_white("[2/4]") + f" Fetching historical data...")
    historical = await fetch_all_historical(locations)
    print()

    # ---- Step 3: Initialize WeatherAnalyzer & run BMA ----
    print(_white("[3/4]") + f" Initializing WeatherAnalyzer & running BMA ensemble...")
    analyzer = WeatherAnalyzer()
    await analyzer.initialize()
    bma_results = await run_bma_for_all(analyzer, locations)
    await analyzer.close()
    print()

    # ---- Step 4: Compute comparisons ----
    print(_white("[4/4]") + f" Computing calibration stats...")
    hist_by_city = {h["city"]: h for h in historical}
    bma_by_city = {r["city"]: r for r in bma_results}

    comparisons: list[dict[str, Any]] = []
    for loc in locations:
        hist = hist_by_city.get(loc.name, {})
        bma = bma_by_city.get(loc.name, {})
        comp = compute_comparison(hist, bma)
        comparisons.append(comp)

    print(f"  {_green('✓')} Computed stats for {len(comparisons)} cities")
    print()

    # ---- Print tables ----
    print_backtest_table(comparisons, bma_results)
    print_spill_backtest_table(comparisons, bma_results)
    print_weekly_detail(comparisons, bma_results, top_n=15)

    # ---- Save results ----
    elapsed = time.perf_counter() - t_start
    print(f"  ⏱️ Total elapsed: {elapsed:.1f}s")
    print()
    save_results(comparisons, bma_results, historical, elapsed)
    print()
    print(_green("═══ BACKTEST COMPLETE ═══"))


if __name__ == "__main__":
    asyncio.run(main())
