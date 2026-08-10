#!/usr/bin/env python3
"""
Generate a human-readable quality report from _model_quality_log.json.

Outputs to both stdout and _quality_report.md. Also generates HTML dashboard.

Usage:
    python _generate_quality_report.py
    python _generate_quality_report.py --html
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

LOG_FILE = Path(_SCRIPT_DIR) / "_model_quality_log.json"
REPORT_FILE = Path(_SCRIPT_DIR) / "_quality_report.md"
HTML_REPORT_FILE = Path(_SCRIPT_DIR) / "_quality_report.html"


def _load_log() -> dict:
    """Load existing quality log or return empty structure."""
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {"runs": []}


def _generate_report() -> str:
    """Generate the full report string."""
    log_data = _load_log()
    runs = log_data.get("runs", [])
    cum = log_data.get("cumulative", {})

    lines: list[str] = []

    lines.append("═" * 60)
    lines.append("     MODELLKVALITET — KUMULATIV RAPPORT (3 STRATEGIER)")
    lines.append("═" * 60)
    lines.append("")

    generated = date.today().isoformat()
    lines.append(f"Generert: {generated}")
    lines.append("")

    if not runs:
        lines.append("Ingen data i loggen. Kjør `python _model_quality_tracker.py --mode daily_bma` først.")
        lines.append("")
        return "\n".join(lines)

    # Aggregate per-strategy stats
    sigma_wins = sum(r.get("summary", {}).get("sigma_wins", 0) for r in runs)
    sigma_losses = sum(r.get("summary", {}).get("sigma_losses", 0) for r in runs)
    p5_wins = sum(r.get("summary", {}).get("p5_wins", 0) for r in runs)
    p5_losses = sum(r.get("summary", {}).get("p5_losses", 0) for r in runs)
    mean_wins = sum(r.get("summary", {}).get("mean_wins", 0) for r in runs)
    mean_losses = sum(r.get("summary", {}).get("mean_losses", 0) for r in runs)

    sigma_total = sigma_wins + sigma_losses
    p5_total = p5_wins + p5_losses
    mean_total = mean_wins + mean_losses

    total_cities = sum(len(r.get("predictions", {})) for r in runs)

    lines.append(f"Dager kjørt: {len(runs)}")
    lines.append(f"Totalt by-prediksjoner: {total_cities}")
    lines.append("")

    lines.append("📊 PER-STRATEGI RESULTATER:")
    lines.append(f"   🎯 Sigma (μ−kσ):  V:{sigma_wins} T:{sigma_losses}  "
                 f"({round(sigma_wins/max(1,sigma_total)*100,1)}%)")
    lines.append(f"   🛡️ P5-basert:      V:{p5_wins} T:{p5_losses}  "
                 f"({round(p5_wins/max(1,p5_total)*100,1)}%)")
    lines.append(f"   📊 Mean-basert:    V:{mean_wins} T:{mean_losses}  "
                 f"({round(mean_wins/max(1,mean_total)*100,1)}%)")
    lines.append("")

    # Best strategy
    rates = {
        "Sigma (μ−kσ)": round(sigma_wins / max(1, sigma_total) * 100, 1),
        "P5-basert": round(p5_wins / max(1, p5_total) * 100, 1),
        "Mean-basert": round(mean_wins / max(1, mean_total) * 100, 1),
    }
    best = max(rates, key=lambda k: rates[k])
    lines.append(f"🏆 BESTE STRATEGI: {best} ({rates[best]}%)")
    lines.append("")

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

    # Cities with strategy divergence
    lines.append("🔍 BYER MED STRATEGI-FORSKJELLER (>20pp):")
    found = False
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
        if max_r - min_r > 20:
            found = True
            best_s = "Sigma" if sigma_r == max_r else ("P5" if p5_r == max_r else "Mean")
            lines.append(f"   {city:<30s} sigma={sigma_r:.0f}% p5={p5_r:.0f}% mean={mean_r:.0f}% → {best_s} best")
    if not found:
        lines.append("   Ingen signifikante forskjeller funnet.")
    lines.append("")

    # Flip recommendations
    flip_count = 0
    flip_wins = 0
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            rec = pdata.get("recommendation", "")
            if rec and "SELG" in str(rec):
                flip_count += 1
                # Check if P5 would have won (i.e., flip would be profitable)
                p5_result = pdata.get("strategies", {}).get("p5", {}).get("result", "")
                if p5_result == "WIN":
                    flip_wins += 1
    if flip_count > 0:
        lines.append(f"🔄 FLIP-ANBEFALINGER: {flip_count} totalt "
                     f"({flip_wins} ville vært profitable via P5)")
        lines.append("")

    # Per-run table
    lines.append("─" * 60)
    lines.append("📋 PER DAG:")
    lines.append("─" * 60)
    for run in runs:
        rd = run.get("run_date", "?")
        s = run.get("summary", {})
        sw = s.get("sigma_wins", 0)
        sl = s.get("sigma_losses", 0)
        pw = s.get("p5_wins", 0)
        pl = s.get("p5_losses", 0)
        mw = s.get("mean_wins", 0)
        ml = s.get("mean_losses", 0)
        npred = len(run.get("predictions", {}))
        if sw + sl > 0:
            sr = round(sw / (sw + sl) * 100, 1)
            lines.append(f"   {rd}  │  {npred:3d} byer  │  sigma={sw}/{sl} ({sr}%)  "
                         f"p5={pw}/{pl}  mean={mw}/{ml}")
        else:
            lines.append(f"   {rd}  │  {npred:3d} byer  │  (ingen resultater)")

    lines.append("")
    lines.append("═" * 60)
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# HTML Report Generator (Dark Theme Dashboard — 3-Strategy Edition)
# =============================================================================

def _build_top5_rows_html(predictions: dict, top5_cities: list[str]) -> str:
    """Build HTML table rows for top 5 cities with all 3 strategy results."""
    rows = ""
    for i, city in enumerate(top5_cities):
        pdata = predictions.get(city, {})
        if not pdata:
            continue
        bma_mean = pdata.get("bma_mean", "—")
        bma_std = pdata.get("bma_std", "—")
        conf = pdata.get("confidence", 0)
        model_ct = pdata.get("models", 0)
        strategies = pdata.get("strategies", {})

        sigma = strategies.get("sigma", {})
        p5s = strategies.get("p5", {})
        means = strategies.get("mean", {})

        sigma_spill = sigma.get("spill", "?")
        sigma_wp = sigma.get("win_prob", 0)
        sigma_k = sigma.get("k", 0)
        sigma_result = sigma.get("result", "")
        sigma_actual = sigma.get("actual_peak")

        p5_spill = p5s.get("spill", "?")
        p5_result = p5s.get("result", "")

        mean_spill = means.get("spill", "?")
        mean_result = means.get("result", "")

        rec = pdata.get("recommendation", "—") or "—"

        # Confidence color
        if conf >= 0.8:
            conf_icon = "🟢"
        elif conf >= 0.7:
            conf_icon = "🟠"
        else:
            conf_icon = "🔴"

        def _res_badge(r):
            if r == "WIN":
                return '<span class="badge-win">✅ WIN</span>'
            elif r == "LOSS":
                return '<span class="badge-loss">❌ LOSS</span>'
            return '<span style="color:#8b949e;">⏳</span>'

        sigma_badge = _res_badge(sigma_result)
        p5_badge = _res_badge(p5_result)
        mean_badge = _res_badge(mean_result)

        bma_str = f"{bma_mean:.1f}°C" if isinstance(bma_mean, (int, float)) else str(bma_mean)
        std_str = f"{bma_std:.1f}" if isinstance(bma_std, (int, float)) else str(bma_std)

        actual_str = f"{sigma_actual:.1f}°C" if isinstance(sigma_actual, (int, float)) else "—"

        # Recommendation styling
        rec_class = ""
        if rec and "HOLD" in str(rec):
            rec_class = 'style="color:#1b5e20;"'
        elif rec and "SELG" in str(rec):
            rec_class = 'style="color:#b71c1c;"'
        elif rec and "AVVENT" in str(rec):
            rec_class = 'style="color:#d2991d;"'

        sigma_cell = (
            f'BUY <strong>{sigma_spill}°C</strong> '
            f'<span style="font-size:0.75rem;color:#8b949e;">'
            f'(k={sigma_k}, {sigma_wp*100:.0f}%)</span>'
        )

        rows += f"""
            <tr>
                <td>{i+1}</td>
                <td><strong>{city}</strong></td>
                <td>{bma_str} <span style="color:#8b949e;font-size:0.75rem;">σ={std_str}</span></td>
                <td>{sigma_cell}</td>
                <td>BUY {p5_spill}°C</td>
                <td>BUY {mean_spill}°C</td>
                <td>{conf_icon} {(conf*100):.0f}%</td>
                <td>{model_ct}/8</td>
                <td>{actual_str}</td>
                <td>{sigma_badge}</td>
                <td>{p5_badge}</td>
                <td>{mean_badge}</td>
                <td {rec_class}>{rec}</td>
            </tr>"""
    return rows


def _build_strategy_comparison_section(predictions: dict) -> str:
    """Build a per-city strategy comparison table showing all 3 strategies."""
    rows = ""
    for city, pdata in sorted(predictions.items()):
        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        p5s = strategies.get("p5", {})
        means = strategies.get("mean", {})

        sigma_result = sigma.get("result", "")
        p5_result = p5s.get("result", "")
        mean_result = means.get("result", "")

        def _win_icon(r):
            return "✅" if r == "WIN" else ("❌" if r == "LOSS" else "⏳")

        # Find best strategy for this city
        results_map = {
            "sigma": sigma_result,
            "p5": p5_result,
            "mean": mean_result,
        }
        best_strat = None
        if "WIN" in results_map.values():
            # Prefer sigma if it wins, else whichever wins
            if sigma_result == "WIN":
                best_strat = "sigma"
            elif p5_result == "WIN":
                best_strat = "p5"
            elif mean_result == "WIN":
                best_strat = "mean"

        sigma_hl = 'style="color:#3fb950;font-weight:600;"' if best_strat == "sigma" else ""
        p5_hl = 'style="color:#3fb950;font-weight:600;"' if best_strat == "p5" else ""
        mean_hl = 'style="color:#3fb950;font-weight:600;"' if best_strat == "mean" else ""

        rows += f"""
            <tr>
                <td>{city}</td>
                <td {sigma_hl}>BUY {sigma.get('spill','?')}°C {_win_icon(sigma_result)}</td>
                <td {p5_hl}>BUY {p5s.get('spill','?')}°C {_win_icon(p5_result)}</td>
                <td {mean_hl}>BUY {means.get('spill','?')}°C {_win_icon(mean_result)}</td>
            </tr>"""
    if not rows:
        return ""
    return f"""
   <div class="section">
     <h2>📊 Per-City Strategy Comparison</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Green = best performing strategy for each city. Click headers to sort.
     </p>
     <div style="max-height: 600px; overflow-y: auto;">
     <table id="strategyTable" class="sortable">
       <thead><tr>
         <th onclick="sortTable(0)">City ↕</th>
         <th onclick="sortTable(1)">🎯 Sigma (μ−kσ) ↕</th>
         <th onclick="sortTable(2)">🛡️ P5-Basert ↕</th>
         <th onclick="sortTable(3)">📊 Mean-Basert ↕</th>
       </tr></thead>
       <tbody>{rows}
       </tbody>
     </table>
     </div>
   </div>"""


def _build_strategy_summary_cards(
    sigma_wins: int, sigma_losses: int,
    p5_wins: int, p5_losses: int,
    mean_wins: int, mean_losses: int,
) -> str:
    """Build summary card grid for per-strategy performance."""
    sigma_total = sigma_wins + sigma_losses
    p5_total = p5_wins + p5_losses
    mean_total = mean_wins + mean_losses

    sigma_rate = round(sigma_wins / max(1, sigma_total) * 100, 1)
    p5_rate = round(p5_wins / max(1, p5_total) * 100, 1)
    mean_rate = round(mean_wins / max(1, mean_total) * 100, 1)

    def _rate_color(r):
        if r >= 60:
            return "#3fb950"
        elif r >= 50:
            return "#d2991d"
        return "#f85149"

    # Determine best strategy
    rates = {"sigma": sigma_rate, "p5": p5_rate, "mean": mean_rate}
    best_key = max(rates, key=lambda k: rates[k])
    best_names = {"sigma": "Sigma (μ−kσ)", "p5": "P5-Basert", "mean": "Mean-Basert"}

    return f"""
   <div class="card-grid">
     <div class="card" style="{'border: 2px solid #3fb950;' if best_key == 'sigma' else ''}">
       <div class="value" style="color: {_rate_color(sigma_rate)};">{sigma_rate}%</div>
       <div class="label">🎯 Sigma (μ−kσ)<br/>{sigma_wins}W / {sigma_losses}L</div>
     </div>
     <div class="card" style="{'border: 2px solid #3fb950;' if best_key == 'p5' else ''}">
       <div class="value" style="color: {_rate_color(p5_rate)};">{p5_rate}%</div>
       <div class="label">🛡️ P5-Basert<br/>{p5_wins}W / {p5_losses}L</div>
     </div>
     <div class="card" style="{'border: 2px solid #3fb950;' if best_key == 'mean' else ''}">
       <div class="value" style="color: {_rate_color(mean_rate)};">{mean_rate}%</div>
       <div class="label">📊 Mean-Basert<br/>{mean_wins}W / {mean_losses}L</div>
     </div>
     <div class="card">
       <div class="value" style="color: var(--purple);">🏆 {best_names.get(best_key, '—')}</div>
       <div class="label">Best Strategy</div>
     </div>
   </div>"""


def _build_flip_recommendations_section(predictions: dict, top5_cities: list[str]) -> str:
    """Build section showing flip recommendations for cities that need them."""
    rows = ""
    for city in top5_cities:
        pdata = predictions.get(city, {})
        if not pdata:
            continue
        rec = pdata.get("recommendation", "")
        if not rec or "HOLD" in str(rec):
            continue

        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        actual = sigma.get("actual_peak", "—")
        spill = sigma.get("spill", "?")
        sigma_result = sigma.get("result", "")
        p5_result = strategies.get("p5", {}).get("result", "")

        # Did flip make sense? If P5 would have won, the temperature didn't reach the very conservative level
        flip_profitable = p5_result == "WIN"

        rec_class = 'style="color:#f85149;"' if "SELG" in str(rec) else 'style="color:#d2991d;"'
        profit_icon = "💰" if flip_profitable else "—"
        actual_str = f"{actual:.1f}°C" if isinstance(actual, (int, float)) else str(actual)

        rows += f"""
            <tr>
                <td><strong>{city}</strong></td>
                <td>BUY {spill}°C</td>
                <td>{actual_str}</td>
                <td {rec_class}>{rec}</td>
                <td>{profit_icon}</td>
            </tr>"""

    if not rows:
        return ""

    return f"""
   <div class="section">
     <h2>🔄 Flip Recommendations</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Cities where the BUY position lost — recommendation is to SELL and go SHORT.
       💰 = flip would have been profitable (P5 strategy would have won).
     </p>
     <table>
       <thead><tr><th>City</th><th>Position</th><th>Actual Peak</th><th>Recommendation</th><th>Flip Profitable?</th></tr></thead>
       <tbody>{rows}
       </tbody>
     </table>
   </div>"""


def _build_city_divergence_section(predictions: dict) -> str:
    """Highlight cities where one strategy significantly outperforms others."""
    # Aggregate city stats from predictions
    city_strats: dict[str, dict] = {}
    for city, pdata in predictions.items():
        strategies = pdata.get("strategies", {})
        city_strats[city] = {}
        for sn in ("sigma", "p5", "mean"):
            s = strategies.get(sn, {})
            city_strats[city][sn] = {
                "result": s.get("result", ""),
                "spill": s.get("spill", "?"),
            }

    rows = ""
    for city, stats in sorted(city_strats.items()):
        results = {sn: stats[sn]["result"] for sn in ("sigma", "p5", "mean")}
        # Only show if there's divergence (one wins, another loses)
        has_win = "WIN" in results.values()
        has_loss = "LOSS" in results.values()
        if not (has_win and has_loss):
            continue

        def _icon(r):
            return "✅" if r == "WIN" else ("❌" if r == "LOSS" else "⏳")

        sigma_icon = _icon(results.get("sigma", ""))
        p5_icon = _icon(results.get("p5", ""))
        mean_icon = _icon(results.get("mean", ""))

        rows += f"""
            <tr>
                <td><strong>{city}</strong></td>
                <td>{sigma_icon} BUY {stats['sigma']['spill']}°C</td>
                <td>{p5_icon} BUY {stats['p5']['spill']}°C</td>
                <td>{mean_icon} BUY {stats['mean']['spill']}°C</td>
            </tr>"""

    if not rows:
        return ""

    return f"""
   <div class="section">
     <h2>🔍 Strategy Divergence — Mixed Results</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Cities where strategies disagreed: at least one won while another lost.
       These are candidates for strategy optimization.
     </p>
     <table>
       <thead><tr><th>City</th><th>🎯 Sigma</th><th>🛡️ P5</th><th>📊 Mean</th></tr></thead>
       <tbody>{rows}
       </tbody>
     </table>
   </div>"""


# =============================================================================
# Live Temperature Fetch — Shared JavaScript helpers
# =============================================================================

def _build_cities_js_array() -> str:
    """Build a JavaScript array of city coordinates from the defaults JSON."""
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    entries: list[str] = []
    if defaults_path.exists():
        try:
            defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
            for loc in defaults.get("default_locations", []):
                name = loc.get("name", "")
                lat = loc.get("lat", 0)
                lon = loc.get("lon", 0)
                tz = loc.get("tz", "UTC")
                if name:
                    entries.append(
                        f'  {{name: "{name}", lat: {lat}, lon: {lon}, tz: "{tz}"}}'
                    )
        except Exception:
            pass
    return "const CITIES = [\n" + ",\n".join(entries) + "\n];"


def _build_live_fetch_js(*, with_rate_limiting: bool = False) -> str:
    """Build the JavaScript for live temperature fetching via Open-Meteo API.
    
    Fetches current temperature only (no daily parameter, which uses a separate
    quota bucket). Includes headers, retry logic, and 1s delay between cities
    to avoid browser-side rate limiting.
    """
    rate_limit_code = ""
    if with_rate_limiting:
        rate_limit_code = """
    const total = CITIES.length;
    let done = 0;
    for (const city of CITIES) {
        await fetchOneCity(city);
        done++;
        if (done < total) {
            document.getElementById('fetch-status').textContent = `⏳ Henter ${done}/${total}...`;
            await new Promise(r => setTimeout(r, 1000));
        }
    }"""

    return f"""// ---- Live Temperature Fetch (Open-Meteo, no API key) ----
async function fetchOneCity(city) {{
    const safeId = city.name.replace(/[^a-zA-Z0-9]/g, '_');
    for (let attempt = 0; attempt < 2; attempt++) {{
        try {{
            const resp = await fetch(
                `https://api.open-meteo.com/v1/forecast?latitude=${{city.lat}}&longitude=${{city.lon}}&current=temperature_2m&timezone=${{encodeURIComponent(city.tz)}}`,
                {{ headers: {{ 'User-Agent': 'WeatherMonitor/1.0' }} }}
            );
            const data = await resp.json();
            if (data.error) throw new Error(data.reason);
            
            const currentTemp = data.current?.temperature_2m;
            const el = document.getElementById('live-' + safeId);
            if (el) {{
                el.textContent = currentTemp != null
                    ? `🌡️${{currentTemp.toFixed(1)}}°C`
                    : '—';
            }}
            return {{ name: city.name, currentTemp }};
        }} catch (e) {{
            if (attempt === 0) await new Promise(r => setTimeout(r, 3000));
            else {{
                const el = document.getElementById('live-' + safeId);
                if (el) el.textContent = '⚠️';
            }}
        }}
    }}
}}

async function fetchLiveData() {{
    const statusEl = document.getElementById('fetch-status');
    const updatedEl = document.getElementById('live-updated');
    if (statusEl) statusEl.textContent = '⏳ Henter...';
    
    const results = [];{rate_limit_code}
    
    if (statusEl) statusEl.textContent = '✅ Oppdatert';
    if (updatedEl) updatedEl.textContent = new Date().toLocaleTimeString('no-NO');
    
    // Auto-disable button for 60s to prevent spam
    const btn = document.getElementById('fetch-btn');
    if (btn) {{
        btn.disabled = true;
        btn.textContent = '⏳ Vent 60s...';
        setTimeout(() => {{
            btn.disabled = false;
            btn.textContent = '🔄 Hent Nåværende Temperatur';
        }}, 60000);
    }}
    
    return results;
}}

// Manual fetch only — button click required, no auto-fetch on page load
"""


def _generate_html_report() -> str:
    """Generate a self-contained HTML dashboard with dark theme and 3-strategy comparison."""
    log_data = _load_log()
    runs = log_data.get("runs", [])
    cum = log_data.get("cumulative", {})

    # Aggregate per-strategy stats
    sigma_wins = sum(r.get("summary", {}).get("sigma_wins", 0) for r in runs)
    sigma_losses = sum(r.get("summary", {}).get("sigma_losses", 0) for r in runs)
    p5_wins = sum(r.get("summary", {}).get("p5_wins", 0) for r in runs)
    p5_losses = sum(r.get("summary", {}).get("p5_losses", 0) for r in runs)
    mean_wins = sum(r.get("summary", {}).get("mean_wins", 0) for r in runs)
    mean_losses = sum(r.get("summary", {}).get("mean_losses", 0) for r in runs)

    sigma_total = sigma_wins + sigma_losses
    p5_total = p5_wins + p5_losses
    mean_total = mean_wins + mean_losses
    overall_total = sigma_total  # We use sigma as the primary metric

    # Count unique dates
    unique_dates = set()
    for r in runs:
        rd = r.get("run_date", "")
        if rd:
            unique_dates.add(rd)
    total_days = len(unique_dates)

    # Avg confidence (sigma strategy)
    all_conf_w: list[float] = []
    all_conf_l: list[float] = []
    for run in runs:
        for pdata in run.get("predictions", {}).values():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            if sigma.get("result") == "WIN":
                all_conf_w.append(pdata.get("confidence", 0))
            elif sigma.get("result") == "LOSS":
                all_conf_l.append(pdata.get("confidence", 0))
    avg_conf_w = round(sum(all_conf_w) / max(1, len(all_conf_w)), 3)
    avg_conf_l = round(sum(all_conf_l) / max(1, len(all_conf_l)), 3)

    # Per confidence tier (sigma strategy, 4 tiers)
    tiers = [
        {"pos": 0, "wins": 0, "label": ">80%", "icon": "🟢", "lo": 0.8, "hi": 1.0},
        {"pos": 0, "wins": 0, "label": "70-80%", "icon": "🟠", "lo": 0.7, "hi": 0.8},
        {"pos": 0, "wins": 0, "label": "60-70%", "icon": "🔴", "lo": 0.6, "hi": 0.7},
        {"pos": 0, "wins": 0, "label": "<60%", "icon": "🔴", "lo": 0.0, "hi": 0.6},
    ]

    for run in runs:
        for pdata in run.get("predictions", {}).values():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            conf = pdata.get("confidence", 0)
            if result in ("WIN", "LOSS"):
                for t in tiers:
                    if t["lo"] <= conf < t["hi"] or (t["hi"] == 1.0 and conf >= 0.8):
                        t["pos"] += 1
                        if result == "WIN":
                            t["wins"] += 1
                        break

    tier_rows = ""
    for t in tiers:
        losses = t["pos"] - t["wins"]
        wr = round(t["wins"] / max(1, t["pos"]) * 100, 1) if t["pos"] > 0 else 0
        tier_rows += f"""
            <tr>
                <td>{t['icon']} {t['label']}</td>
                <td>{t['pos']}</td>
                <td>{t['wins']}</td>
                <td>{losses}</td>
                <td>{wr}%</td>
            </tr>"""

    # Recent daily results
    daily_rows = ""
    recent = sorted(runs, key=lambda r: r.get("run_date", ""), reverse=True)[:14]
    for r in recent:
        s = r.get("summary", {})
        sw = s.get("sigma_wins", 0)
        sl = s.get("sigma_losses", 0)
        pw = s.get("p5_wins", 0)
        pl = s.get("p5_losses", 0)
        mw = s.get("mean_wins", 0)
        ml = s.get("mean_losses", 0)
        phase = r.get("phase", "?")
        sigma_rate = round(sw / max(1, sw + sl) * 100, 1) if (sw + sl) > 0 else "—"
        sigma_rate_str = f"{sigma_rate}%" if isinstance(sigma_rate, (int, float)) else str(sigma_rate)
        phase_badge = {"daily_bma": "🔮 BMA", "hourly_check": "⏱️ hourly",
                       "rapid_peak_monitor": "⚡ rapid", "daily_close": "🔒 closed"}.get(phase, phase)
        daily_rows += f"""
                <tr><td>{r['run_date']}</td><td>{phase_badge}</td><td>{sw}/{sl}</td><td>{pw}/{pl}</td><td>{mw}/{ml}</td><td>{sigma_rate_str}</td></tr>"""

    # ── Top 5 predictions section (multi-day: I DAG + I MORGEN) ──
    predictions_html = ""
    latest_run = runs[-1] if runs else {}

    if latest_run:
        top5_cities = latest_run.get("top_5_confidence", [])
        preds = latest_run.get("predictions", {})
        multi_day = latest_run.get("predictions_multi_day", {})
        target_date = latest_run.get("target_date", latest_run.get("run_date", ""))

        # ── Day 1: I DAG ──
        if top5_cities:
            top5_rows = _build_top5_rows_html(preds, top5_cities[:5])
            flip_section = _build_flip_recommendations_section(preds, top5_cities[:5])
            strategy_comparison = _build_strategy_comparison_section(preds)
            divergence_section = _build_city_divergence_section(preds)

            predictions_html += f"""
    <div class="section">
      <h2>📅 TOP 5 — I DAG ({target_date})</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        All 3 strategies shown: 🎯 Sigma (μ−kσ, dynamic k), 🛡️ P5-Basert (ultra-conservative), 📊 Mean-Basert (50/50)
      </p>
      <div style="overflow-x: auto;">
      <table>
        <thead><tr><th>#</th><th>City</th><th>BMA μ</th><th>Sigma Pos</th><th>P5 Pos</th><th>Mean Pos</th><th>Conf</th><th>Models</th><th>Peak</th><th>Sigma</th><th>P5</th><th>Mean</th><th>Rec</th></tr></thead>
        <tbody>{top5_rows}
        </tbody>
      </table>
      </div>
    </div>
    {flip_section}
    {divergence_section}
    {strategy_comparison}"""

        # ── Day 2: I MORGEN ──
        day2_preds = multi_day.get("day2", {})
        if day2_preds:
            try:
                tomorrow_date = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
            except (ValueError, TypeError):
                tomorrow_date = (date.today() + timedelta(days=1)).isoformat()

            # Sort day2 by confidence for top 5
            day2_sorted = sorted(
                day2_preds.items(),
                key=lambda kv: kv[1].get("confidence", 0),
                reverse=True,
            )
            day2_top5 = [c for c, _ in day2_sorted[:5]]
            day2_top5_rows = _build_top5_rows_html(day2_preds, day2_top5)

            predictions_html += f"""
    <div class="section">
      <h2>📅 TOP 5 — I MORGEN ({tomorrow_date})</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        All 3 strategies shown: 🎯 Sigma (μ−kσ, dynamic k), 🛡️ P5-Basert (ultra-conservative), 📊 Mean-Basert (50/50)
      </p>
      <div style="overflow-x: auto;">
      <table>
        <thead><tr><th>#</th><th>City</th><th>BMA μ</th><th>Sigma Pos</th><th>P5 Pos</th><th>Mean Pos</th><th>Conf</th><th>Models</th><th>Peak</th><th>Sigma</th><th>P5</th><th>Mean</th><th>Rec</th></tr></thead>
        <tbody>{day2_top5_rows}
        </tbody>
      </table>
      </div>
    </div>"""

        # ── RESOLVED RESULTS section (show all 51 cities' outcomes) ──
        resolved_rows = ""
        resolved_cities = []
        for city, pdata in sorted(preds.items()):
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            p5s = strategies.get("p5", {})
            means = strategies.get("mean", {})
            sigma_result = sigma.get("result", "")
            if sigma_result in ("WIN", "LOSS"):
                resolved_cities.append((city, sigma, p5s, means))

        if resolved_cities:
            for city, sigma, p5s, means in resolved_cities:
                def _ri(r):
                    return "✅ WIN" if r == "WIN" else ("❌ LOSS" if r == "LOSS" else "⏳")

                sigma_spill = sigma.get("spill", "?")
                p5_spill = p5s.get("spill", "?")
                mean_spill = means.get("spill", "?")
                actual = sigma.get("actual_peak")
                actual_str = f"{actual:.1f}°C" if isinstance(actual, (int, float)) else "—"

                resolved_rows += f"""
                <tr>
                    <td><strong>{city}</strong></td>
                    <td>BUY {sigma_spill}°C</td>
                    <td>{_ri(sigma.get('result', ''))} ({actual_str})</td>
                    <td>BUY {p5_spill}°C</td>
                    <td>{_ri(p5s.get('result', ''))}</td>
                    <td>BUY {mean_spill}°C</td>
                    <td>{_ri(means.get('result', ''))}</td>
                </tr>"""

            predictions_html += f"""
   <div class="section">
      <h2>📊 AVGJORTE RESULTATER ({len(resolved_cities)} byer, {target_date})</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Resolved against archive data. ✅ WIN = round(actual peak) == spill bucket.
      </p>
     <div style="max-height: 600px; overflow-y: auto;">
     <table>
       <thead><tr><th>By</th><th>Sigma Spill</th><th>Sigma Utfall</th><th>P5 Spill</th><th>P5 Utfall</th><th>Mean Spill</th><th>Mean Utfall</th></tr></thead>
       <tbody>{resolved_rows}
       </tbody>
     </table>
     </div>
   </div>"""

    # Strategy summary cards
    strategy_cards = _build_strategy_summary_cards(
        sigma_wins, sigma_losses, p5_wins, p5_losses, mean_wins, mean_losses
    )

    # Overall win rate uses sigma strategy
    overall_win_rate = round(sigma_wins / max(1, sigma_total) * 100, 1)
    win_color = "#4CAF50" if overall_win_rate >= 55 else ("#FF9800" if overall_win_rate >= 45 else "#F44336")
    if sigma_total == 0:
        win_color = "#8b949e"

    deploy_time_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    has_data = len(runs) > 0

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Model Quality Dashboard — 3-Strategy BMA Ensemble</title>
<style>
  :root {{
    --bg: #0d1117;
    --bg-card: #161b22;
    --bg-card-hover: #1c2333;
    --border: #30363d;
    --text: #c9d1d9;
    --text-dim: #8b949e;
    --green: #3fb950;
    --red: #f85149;
    --orange: #d2991d;
    --blue: #58a6ff;
    --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
    background: var(--bg);
    color: var(--text);
    min-height: 100vh;
    line-height: 1.6;
  }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  header {{
    text-align: center;
    padding: 30px 20px;
    border-bottom: 1px solid var(--border);
    margin-bottom: 24px;
  }}
  header h1 {{ font-size: 1.8rem; color: var(--blue); font-weight: 700; }}
  header .subtitle {{ color: var(--text-dim); font-size: 0.9rem; margin-top: 4px; }}
  .card-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
    gap: 16px;
    margin-bottom: 24px;
  }}
  .card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 20px;
    text-align: center;
    transition: background 0.2s;
  }}
  .card:hover {{ background: var(--bg-card-hover); }}
  .card .value {{ font-size: 2rem; font-weight: 700; }}
  .card .label {{ color: var(--text-dim); font-size: 0.85rem; margin-top: 4px; }}
  .section {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 24px;
    margin-bottom: 20px;
  }}
  .section h2 {{
    font-size: 1.2rem;
    color: var(--purple);
    margin-bottom: 16px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--border);
  }}
  table {{
    width: 100%;
    border-collapse: collapse;
    font-size: 0.85rem;
  }}
  th, td {{
    text-align: left;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
    white-space: nowrap;
  }}
  th {{ color: var(--text-dim); font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; cursor: pointer; }}
  th:hover {{ color: var(--blue); }}
  tr:hover {{ background: var(--bg-card-hover); }}
  .grid-2 {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 20px;
  }}
  @media (max-width: 768px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  footer {{
    text-align: center;
    padding: 20px;
    color: var(--text-dim);
    font-size: 0.8rem;
    border-top: 1px solid var(--border);
    margin-top: 24px;
  }}
  .badge-win {{ color: #1b5e20; font-weight: 600; }}
  .badge-loss {{ color: #b71c1c; font-weight: 600; }}
  .row-win td {{ color: #1a1a1a !important; }}
  .row-loss td {{ color: #1a1a1a !important; }}
  .status-win {{ color: #1b5e20; }}
  .status-loss {{ color: #b71c1c; }}
  .rapid-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 600;
    background: rgba(88, 166, 255, 0.15);
    color: var(--blue);
    margin-left: 8px;
  }}
  .best-strategy {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 12px;
    font-size: 0.75rem;
    font-weight: 600;
    background: rgba(63, 185, 80, 0.15);
    color: var(--green);
    margin-left: 8px;
  }}
  .live-bar {{
    text-align: center;
    padding: 14px 20px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 0 auto 20px;
    max-width: 700px;
  }}
  .live-btn {{
    background: rgba(88, 166, 255, 0.15);
    border: 1px solid var(--blue);
    color: var(--blue);
    padding: 10px 24px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
    transition: all 0.2s;
  }}
  .live-btn:hover {{ background: rgba(88, 166, 255, 0.25); }}
  .live-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .live-status {{ color: var(--text-dim); font-size: 0.85rem; margin-left: 12px; }}
  .live-updated {{ color: var(--text-dim); font-size: 0.8rem; margin-left: 8px; }}
</style>
<script>
// No auto-refresh — manual refresh only
// Dynamic "last updated" countdown + next pipeline indicator
(function() {{
    var deployTime = new Date('{deploy_time_iso}');
    var HAS_DATA = {str(has_data).lower()};

    function getNextPipelineUTC() {{
        var now = new Date();
        var next = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), now.getUTCHours() + 1, 0, 0));
        var hh = String(next.getUTCHours()).padStart(2, '0');
        var mm = String(next.getUTCMinutes()).padStart(2, '0');
        return hh + ':' + mm + ' UTC';
    }}

    function updateTimer() {{
        var el = document.getElementById('last-updated');
        if (!el) return;
        if (!HAS_DATA) {{
            el.innerHTML = '⏳ Ingen data enda &mdash; f&oslash;rste pipeline-kj&oslash;ring kl 06:00 UTC';
            return;
        }}
        var now = new Date();
        var diff = Math.floor((now - deployTime) / 1000);
        var mins = Math.floor(diff / 60);
        var secs = diff % 60;
        var nextPipe = getNextPipelineUTC();
        var timeStr;
        if (mins >= 120) {{
            timeStr = Math.floor(mins / 60) + 't ' + (mins % 60) + 'm siden';
        }} else if (mins >= 60) {{
            timeStr = Math.floor(mins / 60) + 't ' + (mins % 60) + 'm siden';
        }} else if (mins > 0) {{
            timeStr = mins + 'm ' + secs + 's siden';
        }} else {{
            timeStr = secs + 's siden';
        }}
        el.innerHTML = '&#x1F504; Sist oppdatert: ' + timeStr + ' | Auto-refresh hvert 5. min | Neste pipeline: ' + nextPipe;
    }}

    updateTimer();
    setInterval(updateTimer, 10000); // Update every 10 seconds
}})();

function sortTable(colIdx) {{
  const table = document.getElementById("strategyTable");
  if (!table) return;
  const tbody = table.querySelector("tbody");
  const rows = Array.from(tbody.querySelectorAll("tr"));
  const isAsc = table.dataset.sortCol == colIdx ? table.dataset.sortDir != "asc" : true;
  rows.sort((a, b) => {{
    const aVal = a.cells[colIdx].textContent.trim();
    const bVal = b.cells[colIdx].textContent.trim();
    return isAsc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
  }});
  rows.forEach(r => tbody.appendChild(r));
  table.dataset.sortCol = colIdx;
  table.dataset.sortDir = isAsc ? "asc" : "desc";
}}
</script>
</head>
<body>
<header>
  <h1>🌡️ Model Quality Dashboard <span class="rapid-badge">3 STRATEGIES</span></h1>
  <div class="subtitle" id="last-updated">{'⏳ Ingen data enda — første pipeline-kjøring kl 06:00 UTC' if not has_data else '🔄 Sist oppdatert: … | Auto-refresh hvert 5. min | Neste pipeline: …'}</div>
</header>
<div class="container">

  <div class="card-grid">
    <div class="card">
      <div class="value" style="color: var(--blue);">{total_days}</div>
      <div class="label">Days Tracked</div>
    </div>
    <div class="card">
      <div class="value" style="color: var(--purple);">{overall_total}</div>
      <div class="label">Total Resolved (Sigma)</div>
    </div>
    <div class="card">
      <div class="value" style="color: {win_color};">{overall_win_rate}%</div>
      <div class="label">Overall Win Rate (Sigma)</div>
    </div>
    <div class="card">
      <div class="value" style="color: var(--text);">{avg_conf_w:.3f} / {avg_conf_l:.3f}</div>
      <div class="label">Avg Conf (Winners / Losers)</div>
    </div>
  </div>

  <!-- PER-STRATEGY PERFORMANCE CARDS -->
  {strategy_cards}

  <!-- TOP 5 PREDICTIONS -->
  {predictions_html}

  <div class="section">
    <h2>📊 WIN RATE BY CONFIDENCE (resolved markets only)</h2>
    <table>
      <thead><tr><th>Tier</th><th>Positions</th><th>Wins</th><th>Losses</th><th>Win Rate</th></tr></thead>
      <tbody>{tier_rows if tier_rows.strip() else '<tr><td colspan="5" style="color: var(--text-dim);">No resolved results yet — predictions pending</td></tr>'}
      </tbody>
    </table>
  </div>

  <div class="section">
    <h2>📋 Recent Daily Results (Last 14 Runs)</h2>
    <div style="overflow-x: auto;">
    <table>
      <thead><tr><th>Date</th><th>Phase</th><th>Sigma W/L</th><th>P5 W/L</th><th>Mean W/L</th><th>Rate (Sigma)</th></tr></thead>
      <tbody>{daily_rows if daily_rows else '<tr><td colspan="6" style="color: var(--text-dim);">No runs recorded yet</td></tr>'}
      </tbody>
    </table>
    </div>
  </div>

  <div class="section">
    <h2>⚡ Rapid Peak Monitoring Filters</h2>
    <table>
      <thead><tr><th>Filter</th><th>Condition</th><th>Adjustment</th></tr></thead>
      <tbody>
        <tr><td>💧 Humidity</td><td>>80% relative humidity</td><td>-8% confidence</td></tr>
        <tr><td>💧 Humidity</td><td><40% relative humidity</td><td>+3% confidence</td></tr>
        <tr><td>☁️ Cloud Cover</td><td>>70% cloud cover</td><td>-5% confidence</td></tr>
        <tr><td>☁️ Cloud Cover</td><td><20% cloud cover</td><td>+3% confidence</td></tr>
        <tr><td>🏙️ UHI</td><td>Urban Heat Island adjustment</td><td>+0.5–3.0°C to BMA prediction</td></tr>
        <tr><td>💰 Kelly</td><td>Position sizing criterion</td><td>Optimal bet % of bankroll</td></tr>
        <tr><td>🔗 Correlation</td><td>Cross-city correlation warnings</td><td>Reduce exposure if r >= 0.55</td></tr>
        <tr><td>📊 Ensemble Spread</td><td>P5–P95 range tracking</td><td>Narrow spread = higher confidence</td></tr>
        <tr><td>🚨 Alert Levels</td><td>INFO → MOMENTANT_OVER → ADVARSEL → KRITISK → BEKREFTET</td><td>Progressive alerting</td></tr>
      </tbody>
    </table>
  </div>

</div>
<footer>
  Model Quality Dashboard · 3-Strategy Comparison · Sigma (μ−kσ) vs P5 vs Mean · GitHub Pages Deploy
</footer>
</body>
</html>"""
    return html


ALL_CITIES_HTML_FILE = Path(_SCRIPT_DIR) / "_all_cities.html"


def _generate_all_cities_html() -> str:
    """Generate a self-contained HTML dashboard showing ALL 51 cities
    for the single target date (same day prediction → resolution).

    Data is read from the latest run's flat predictions in the quality log.
    City metadata (timezone, station) is pulled from weather_monitor_defaults.json.
    """
    log_data = _load_log()
    runs = log_data.get("runs", [])

    # Load city defaults for timezone / station metadata
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    city_meta: dict[str, dict] = {}
    if defaults_path.exists():
        try:
            defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
            for loc in defaults.get("default_locations", []):
                name = loc.get("name", "")
                if name:
                    city_meta[name] = {
                        "tz": loc.get("tz", "UTC"),
                        "station": loc.get("station", ""),
                        "lat": loc.get("lat", 0),
                        "lon": loc.get("lon", 0),
                        "peak_hour_start": loc.get("peak_hour_start", 14),
                        "peak_hour_end": loc.get("peak_hour_end", 16),
                    }
        except Exception:
            pass

    # Use multi-day predictions if available, fall back to flat predictions
    latest_run = runs[-1] if runs else {}
    multi_day = latest_run.get("predictions_multi_day", {})
    target_date = latest_run.get("target_date", latest_run.get("run_date", ""))

    day_data_by_lead: dict[int, dict] = {}
    day_labels: dict[int, str] = {}
    day_target_dates: dict[int, str] = {}

    # Day 1: Today (lead_days=0)
    day1_preds = multi_day.get("day1", latest_run.get("predictions", {}))
    if day1_preds:
        day_data_by_lead[0] = day1_preds
        day_labels[0] = "I DAG"
        day_target_dates[0] = target_date

    # Day 2: Tomorrow (lead_days=1)
    day2_preds = multi_day.get("day2", {})
    if day2_preds:
        try:
            tomorrow = (date.fromisoformat(target_date) + timedelta(days=1)).isoformat()
        except (ValueError, TypeError):
            tomorrow = (date.today() + timedelta(days=1)).isoformat()
        day_data_by_lead[1] = day2_preds
        day_labels[1] = "I MORGEN"
        day_target_dates[1] = tomorrow

    if not day_data_by_lead:
        return "<!DOCTYPE html><html lang=\"no\"><head><meta charset=\"UTF-8\"><title>Alle 51 Byer</title></head><body style=\"background:#0d1117;color:#c9d1d9;font-family:sans-serif;padding:40px;text-align:center;\"><h1>Ingen data enda</h1><p>Kjor pipeline forst.</p></body></html>"

    # Collect ALL unique city names across all lead days
    all_cities_set: set[str] = set()
    for preds in day_data_by_lead.values():
        all_cities_set.update(preds.keys())
    # Also pull cities from defaults for completeness
    all_cities_set.update(city_meta.keys())
    all_cities = sorted(all_cities_set)

    sorted_leads = sorted(day_data_by_lead.keys())

    # Build the per-city data structure
    city_table: dict[str, dict[int, dict]] = {}

    for city in all_cities:
        city_table[city] = {}
        for ld in sorted_leads:
            pdata = day_data_by_lead.get(ld, {}).get(city, {})
            if not pdata:
                city_table[city][ld] = None  # type: ignore[assignment]
                continue

            bma_mean = pdata.get("bma_mean", None)
            bma_std = pdata.get("bma_std", None)
            conf = pdata.get("confidence", 0)
            model_ct = pdata.get("models", 0)
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            p5s = strategies.get("p5", {})
            means = strategies.get("mean", {})
            rec = pdata.get("recommendation", "—") or "—"
            actual_peak = sigma.get("actual_peak")

            city_table[city][ld] = {
                "bma_mean": bma_mean,
                "bma_std": bma_std,
                "conf": conf,
                "model_ct": model_ct,
                "sigma_spill": sigma.get("spill", "?"),
                "sigma_result": sigma.get("result", ""),
                "p5_spill": p5s.get("spill", "?"),
                "p5_result": p5s.get("result", ""),
                "mean_spill": means.get("spill", "?"),
                "mean_result": means.get("result", ""),
                "rec": rec,
                "actual_peak": actual_peak,
            }

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ---- Build date button bar ----
    date_buttons_html = ""
    for ld in sorted_leads:
        label = day_labels.get(ld, f"+{ld}")
        target = day_target_dates.get(ld, "")
        active_class = "active" if ld == 0 else ""
        date_buttons_html += (
            f'<button class="date-btn {active_class}" '
            f'onclick="switchDate({ld})" id="btn-{ld}">'
            f'{label}<br/><small>{target}</small></button>\n        '
        )

    # ---- Build table rows ----
    table_rows = ""
    for city in all_cities:
        meta = city_meta.get(city, {})
        tz_str = meta.get("tz", "UTC")
        local_time_str = ""
        try:
            local_now = datetime.now(ZoneInfo(tz_str))
            local_time_str = local_now.strftime("%H:%M")
        except Exception:
            local_time_str = "—"

        for ld in sorted_leads:
            d = city_table[city].get(ld)
            if d is None:
                continue
            conf = d["conf"]
            if conf >= 0.8:
                conf_icon = "🟢"
                conf_class = "conf-high"
            elif conf >= 0.7:
                conf_icon = "🟠"
                conf_class = "conf-mid"
            else:
                conf_icon = "🔴"
                conf_class = "conf-low"

            bma_str = f"{d['bma_mean']:.1f}" if isinstance(d['bma_mean'], (int, float)) else "—"
            std_str = f"{d['bma_std']:.1f}" if isinstance(d['bma_std'], (int, float)) else "—"

            p5_p95 = f"{d['p5_spill']}-{d['sigma_spill']}" if d['p5_spill'] != "?" and d['sigma_spill'] != "?" else "—"

            def _ri(r: str) -> str:
                if r == "WIN":
                    return "✅"
                elif r == "LOSS":
                    return "❌"
                return "⏳"

            sigma_cell = f'BUY {d["sigma_spill"]}°C {_ri(d["sigma_result"])}'
            p5_cell = f'BUY {d["p5_spill"]}°C {_ri(d["p5_result"])}'
            mean_cell = f'BUY {d["mean_spill"]}°C {_ri(d["mean_result"])}'

            actual_str = f"{d['actual_peak']:.1f}°C" if isinstance(d['actual_peak'], (int, float)) else "—"

            rec = d.get("rec", "—")
            rec_class = ""
            if rec and "HOLD" in str(rec):
                rec_class = "rec-hold"
            elif rec and "SELG" in str(rec):
                rec_class = "rec-sell"
            elif rec and "AVVENT" in str(rec):
                rec_class = "rec-wait"

            # Check if today's observed max resolves to the spill bucket (rounding rule)
            actual_peak = d.get("actual_peak")
            sigma_spill = d["sigma_spill"]
            row_win = False
            peak_won = False
            if isinstance(actual_peak, (int, float)) and isinstance(sigma_spill, (int, float)):
                if round(actual_peak) == sigma_spill:
                    row_win = True
                    peak_won = True
            row_class = "city-row row-win" if row_win else "city-row row-loss"

            safe_city_id = re.sub(r'[^a-zA-Z0-9]', '_', city)
            table_rows += f"""<tr class="{row_class}" data-lead="{ld}" data-city="{city}" data-conf="{conf:.3f}">
                <td class="col-rank"></td>
                <td class="col-city">{city}</td>
                <td class="col-bma">{bma_str} <span class="dim">σ={std_str}</span></td>
                <td class="col-range">{p5_p95}°C</td>
                <td class="col-sigma">{sigma_cell}</td>
                <td class="col-p5">{p5_cell}</td>
                <td class="col-mean">{mean_cell}</td>
                <td class="col-conf {conf_class}">{conf_icon} {(conf*100):.0f}%</td>
                <td class="col-models">{d['model_ct']}/8</td>
                <td class="col-peak">{actual_str}{" ✅ VUNNET" if peak_won else ""}</td>
                <td class="col-live" id="live-{safe_city_id}">—</td>
                <td class="col-rec {rec_class}">{rec}</td>
                <td class="col-local">{local_time_str}</td>
            </tr>
"""

    cities_js = _build_cities_js_array()
    live_fetch_js = _build_live_fetch_js(with_rate_limiting=True)

    # ---- Build full HTML (no auto-refresh) ----
    html = f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Alle 51 Byer — BMA Ensemble Dashboard</title>
<style>
  :root {{
    --bg: #0d1117; --bg-card: #161b22; --bg-card-hover: #1c2333;
    --border: #30363d; --text: #c9d1d9; --text-dim: #8b949e;
    --green: #3fb950; --red: #f85149; --orange: #d2991d;
    --blue: #58a6ff; --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; line-height: 1.6; }}
  .container {{ max-width: 1600px; margin: 0 auto; padding: 20px; }}
  header {{ text-align: center; padding: 24px 20px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }}
  header h1 {{ font-size: 1.6rem; color: var(--blue); font-weight: 700; }}
  header .subtitle {{ color: var(--text-dim); font-size: 0.85rem; margin-top: 4px; }}
  .live-bar {{
    text-align: center;
    padding: 12px 20px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    margin: 0 auto 20px;
    max-width: 800px;
  }}
  .live-btn {{
    background: rgba(88, 166, 255, 0.15);
    border: 1px solid var(--blue);
    color: var(--blue);
    padding: 10px 24px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 0.9rem;
    font-weight: 600;
    transition: all 0.2s;
  }}
  .live-btn:hover {{ background: rgba(88, 166, 255, 0.25); }}
  .live-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
  .live-status {{ color: var(--text-dim); font-size: 0.85rem; margin-left: 12px; }}
  .live-updated {{ color: var(--text-dim); font-size: 0.8rem; margin-left: 8px; }}
  .col-live {{ color: var(--green); font-weight: 600; font-size: 0.78rem; }}
  .date-bar {{ display: flex; gap: 10px; justify-content: center; margin-bottom: 20px; flex-wrap: wrap; }}
  .date-btn {{ background: var(--bg-card); border: 1px solid var(--border); color: var(--text); padding: 10px 24px; border-radius: 8px; cursor: pointer; font-size: 0.9rem; font-weight: 600; transition: all 0.2s; }}
  .date-btn:hover {{ background: var(--bg-card-hover); border-color: var(--blue); }}
  .date-btn.active {{ background: rgba(88, 166, 255, 0.15); border-color: var(--blue); color: var(--blue); }}
  .date-btn small {{ display: block; font-weight: 400; color: var(--text-dim); font-size: 0.7rem; }}
  .filter-bar {{ display: flex; gap: 12px; align-items: center; margin-bottom: 16px; flex-wrap: wrap; justify-content: center; }}
  .filter-bar input {{ background: var(--bg-card); border: 1px solid var(--border); color: var(--text); padding: 8px 14px; border-radius: 6px; font-size: 0.85rem; width: 220px; }}
  .filter-bar input::placeholder {{ color: var(--text-dim); }}
  .filter-bar .info-text {{ color: var(--text-dim); font-size: 0.8rem; }}
  .table-wrap {{ background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; overflow: hidden; }}
  .table-scroll {{ max-height: 75vh; overflow-y: auto; overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  thead {{ position: sticky; top: 0; z-index: 10; }}
  th {{ background: #21262d; color: var(--text-dim); font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; padding: 10px 8px; border-bottom: 1px solid var(--border); cursor: pointer; white-space: nowrap; user-select: none; }}
  th:hover {{ color: var(--blue); }}
  th.sorted-asc::after {{ content: " ▲"; }}
  th.sorted-desc::after {{ content: " ▼"; }}
  td {{ padding: 7px 8px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
  tr:hover {{ background: var(--bg-card-hover); }}
  tr.city-row.hidden {{ display: none; }}
  .dim {{ color: var(--text-dim); font-size: 0.7rem; }}
  .conf-high {{ color: var(--green); font-weight: 600; }}
  .conf-mid {{ color: var(--orange); font-weight: 600; }}
  .conf-low {{ color: var(--red); font-weight: 600; }}
  .rec-hold {{ color: #1b5e20; }}
  .rec-sell {{ color: #b71c1c; }}
  .rec-wait {{ color: #d2991d; }}
  .row-win {{ background: #c8e6c9; }}
  .row-win td {{ color: #1a1a1a !important; }}
  .row-win:hover {{ background: #b9d9ba; }}
  .row-loss {{ background: #ffcdd2; }}
  .row-loss td {{ color: #1a1a1a !important; }}
  .row-loss:hover {{ background: #f0c0c5; }}
  .status-win {{ color: #1b5e20; }}
  .status-loss {{ color: #b71c1c; }}
  footer {{ text-align: center; padding: 16px; color: var(--text-dim); font-size: 0.75rem; border-top: 1px solid var(--border); margin-top: 20px; }}
  @media (max-width: 768px) {{ .date-bar {{ gap: 6px; }} .date-btn {{ padding: 8px 14px; font-size: 0.8rem; }} table {{ font-size: 0.7rem; }} }}
</style>
</head>
<body>
<header>
  <h1>🌍 ALLE 51 BYER — BMA Ensemble</h1>
  <div class="subtitle">Generert: {now_str} | Multi-Strategy: 🎯 Sigma · 🛡️ P5 · 📊 Mean</div>
</header>
<div class="live-bar">
  <button class="live-btn" onclick="fetchLiveData()" id="fetch-btn">🔄 Hent Nåværende Temperatur & Døgnmaks</button>
  <span class="live-status" id="fetch-status"></span>
  <span class="live-updated" id="live-updated"></span>
</div>
<div class="container">

  <div class="date-bar" id="dateBar">
    {date_buttons_html}
  </div>

  <div class="filter-bar">
    <input type="text" id="cityFilter" placeholder="🔍 Søk by..." oninput="applyFilters()">
    <span class="info-text" id="visibleCount"></span>
    <span class="info-text" style="margin-left: auto;">Klikk kolonneoverskrift for å sortere</span>
  </div>

  <div class="table-wrap">
    <div class="table-scroll">
    <table id="cityTable">
      <thead>
        <tr>
          <th onclick="sortTable(0)">#</th>
          <th onclick="sortTable(1)">By</th>
          <th onclick="sortTable(2)">BMA μ</th>
          <th onclick="sortTable(3)">P5–P95</th>
          <th onclick="sortTable(4)">🎯 Sigma</th>
          <th onclick="sortTable(5)">🛡️ P5</th>
          <th onclick="sortTable(6)">📊 Mean</th>
          <th onclick="sortTable(7)">Konf</th>
          <th onclick="sortTable(8)">Modeller</th>
          <th onclick="sortTable(9)">Peak</th>
          <th onclick="sortTable(10)">🔴 Live</th>
          <th onclick="sortTable(11)">Anbefaling</th>
          <th onclick="sortTable(12)">🕐 Lokal</th>
        </tr>
      </thead>
      <tbody>
        {table_rows}
      </tbody>
    </table>
    </div>
  </div>

</div>
<footer>
  Alle 51 Byer Dashboard · BMA Multi-Model Ensemble · 3-Strategy Comparison · GitHub Pages Deploy
</footer>

<script>
{cities_js}

{live_fetch_js}

var currentLead = {sorted_leads[0] if sorted_leads else 1};

function switchDate(lead) {{
    currentLead = lead;
    document.querySelectorAll('.date-btn').forEach(function(b) {{ b.classList.remove('active'); }});
    var btn = document.getElementById('btn-' + lead);
    if (btn) btn.classList.add('active');
    applyFilters();
}}

function applyFilters() {{
    var filterText = (document.getElementById('cityFilter').value || '').toLowerCase();
    var rows = document.querySelectorAll('#cityTable tbody tr.city-row');
    var visibleCount = 0;
    rows.forEach(function(row) {{
        var rowLead = parseInt(row.getAttribute('data-lead'));
        var cityName = (row.getAttribute('data-city') || '').toLowerCase();
        var leadMatch = (rowLead === currentLead);
        var filterMatch = !filterText || cityName.indexOf(filterText) !== -1;
        if (leadMatch && filterMatch) {{
            row.classList.remove('hidden');
            visibleCount++;
        }} else {{
            row.classList.add('hidden');
        }}
    }});
    var rank = 1;
    rows.forEach(function(row) {{
        if (!row.classList.contains('hidden')) {{
            row.querySelector('.col-rank').textContent = rank++;
        }}
    }});
    document.getElementById('visibleCount').textContent = visibleCount + ' byer vist';
}}

var sortCol = -1;
var sortAsc = true;

function sortTable(colIdx) {{
    var table = document.getElementById('cityTable');
    var tbody = table.querySelector('tbody');
    var rows = Array.from(tbody.querySelectorAll('tr.city-row'));
    if (sortCol === colIdx) {{ sortAsc = !sortAsc; }}
    else {{ sortCol = colIdx; sortAsc = true; }}
    document.querySelectorAll('th').forEach(function(th, i) {{
        th.classList.remove('sorted-asc', 'sorted-desc');
        if (i === colIdx) th.classList.add(sortAsc ? 'sorted-asc' : 'sorted-desc');
    }});
    rows.sort(function(a, b) {{
        // Confidence column (7): use data-conf attribute for numeric sorting
        if (colIdx === 7) {{
            var aNum = parseFloat(a.getAttribute('data-conf')) || 0;
            var bNum = parseFloat(b.getAttribute('data-conf')) || 0;
            return sortAsc ? aNum - bNum : bNum - aNum;
        }}
        var aVal = (a.cells[colIdx] ? a.cells[colIdx].textContent.trim() : '');
        var bVal = (b.cells[colIdx] ? b.cells[colIdx].textContent.trim() : '');
        if (colIdx === 0 || colIdx === 2 || colIdx === 3 || colIdx === 8 || colIdx === 9) {{
            var aNum = parseFloat(aVal) || 0;
            var bNum = parseFloat(bVal) || 0;
            return sortAsc ? aNum - bNum : bNum - aNum;
        }}
        return sortAsc ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
    }});
    rows.forEach(function(r) {{ tbody.appendChild(r); }});
    applyFilters();
}}

(function() {{
    var firstBtn = document.querySelector('.date-btn.active');
    if (!firstBtn) {{
        var btns = document.querySelectorAll('.date-btn');
        if (btns.length > 0) {{
            var m = btns[0].getAttribute('onclick').match(/\\d+/);
            if (m) switchDate(parseInt(m[0]));
        }}
    }} else {{ switchDate(currentLead); }}
    applyFilters();
    // Default sort: confidence descending (col 7)
    sortCol = 7;
    sortAsc = false;
    sortTable(7);
}})();
</script>
</body>
</html>"""
    return html


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate human-readable quality report from _model_quality_log.json",
    )
    parser.add_argument(
        "--html",
        action="store_true",
        help="Generate self-contained HTML dashboard (_quality_report.html)",
    )
    parser.add_argument(
        "--all-cities",
        action="store_true",
        help="Generate all-51-cities standalone HTML page (_all_cities.html)",
    )
    args = parser.parse_args()

    if args.all_cities:
        html = _generate_all_cities_html()
        ALL_CITIES_HTML_FILE.write_text(html, encoding="utf-8")
        print(f"All-cities HTML dashboard written to: {ALL_CITIES_HTML_FILE}")
    elif args.html:
        html = _generate_html_report()
        HTML_REPORT_FILE.write_text(html, encoding="utf-8")
        print(f"HTML dashboard written to: {HTML_REPORT_FILE}")
    else:
        report = _generate_report()
        print(report)
        REPORT_FILE.write_text(report, encoding="utf-8")
        print(f"Rapport skrevet til: {REPORT_FILE}")


if __name__ == "__main__":
    main()
