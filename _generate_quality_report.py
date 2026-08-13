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
INDEX_FILE = Path(_SCRIPT_DIR) / "index.html"
PEAK_DETECTION_FILE = Path(_SCRIPT_DIR) / "_peak_detection.html"
PEAK_VERIFICATION_LOG = Path(_SCRIPT_DIR) / "_peak_verification_log.json"

# Market edge computation (BMA vs Polymarket)
try:
    from _compute_market_edge import (  # type: ignore[import-not-found]
        compute_edges, load_market_prices, load_bma_predictions,
        format_edge_html_rows, build_market_lookup, compute_bma_prob,
        compute_resolution_arbitrage, format_resolution_arbitrage_summary_html,
        split_edges_by_type, build_market_type_section_html,
        build_safe_winners_html_section,
        is_us_city, c_to_f, fmt_temp,
    )
    HAS_MARKET_EDGE = True
except ImportError:
    HAS_MARKET_EDGE = False


def _load_log() -> dict:
    """Load existing quality log or return empty structure."""
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {"runs": []}


def _tally_from_predictions(runs: list) -> dict:
    """Compute per-strategy win/loss totals from predictions (ground truth).

    The run ``summary`` can be stale (e.g. an hourly run that resolved cities
    before daily_close) so all headline W/L numbers are recomputed from the
    resolved strategy results stored in each city's prediction.
    """
    totals = {
        "sigma_wins": 0, "sigma_losses": 0,
        "p5_wins": 0, "p5_losses": 0,
        "mean_wins": 0, "mean_losses": 0,
    }
    for run in runs:
        for pdata in run.get("predictions", {}).values():
            strategies = pdata.get("strategies", {}) or {}
            for sn, win_key, loss_key in (
                ("sigma", "sigma_wins", "sigma_losses"),
                ("p5", "p5_wins", "p5_losses"),
                ("mean", "mean_wins", "mean_losses"),
            ):
                result = strategies.get(sn, {}).get("result")
                if result == "WIN":
                    totals[win_key] += 1
                elif result == "LOSS":
                    totals[loss_key] += 1
    return totals


def _pick_most_resolved_run(runs: list) -> dict:
    """Return the run with the most resolved predictions (sigma actual_peak)."""
    best = None
    best_count = -1
    for run in runs:
        count = sum(
            1 for p in run.get("predictions", {}).values()
            if p.get("strategies", {}).get("sigma", {}).get("actual_peak") is not None
        )
        if count > best_count:
            best_count = count
            best = run
    if best is None:
        return runs[-1] if runs else {}
    return best


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

    # Aggregate per-strategy stats (recomputed from predictions, not the
    # run-level summary which can go stale before daily_close).
    _tally = _tally_from_predictions(runs)
    sigma_wins = _tally["sigma_wins"]
    sigma_losses = _tally["sigma_losses"]
    p5_wins = _tally["p5_wins"]
    p5_losses = _tally["p5_losses"]
    mean_wins = _tally["mean_wins"]
    mean_losses = _tally["mean_losses"]

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
        _rt = _tally_from_predictions([run])
        sw = _rt["sigma_wins"]
        sl = _rt["sigma_losses"]
        pw = _rt["p5_wins"]
        pl = _rt["p5_losses"]
        mw = _rt["mean_wins"]
        ml = _rt["mean_losses"]
        npred = len(run.get("predictions", {}))
        if sw + sl > 0:
            sr = round(sw / (sw + sl) * 100, 1)
            lines.append(f"   {rd}  │  {npred:3d} byer  │  sigma={sw}/{sl} ({sr}%)  "
                         f"p5={pw}/{pl}  mean={mw}/{ml}")
        else:
            lines.append(f"   {rd}  │  {npred:3d} byer  │  (ingen resultater)")

    lines.append("")

    # ── Per-city strategy recommendation + Resultant Monitor ──
    best_per_city = _get_best_strategy_per_city(runs)
    resultant_lines = _build_resultant_monitor_lines(best_per_city)
    lines.extend(resultant_lines)

    lines.append("═" * 60)
    lines.append("")

    return "\n".join(lines)


# =============================================================================
# Per-City Strategy Recommendation Engine
# =============================================================================

def _get_best_strategy_per_city(runs: list) -> dict:
    """Compute per-city win rates for sigma, p5, mean across all historical runs.

    Returns:
        {city_name: {"best": "sigma", "sigma_rate": 42.0, "p5_rate": 10.0, "mean_rate": 25.0,
                     "sigma_wl": "5W/7L", "p5_wl": "1W/11L", "mean_wl": "3W/9L", "total_resolved": 12}}
    """
    city_stats: dict[str, dict] = {}
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            if city not in city_stats:
                city_stats[city] = {
                    "sigma": {"wins": 0, "losses": 0},
                    "p5": {"wins": 0, "losses": 0},
                    "mean": {"wins": 0, "losses": 0},
                }
            strategies = pdata.get("strategies", {})
            for sn in ("sigma", "p5", "mean"):
                s = strategies.get(sn, {})
                if s.get("result") == "WIN":
                    city_stats[city][sn]["wins"] += 1
                elif s.get("result") == "LOSS":
                    city_stats[city][sn]["losses"] += 1

    result: dict[str, dict] = {}
    for city, stats in city_stats.items():
        sigma_t = stats["sigma"]["wins"] + stats["sigma"]["losses"]
        p5_t = stats["p5"]["wins"] + stats["p5"]["losses"]
        mean_t = stats["mean"]["wins"] + stats["mean"]["losses"]
        total_resolved = sigma_t

        sigma_rate = round(stats["sigma"]["wins"] / max(1, sigma_t) * 100, 1)
        p5_rate = round(stats["p5"]["wins"] / max(1, p5_t) * 100, 1)
        mean_rate = round(stats["mean"]["wins"] / max(1, mean_t) * 100, 1)

        sigma_wl = f"{stats['sigma']['wins']}W/{stats['sigma']['losses']}L"
        p5_wl = f"{stats['p5']['wins']}W/{stats['p5']['losses']}L"
        mean_wl = f"{stats['mean']['wins']}W/{stats['mean']['losses']}L"

        rates = {"sigma": sigma_rate, "p5": p5_rate, "mean": mean_rate}
        if total_resolved == 0:
            best = "none"
        else:
            best = max(rates, key=lambda k: rates[k])

        result[city] = {
            "best": best,
            "sigma_rate": sigma_rate,
            "p5_rate": p5_rate,
            "mean_rate": mean_rate,
            "sigma_wl": sigma_wl,
            "p5_wl": p5_wl,
            "mean_wl": mean_wl,
            "total_resolved": total_resolved,
        }

    return result


def _build_resultant_monitor_lines(best_per_city: dict) -> list[str]:
    """Build the Resultant Monitor summary lines for the markdown report."""
    lines: list[str] = []

    if not best_per_city:
        return lines

    sigma_cities = [c for c, d in best_per_city.items() if d["best"] == "sigma"]
    mean_cities = [c for c, d in best_per_city.items() if d["best"] == "mean"]
    p5_cities = [c for c, d in best_per_city.items() if d["best"] == "p5"]

    total_with_data = len(best_per_city)

    # Calculate samplet edge if all followed recommended
    total_wins = 0
    total_positions = 0
    for city, d in best_per_city.items():
        if d["total_resolved"] > 0:
            best_key = d["best"]
            # Parse W/L for the best strategy
            wl_str = d.get(f"{best_key}_wl", "0W/0L")
            parts = wl_str.split("W/")
            wins = int(parts[0]) if parts else 0
            total_wins += wins
            total_positions += d["total_resolved"]
    edge = round(total_wins / max(1, total_positions) * 100, 1) if total_positions > 0 else 0

    lines.append("═" * 60)
    lines.append("     🎯 RESULTANT MONITOR — Per-City Optimal Strategy")
    lines.append("═" * 60)
    lines.append("")

    sigma_pct = round(len(sigma_cities) / max(1, total_with_data) * 100, 1)
    mean_pct = round(len(mean_cities) / max(1, total_with_data) * 100, 1)
    p5_pct = round(len(p5_cities) / max(1, total_with_data) * 100, 1)

    lines.append(f"Byer med Sigma som best:   {len(sigma_cities):3d} ({sigma_pct}%)")
    lines.append(f"Byer med Mean som best:    {len(mean_cities):3d} ({mean_pct}%)")
    lines.append(f"Byer med P5 som best:      {len(p5_cities):3d} ({p5_pct}%)")
    lines.append("")
    lines.append(f"Samlet edge (hvis alle fulgte anbefalt): {edge}%")
    lines.append("")

    # Top 10 cities with their recommended strategy (only resolved cities)
    cities_with_data = [(c, d) for c, d in best_per_city.items() if d["total_resolved"] > 0]
    sorted_cities = sorted(cities_with_data, key=lambda kv: kv[1][f"{kv[1]['best']}_rate"], reverse=True)
    lines.append("🏆 TOP 10 PER CITY:")
    if sorted_cities:
        for i, (city, d) in enumerate(sorted_cities[:10]):
            best_name = {"sigma": "Sigma", "p5": "P5", "mean": "Mean"}.get(d["best"], d["best"])
            rate = d[f"{d['best']}_rate"]
            lines.append(f"   {i+1:2d}. {city:<30s} → {best_name} ({rate}%) [{d['total_resolved']} resolved]")
    else:
        lines.append("   (ingen løste data ennå)")
    lines.append("")

    # Per-city detailed breakdown
    lines.append("─" * 60)
    lines.append("📊 PER-CITY STRATEGI-ANALYSE:")
    lines.append("─" * 60)
    for city, d in sorted(best_per_city.items(), key=lambda kv: kv[1]["total_resolved"], reverse=True):
        if d["total_resolved"] == 0:
            continue
        sigma_mark = " ← ANBEFALT" if d["best"] == "sigma" else ""
        p5_mark = " ← ANBEFALT" if d["best"] == "p5" else ""
        mean_mark = " ← ANBEFALT" if d["best"] == "mean" else ""
        lines.append(f"   📊 {city} — Strategi-analyse ({d['total_resolved']} resolved trades)")
        lines.append(f"      Sigma: {d['sigma_wl']} = {d['sigma_rate']}%{sigma_mark}")
        lines.append(f"      P5:    {d['p5_wl']} = {d['p5_rate']}%{p5_mark}")
        lines.append(f"      Mean:  {d['mean_wl']} = {d['mean_rate']}%{mean_mark}")
        lines.append("")
    return lines


def _build_strat_rec_cell(city: str, best_per_city: dict) -> str:
    """Build an HTML table cell showing the recommended strategy for a city."""
    info = best_per_city.get(city)
    if not info or info["total_resolved"] == 0:
        return '<span style="color: var(--text-dim);">— (ingen data)</span>'

    best_name = {"sigma": "Sigma", "p5": "P5", "mean": "Mean"}.get(info["best"], info["best"])
    rate = info[f"{info['best']}_rate"]
    emoji = {"sigma": "🎯", "p5": "🛡️", "mean": "📊"}.get(info["best"], "")

    if best_name == "Sigma":
        color = "#3fb950"
    elif best_name == "P5":
        color = "#58a6ff"
    else:
        color = "#d2991d"

    return (
        f'<span style="color:{color};font-weight:600;">'
        f'{emoji} {best_name} ({rate}%)</span>'
        f' <span style="color:var(--text-dim);font-size:0.7rem;">← BEST</span>'
    )


def _build_strat_rec_html_section(best_per_city: dict) -> str:
    """Build an HTML section showing top cities with recommended strategies."""
    if not best_per_city:
        return ""

    # Count how many cities have each best strategy
    sigma_cities = [c for c, d in best_per_city.items() if d["best"] == "sigma"]
    mean_cities = [c for c, d in best_per_city.items() if d["best"] == "mean"]
    p5_cities = [c for c, d in best_per_city.items() if d["best"] == "p5"]
    total_with_data = len(best_per_city)

    # Calculate samplet edge
    total_wins = 0
    total_positions = 0
    for city, d in best_per_city.items():
        if d["total_resolved"] > 0:
            best_key = d["best"]
            wl_str = d.get(f"{best_key}_wl", "0W/0L")
            parts = wl_str.split("W/")
            wins = int(parts[0]) if parts else 0
            total_wins += wins
            total_positions += d["total_resolved"]
    edge = round(total_wins / max(1, total_positions) * 100, 1) if total_positions > 0 else 0

    sigma_pct = round(len(sigma_cities) / max(1, total_with_data) * 100, 1)
    mean_pct = round(len(mean_cities) / max(1, total_with_data) * 100, 1)
    p5_pct = round(len(p5_cities) / max(1, total_with_data) * 100, 1)

    # Top 10 rows — only show cities with resolved data
    cities_with_data = [(c, d) for c, d in best_per_city.items() if d["total_resolved"] > 0]
    sorted_cities = sorted(cities_with_data, key=lambda kv: kv[1][f"{kv[1]['best']}_rate"], reverse=True)
    top10_rows = ""
    if sorted_cities:
        for i, (city, d) in enumerate(sorted_cities[:10]):
            best_name = {"sigma": "Sigma", "p5": "P5", "mean": "Mean"}.get(d["best"], d["best"])
            rate = d[f"{d['best']}_rate"]
            emoji = {"sigma": "🎯", "p5": "🛡️", "mean": "📊"}.get(d["best"], "")
            top10_rows += (
                f'<tr><td>{i+1}</td><td><strong>{city}</strong></td>'
                f'<td>{emoji} {best_name}</td>'
                f'<td style="font-weight:600;">{rate}%</td>'
                f'<td style="color:var(--text-dim);">{d["total_resolved"]}</td></tr>'
            )
    else:
        top10_rows = '<tr><td colspan="5" style="color:var(--text-dim);text-align:center;padding:20px;">Ingen løste data ennå — vent til daily_close kl 23:00 UTC</td></tr>'

    return f"""
    <div class="section">
      <h2>🎯 ANBEFALT STRATEGI PER BY</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Basert på historisk resolved data. Hver by får den strategien med høyest win rate.
      </p>
      <div class="card-grid" style="margin-bottom: 20px;">
        <div class="card" style="{'border: 2px solid #3fb950;' if sigma_cities else ''}">
          <div class="value" style="color: #3fb950;">{len(sigma_cities)}</div>
          <div class="label">🎯 Sigma ({sigma_pct}%)</div>
        </div>
        <div class="card" style="{'border: 2px solid #d2991d;' if mean_cities else ''}">
          <div class="value" style="color: #d2991d;">{len(mean_cities)}</div>
          <div class="label">📊 Mean ({mean_pct}%)</div>
        </div>
        <div class="card" style="{'border: 2px solid #58a6ff;' if p5_cities else ''}">
          <div class="value" style="color: #58a6ff;">{len(p5_cities)}</div>
          <div class="label">🛡️ P5 ({p5_pct}%)</div>
        </div>
        <div class="card">
          <div class="value" style="color: var(--purple);">{edge}%</div>
          <div class="label">Samlet Edge (anbefalt)</div>
        </div>
      </div>
      <h3 style="color: var(--text-dim); font-size: 0.9rem; margin-bottom: 8px;">🏆 TOP 10 — Høyest Forventet Win Rate</h3>
      <table>
        <thead><tr><th>#</th><th>By</th><th>Anbefalt Strategi</th><th>Win Rate</th><th>Resolved</th></tr></thead>
        <tbody>{top10_rows}</tbody>
      </table>
    </div>"""


# =============================================================================
# HTML Report Generator (Dark Theme Dashboard — 3-Strategy Edition)
# =============================================================================

def _build_top5_rows_html(predictions: dict, top5_cities: list[str], peak_data: dict | None = None) -> str:
    """Build HTML table rows for top 5 cities with all 3 strategy results."""
    if peak_data is None:
        peak_data = {}
    resolved_market = _load_resolved_market_outcomes()
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

        # Use pipeline peak_data first, fall back to sigma_actual from archive
        pipeline_actual = peak_data.get(city)
        actual_str = f"{pipeline_actual:.1f}°C" if isinstance(pipeline_actual, (int, float)) else (
            f"{sigma_actual:.1f}°C" if isinstance(sigma_actual, (int, float)) else "—"
        )

        # Build market peak cell from pipeline peak data or archive data
        effective_actual = pipeline_actual if isinstance(pipeline_actual, (int, float)) else sigma_actual
        market_peak_str = actual_str
        city_base = city.split(",")[0].strip()
        market_temp = resolved_market.get(city_base) or resolved_market.get(city)
        if market_temp is not None and isinstance(effective_actual, (int, float)):
            gap = round(effective_actual - market_temp, 1)
            gap_color = "#f85149" if abs(gap) > 2.0 else ("#d2991d" if abs(gap) > 1.0 else "#3fb950")
            avvik_icon = "⚠️" if abs(gap) > 2.0 else ("🟡" if abs(gap) > 1.0 else "✅")
            market_peak_str = (
                f'📡 {actual_str} | Marked: {market_temp}°C '
                f'<span style="color:{gap_color};font-weight:600;">'
                f'{avvik_icon} {gap:+.1f}°C</span>'
            )
        elif market_temp is not None:
            market_peak_str = f'📡 {actual_str} | Marked: {market_temp}°C'
        elif isinstance(pipeline_actual, (int, float)):
            market_peak_str = f'📡 {actual_str} ✅'

        # Recommendation styling
        rec_class = ""
        if rec and "HOLD" in str(rec):
            rec_class = 'style="color:#1b5e20;"'
        elif rec and "SELG" in str(rec):
            rec_class = 'style="color:#b71c1c;"'
        elif rec and "AVVENT" in str(rec):
            rec_class = 'style="color:#d2991d;"'

        sigma_cell = (
            f'<strong>{sigma_spill}°C</strong> '
            f'<span style="font-size:0.75rem;color:#8b949e;">'
            f'(k={sigma_k}, {sigma_wp*100:.0f}%)</span>'
        )

        rows += f"""
            <tr>
                <td>{i+1}</td>
                <td><strong>{city}</strong></td>
                <td>{bma_str} <span style="color:#8b949e;font-size:0.75rem;">σ={std_str}</span></td>
                <td>{sigma_cell}</td>
                <td>{p5_spill}°C</td>
                <td>{mean_spill}°C</td>
                <td>{conf_icon} {(conf*100):.0f}%</td>
                <td>{model_ct}/8</td>
                <td>{market_peak_str}</td>
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
                <td {sigma_hl}>{sigma.get('spill','?')}°C {_win_icon(sigma_result)}</td>
                <td {p5_hl}>{p5s.get('spill','?')}°C {_win_icon(p5_result)}</td>
                <td {mean_hl}>{means.get('spill','?')}°C {_win_icon(mean_result)}</td>
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
                <td>{spill}°C</td>
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
       Cities where the position lost — recommendation is to reverse.
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
                <td>{sigma_icon} {stats['sigma']['spill']}°C</td>
                <td>{p5_icon} {stats['p5']['spill']}°C</td>
                <td>{mean_icon} {stats['mean']['spill']}°C</td>
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
# Today vs Tomorrow Separation + Edge Decay
# =============================================================================

def _build_today_tomorrow_section(runs: list) -> str:
    """Build today vs tomorrow win rate comparison with edge decay.

    Iterates ALL runs, checks resolved predictions' _lead_days field,
    and computes separate win rates for today (lead_days=0) vs tomorrow (lead_days=1).
    """
    today_counts = {"sigma": {"wins": 0, "losses": 0}, "p5": {"wins": 0, "losses": 0}, "mean": {"wins": 0, "losses": 0}}
    tomorrow_counts = {"sigma": {"wins": 0, "losses": 0}, "p5": {"wins": 0, "losses": 0}, "mean": {"wins": 0, "losses": 0}}

    for run in runs:
        # Check multi_day first (richer source), fall back to flat predictions
        multi_day = run.get("predictions_multi_day", {})
        flat_preds = run.get("predictions", {})

        # Collect all prediction sources with known lead_days
        sources: list[tuple[dict, int]] = []

        if multi_day:
            day1_preds = multi_day.get("day1", {})
            day2_preds = multi_day.get("day2", {})
            # day1 is lead_days=0 (today), day2 is lead_days=1 (tomorrow)
            for city, pdata in day1_preds.items():
                sources.append((pdata, 0))
            for city, pdata in day2_preds.items():
                sources.append((pdata, 1))
        else:
            # Fallback: use flat predictions with _lead_days field
            for city, pdata in flat_preds.items():
                ld = pdata.get("_lead_days", 0)
                sources.append((pdata, ld))

        for pdata, ld in sources:
            strategies = pdata.get("strategies", {})
            for sn in ("sigma", "p5", "mean"):
                s = strategies.get(sn, {})
                result = s.get("result", "")
                if result not in ("WIN", "LOSS"):
                    continue
                bucket = today_counts if ld == 0 else tomorrow_counts
                if result == "WIN":
                    bucket[sn]["wins"] += 1
                else:
                    bucket[sn]["losses"] += 1

    def _rate(wins: int, losses: int) -> float:
        total = wins + losses
        return round(wins / max(1, total) * 100, 1)

    today_sigma_r = _rate(today_counts["sigma"]["wins"], today_counts["sigma"]["losses"])
    today_p5_r = _rate(today_counts["p5"]["wins"], today_counts["p5"]["losses"])
    today_mean_r = _rate(today_counts["mean"]["wins"], today_counts["mean"]["losses"])

    tomorrow_sigma_r = _rate(tomorrow_counts["sigma"]["wins"], tomorrow_counts["sigma"]["losses"])
    tomorrow_p5_r = _rate(tomorrow_counts["p5"]["wins"], tomorrow_counts["p5"]["losses"])
    tomorrow_mean_r = _rate(tomorrow_counts["mean"]["wins"], tomorrow_counts["mean"]["losses"])

    today_sigma_total = today_counts["sigma"]["wins"] + today_counts["sigma"]["losses"]
    tomorrow_sigma_total = tomorrow_counts["sigma"]["wins"] + tomorrow_counts["sigma"]["losses"]
    today_p5_total = today_counts["p5"]["wins"] + today_counts["p5"]["losses"]
    tomorrow_p5_total = tomorrow_counts["p5"]["wins"] + tomorrow_counts["p5"]["losses"]
    today_mean_total = today_counts["mean"]["wins"] + today_counts["mean"]["losses"]
    tomorrow_mean_total = tomorrow_counts["mean"]["wins"] + tomorrow_counts["mean"]["losses"]

    if today_sigma_total == 0 and tomorrow_sigma_total == 0:
        return ""  # No resolved data yet

    # Edge decay: how much less accurate is tomorrow vs today?
    decay_sigma = round(today_sigma_r - tomorrow_sigma_r, 1) if tomorrow_sigma_total > 0 else 0
    decay_p5 = round(today_p5_r - tomorrow_p5_r, 1) if tomorrow_p5_total > 0 else 0
    decay_mean = round(today_mean_r - tomorrow_mean_r, 1) if tomorrow_mean_total > 0 else 0

    decay_color = "#f85149" if decay_sigma > 5 else ("#d2991d" if decay_sigma > 0 else "#3fb950")

    return f"""
    <div class="section">
      <h2>📊 Today vs Tomorrow Prediction Accuracy</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Compares same-day predictions (lead_days=0) against next-day predictions (lead_days=1).
        Higher edge decay = tomorrow predictions lose more accuracy.
      </p>
      <div class="grid-2">
        <div>
          <h3 style="color: var(--green); font-size: 1rem; margin-bottom: 8px;">📊 TODAY'S PREDICTIONS (lead_days=0)</h3>
          <table>
            <thead><tr><th>Strategy</th><th>Record</th><th>Win Rate</th></tr></thead>
            <tbody>
              <tr><td><strong>🎯 Sigma</strong></td><td>{today_counts['sigma']['wins']}W/{today_counts['sigma']['losses']}L</td><td style="font-weight:600;">{today_sigma_r}%</td></tr>
              <tr><td><strong>🛡️ P5</strong></td><td>{today_counts['p5']['wins']}W/{today_counts['p5']['losses']}L</td><td style="font-weight:600;">{today_p5_r}%</td></tr>
              <tr><td><strong>📊 Mean</strong></td><td>{today_counts['mean']['wins']}W/{today_counts['mean']['losses']}L</td><td style="font-weight:600;">{today_mean_r}%</td></tr>
            </tbody>
          </table>
        </div>
        <div>
          <h3 style="color: var(--orange); font-size: 1rem; margin-bottom: 8px;">📊 TOMORROW'S PREDICTIONS (lead_days=1)</h3>
          <table>
            <thead><tr><th>Strategy</th><th>Record</th><th>Win Rate</th></tr></thead>
            <tbody>
              <tr><td><strong>🎯 Sigma</strong></td><td>{tomorrow_counts['sigma']['wins']}W/{tomorrow_counts['sigma']['losses']}L</td><td style="font-weight:600;">{tomorrow_sigma_r}%</td></tr>
              <tr><td><strong>🛡️ P5</strong></td><td>{tomorrow_counts['p5']['wins']}W/{tomorrow_counts['p5']['losses']}L</td><td style="font-weight:600;">{tomorrow_p5_r}%</td></tr>
              <tr><td><strong>📊 Mean</strong></td><td>{tomorrow_counts['mean']['wins']}W/{tomorrow_counts['mean']['losses']}L</td><td style="font-weight:600;">{tomorrow_mean_r}%</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div style="margin-top: 16px; padding: 12px; background: rgba({('248,81,73' if decay_sigma > 5 else ('210,153,29' if decay_sigma > 0 else '63,185,80'))}, 0.1); border-radius: 8px; text-align: center;">
        <span style="font-size: 1.1rem; font-weight: 700; color: {decay_color};">
          📊 EDGE DECAY: Tomorrow predictions are {abs(decay_sigma)}% {'less' if decay_sigma >= 0 else 'more'} accurate (Sigma)
        </span>
        <br/><span style="color: var(--text-dim); font-size: 0.8rem;">
          P5 decay: {decay_p5}% | Mean decay: {decay_mean}%
        </span>
      </div>
    </div>"""


# =============================================================================
# Edge-Maximizing Metric Helpers
# =============================================================================

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


def _build_city_win_rate_section(runs: list) -> str:
   """Section 1: Win Rate Per City sorted by win rate (sigma strategy)."""
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

   city_rates: list[tuple] = []
   for city, wl in city_wl.items():
       total = wl["wins"] + wl["losses"]
       if total >= 2:
           rate = round(wl["wins"] / total * 100, 1)
           city_rates.append((city, wl["wins"], wl["losses"], rate, total))

   city_rates.sort(key=lambda x: x[3], reverse=True)
   if not city_rates:
       return ""

   best_rows = ""
   for i, (city, w, l, rate, total) in enumerate(city_rates[:10]):
       icon = "🥇" if i == 0 else ("🥈" if i == 1 else ("🥉" if i == 2 else f"{i+1}."))
       rate_color = "#3fb950" if rate >= 60 else ("#d2991d" if rate >= 40 else "#f85149")
       best_rows += (
           f'<tr><td>{icon}</td><td><strong>{city}</strong></td>'
           f'<td>{w}W/{l}L</td>'
           f'<td style="color:{rate_color};font-weight:600;">{rate}%</td></tr>'
       )

   worst = city_rates[-10:] if len(city_rates) >= 10 else []
   worst_rows = ""
   for i, (city, w, l, rate, total) in enumerate(reversed(worst)):
       rank = len(city_rates) - len(worst) + i + 1
       rate_color = "#f85149" if rate < 40 else "#d2991d"
       worst_rows += (
           f'<tr><td>{rank}.</td><td><strong>{city}</strong></td>'
           f'<td>{w}W/{l}L</td>'
           f'<td style="color:{rate_color};font-weight:600;">{rate}%</td></tr>'
       )

   return f"""
   <div class="section">
     <h2>🏆 Win Rate Per City — Sigma Strategy</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Sorted by sigma strategy win rate. Only cities with ≥2 resolved predictions shown.
     </p>
     <div class="grid-2">
       <div>
         <h3 style="color: var(--green); font-size: 1rem; margin-bottom: 8px;">🏆 BESTE BYER</h3>
         <table>
           <thead><tr><th>#</th><th>City</th><th>Record</th><th>Win Rate</th></tr></thead>
           <tbody>{best_rows}</tbody>
         </table>
       </div>
       <div>
         <h3 style="color: var(--red); font-size: 1rem; margin-bottom: 8px;">📉 SVESTE BYER</h3>
         <table>
           <thead><tr><th>#</th><th>City</th><th>Record</th><th>Win Rate</th></tr></thead>
           <tbody>{worst_rows}</tbody>
         </table>
       </div>
     </div>
   </div>"""


def _build_model_agreement_section(runs: list) -> str:
   """Section 4: Model Agreement vs Win Rate."""
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

   rows = ""
   for label, t in tiers.items():
       losses = t["pos"] - t["wins"]
       wr = round(t["wins"] / max(1, t["pos"]) * 100, 1) if t["pos"] > 0 else 0
       rows += (
           f'<tr><td><strong>{label}</strong></td>'
           f'<td>{t["pos"]}</td><td>{t["wins"]}W/{losses}L</td>'
           f'<td>{wr}%</td></tr>'
       )

   if not rows:
       return ""

   return f"""
   <div class="section">
     <h2>📊 Model Agreement & Win Rate</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Does higher model consensus lead to better predictions?
     </p>
     <table>
       <thead><tr><th>Agreement</th><th>Positions</th><th>Record</th><th>Win Rate</th></tr></thead>
       <tbody>{rows}</tbody>
     </table>
   </div>"""


def _build_range_accuracy_section(runs: list) -> str:
   """Section 5: P5-P95 Range Size vs Accuracy."""
   tier_defs = [
       ("Smal (<2°C)", 0, 2),
       ("Medium (2-4°C)", 2, 4),
       ("Bred (>4°C)", 4, 999),
   ]
   tier_data: list[dict] = []
   for label, lo, hi in tier_defs:
       tier_data.append({"label": label, "pos": 0, "wins": 0})

   for run in runs:
       for pdata in run.get("predictions", {}).values():
           sigma = pdata.get("strategies", {}).get("sigma", {})
           result = sigma.get("result", "")
           if result not in ("WIN", "LOSS"):
               continue
           p5 = pdata.get("p5", 0)
           p95 = pdata.get("p95", 0)
           rng = abs(p95 - p5) if p95 and p5 else 2.0
           for i, (label, lo, hi) in enumerate(tier_defs):
               if lo <= rng < hi:
                   tier_data[i]["pos"] += 1
                   if result == "WIN":
                       tier_data[i]["wins"] += 1
                   break

   rows = ""
   for td in tier_data:
       losses = td["pos"] - td["wins"]
       wr = round(td["wins"] / max(1, td["pos"]) * 100, 1) if td["pos"] > 0 else 0
       rows += (
           f'<tr><td><strong>{td["label"]}</strong></td>'
           f'<td>{td["pos"]}</td><td>{td["wins"]}W/{losses}L</td>'
           f'<td>{wr}%</td></tr>'
       )

   if not rows:
       return ""

   return f"""
   <div class="section">
     <h2>📏 P5-P95 Range & Accuracy</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Does ensemble spread predict accuracy? Smaller range = higher model agreement.
     </p>
     <table>
       <thead><tr><th>Range Size</th><th>Positions</th><th>Record</th><th>Win Rate</th></tr></thead>
       <tbody>{rows}</tbody>
     </table>
   </div>"""


def _build_optimal_strategy_by_confidence_section(runs: list) -> str:
   """Section 6: Best Strategy PER Confidence Level."""
   conf_tiers = [
       (">80%", 0.8, 1.0, "🟢"),
       ("70-80%", 0.7, 0.8, "🟠"),
       ("60-70%", 0.6, 0.7, "🟡"),
       ("50-60%", 0.5, 0.6, "🔴"),
       ("<50%", 0.0, 0.5, "🔴"),
   ]

   results: list[dict] = []
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
           for i, (label, lo, hi, icon) in enumerate(conf_tiers):
               if lo <= conf < hi or (hi == 1.0 and conf >= 0.8):
                   for sn in ("sigma", "p5", "mean"):
                       s = strategies.get(sn, {})
                       r = s.get("result", "")
                       if r in ("WIN", "LOSS"):
                           results[i][f"{sn}_pos"] += 1
                           if r == "WIN":
                               results[i][f"{sn}_wins"] += 1
                   break

   rows = ""
   for r in results:
       sigma_wr = round(r["sigma_wins"] / max(1, r["sigma_pos"]) * 100, 1) if r["sigma_pos"] > 0 else 0
       p5_wr = round(r["p5_wins"] / max(1, r["p5_pos"]) * 100, 1) if r["p5_pos"] > 0 else 0
       mean_wr = round(r["mean_wins"] / max(1, r["mean_pos"]) * 100, 1) if r["mean_pos"] > 0 else 0

       best_name = "Sigma"
       best_rate = sigma_wr
       if p5_wr > best_rate:
           best_name = "P5"
           best_rate = p5_wr
       if mean_wr > best_rate:
           best_name = "Mean"
           best_rate = mean_wr

       def _hl(val, is_best):
           if is_best:
               return f'<span style="color:#3fb950;font-weight:600;">{val}%</span>'
           return f'{val}%'

       rows += (
           f'<tr><td>{r["icon"]} {r["label"]}</td>'
           f'<td>{_hl(sigma_wr, best_name == "Sigma")}</td>'
           f'<td>{_hl(p5_wr, best_name == "P5")}</td>'
           f'<td>{_hl(mean_wr, best_name == "Mean")}</td>'
           f'<td><span class="best-strategy">{best_name} ({best_rate}%)</span></td></tr>'
       )

   if not rows:
       return ""

   return f"""
   <div class="section">
     <h2>📊 Optimal Strategy by Confidence Level</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Which strategy performs best at each confidence tier? Green = best.
     </p>
     <table>
       <thead><tr><th>Tier</th><th>🎯 Sigma</th><th>🛡️ P5</th><th>📊 Mean</th><th>🏆 Best</th></tr></thead>
       <tbody>{rows}</tbody>
     </table>
   </div>"""


def _build_cumulative_edge_section(runs: list) -> str:
   """Section 7: Cumulative Edge Tracker — betting $100 per sigma recommendation."""
   days_data: list[dict] = []
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
       return ""

   cum = 0
   rows = ""
   for d in days_data[-14:]:
       cum += d["edge"]
       color = "#3fb950" if d["edge"] >= 0 else "#f85149"
       cum_color = "#3fb950" if cum >= 0 else "#f85149"
       rows += (
           f'<tr><td>{d["date"]}</td>'
           f'<td>{d["wins"]}W/{d["losses"]}L</td>'
           f'<td style="color:{color};font-weight:600;">{d["edge"]:+d} units</td>'
           f'<td style="color:{cum_color};font-weight:600;">{cum:+d} units</td></tr>'
       )

   return f"""
   <div class="section">
     <h2>📈 Cumulative Edge Tracker</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Simulates betting $100 on each sigma-recommended position.
       Wins return $139 (odds ~1.39). Last 14 days shown.
     </p>
     <table>
       <thead><tr><th>Date</th><th>Sigma Record</th><th>Daily Edge</th><th>Cumulative</th></tr></thead>
       <tbody>{rows}</tbody>
     </table>
   </div>"""


def _build_region_performance_section(runs: list) -> str:
   """Section 8: Timezone/Region Performance."""
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

   rows = ""
   for region, data in sorted_regions:
       wr = round(data["wins"] / max(1, data["pos"]) * 100, 1) if data["pos"] > 0 else 0
       color = "#3fb950" if wr >= 50 else ("#d2991d" if wr >= 40 else "#f85149")
       rows += (
           f'<tr><td><strong>{region}</strong></td>'
           f'<td>{data["pos"]}</td><td>{data["wins"]}W/{data["pos"] - data["wins"]}L</td>'
           f'<td style="color:{color};font-weight:600;">{wr}%</td></tr>'
       )

   if not rows:
       return ""

   return f"""
   <div class="section">
     <h2>🌍 Region Performance</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Sigma strategy win rate by geographic region (derived from timezone).
     </p>
     <table>
       <thead><tr><th>Region</th><th>Positions</th><th>Record</th><th>Win Rate</th></tr></thead>
       <tbody>{rows}</tbody>
     </table>
   </div>"""


def _build_uhi_accuracy_section(runs: list) -> str:
   """Section 9: UHI Adjustment Accuracy."""
   high_uhi_errors: list[float] = []
   low_uhi_errors: list[float] = []

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

   return f"""
   <div class="section">
     <h2>🏙️ UHI Adjustment Accuracy</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Average |BMA mean − actual peak| error by Urban Heat Island severity.
     </p>
     <table>
       <thead><tr><th>UHI Category</th><th>Count</th><th>Avg BMA Error</th></tr></thead>
       <tbody>
         <tr><td><strong>High UHI cities (≥1.0°C)</strong></td><td>{len(high_uhi_errors)}</td><td>{avg_high}°C</td></tr>
         <tr><td><strong>Low UHI cities (≤0.5°C)</strong></td><td>{len(low_uhi_errors)}</td><td>{avg_low}°C</td></tr>
       </tbody>
     </table>
   </div>"""


# =============================================================================
# Edge Validation Section — A/B test each feature improvement
# =============================================================================

def _impact_label_html(impact: float) -> str:
    """Return HTML badge for an edge impact percentage."""
    if impact > 3:
        return '<span style="color:#3fb950;font-weight:700;">✅ REAL EDGE</span>'
    elif impact >= 1:
        return '<span style="color:#d2991d;font-weight:700;">🟡 MARGINAL</span>'
    else:
        return '<span style="color:#f85149;font-weight:700;">🔴 IMAGINED / NOISE</span>'


def _build_edge_validation_html_section(runs: list) -> str:
    """Build HTML section showing edge impact analysis — Real vs Imagined.

    Compares feature-on vs feature-off win rates across all resolved predictions.
    """
    # Collect all resolved predictions
    all_preds: list[dict] = []
    for run in runs:
        for city, pdata in run.get("predictions", {}).items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            result = sigma.get("result", "")
            if result in ("WIN", "LOSS"):
                all_preds.append({
                    "city": city,
                    "result": result,
                    "models": pdata.get("models", 0),
                    "spread": abs(pdata.get("p95", 0) - pdata.get("p5", 0)),
                    "k": pdata.get("strategies", {}).get("sigma", {}).get("k", 0.5),
                    "uhi": pdata.get("_uhi_adjustment", 0),
                    "confidence": pdata.get("confidence", 0),
                })

    if len(all_preds) < 5:
        return """<div class="section">
      <h2>📊 EDGE VALIDATION — Real vs Imagined</h2>
      <p style="color: var(--text-dim);">Too few resolved predictions (<5) for edge impact analysis.</p>
    </div>"""

    def _rate(group: list[dict]) -> tuple[int, int, float]:
        wins = sum(1 for p in group if p["result"] == "WIN")
        losses = len(group) - wins
        rate = round(wins / max(1, len(group)) * 100, 1)
        return wins, losses, rate

    sections: list[str] = []

    # ── 1. MODEL WEIGHTING ──
    high_weight = [p for p in all_preds if p["models"] >= 8]
    low_weight = [p for p in all_preds if p["models"] <= 4]
    if high_weight and low_weight:
        hw_w, hw_l, hw_rate = _rate(high_weight)
        lw_w, lw_l, lw_rate = _rate(low_weight)
        impact = round(hw_rate - lw_rate, 1)
        sections.append(f"""<tr>
            <td><strong>🔬 Model Weighting</strong></td>
            <td>{hw_w}W/{hw_l}L = {hw_rate}%</td>
            <td>{lw_w}W/{lw_l}L = {lw_rate}%</td>
            <td>≥8 models vs ≤4</td>
            <td style="text-align:right;">{impact:+.1f}%</td>
            <td>{_impact_label_html(impact)}</td>
        </tr>""")

    # ── 2. SPREAD FILTERING ──
    narrow = [p for p in all_preds if p["spread"] < 2.0]
    if narrow:
        nw_w, nw_l, nw_rate = _rate(narrow)
        all_w, all_l, all_rate = _rate(all_preds)
        impact = round(nw_rate - all_rate, 1)
        sections.append(f"""<tr>
            <td><strong>📏 Spread Filtering</strong></td>
            <td>{nw_w}W/{nw_l}L = {nw_rate}%</td>
            <td>{all_w}W/{all_l}L = {all_rate}%</td>
            <td>Narrow (<2°C) vs All</td>
            <td style="text-align:right;">{impact:+.1f}%</td>
            <td>{_impact_label_html(impact)}</td>
        </tr>""")

    # ── 3. DYNAMIC k ──
    high_k = [p for p in all_preds if p["k"] > 0.5]
    low_k = [p for p in all_preds if p["k"] <= 0.5]
    if high_k and low_k:
        hk_w, hk_l, hk_rate = _rate(high_k)
        lk_w, lk_l, lk_rate = _rate(low_k)
        impact = round(hk_rate - lk_rate, 1)
        sections.append(f"""<tr>
            <td><strong>🎯 Dynamic k</strong></td>
            <td>{hk_w}W/{hk_l}L = {hk_rate}%</td>
            <td>{lk_w}W/{lk_l}L = {lk_rate}%</td>
            <td>k>0.5 vs k≤0.5</td>
            <td style="text-align:right;">{impact:+.1f}%</td>
            <td>{_impact_label_html(impact)}</td>
        </tr>""")

    # ── 4. UHI ADJUSTMENT ──
    uhi_yes = [p for p in all_preds if p["uhi"] > 0.5]
    uhi_no = [p for p in all_preds if p["uhi"] <= 0.5]
    if uhi_yes and uhi_no:
        uy_w, uy_l, uy_rate = _rate(uhi_yes)
        un_w, un_l, un_rate = _rate(uhi_no)
        impact = round(uy_rate - un_rate, 1)
        sections.append(f"""<tr>
            <td><strong>🏙️ UHI Adjustment</strong></td>
            <td>{uy_w}W/{uy_l}L = {uy_rate}%</td>
            <td>{un_w}W/{un_l}L = {un_rate}%</td>
            <td>UHI≥0.5°C vs <0.5°C</td>
            <td style="text-align:right;">{impact:+.1f}%</td>
            <td>{_impact_label_html(impact)}</td>
        </tr>""")

    # ── 5. BEST FEATURE COMBOS ──
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
            parts.append("Narrow")
        if has_dyn_k:
            parts.append("Dyn k")
        if has_uhi:
            parts.append("UHI")
        combo_key = " + ".join(parts) if parts else "Baseline"
        if combo_key not in combos:
            combos[combo_key] = {"wins": 0, "total": 0}
        combos[combo_key]["total"] += 1
        if p["result"] == "WIN":
            combos[combo_key]["wins"] += 1

    combo_rows = ""
    sorted_combos = sorted(combos.items(),
                           key=lambda x: x[1]["wins"] / max(1, x[1]["total"]),
                           reverse=True)
    for label, stats in sorted_combos:
        rate = round(stats["wins"] / max(1, stats["total"]) * 100, 1)
        rate_color = "#3fb950" if rate >= 50 else ("#d2991d" if rate >= 40 else "#f85149")
        combo_rows += (
            f'<tr><td><strong>{label}</strong></td>'
            f'<td>{stats["wins"]}W/{stats["total"] - stats["wins"]}L</td>'
            f'<td style="color:{rate_color};font-weight:600;">{rate}%</td></tr>'
        )

    impact_rows = "\n".join(sections)

    return f"""
    <div class="section">
      <h2>📊 EDGE VALIDATION — Real vs Imagined</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        A/B-testing each feature improvement against resolved predictions.
        🟢 Impact >3% = REAL EDGE | 🟡 1–3% = MARGINAL | 🔴 <1% = IMAGINED / NOISE.
      </p>
      <table>
        <thead><tr>
          <th>Feature</th><th>Feature ON</th><th>Feature OFF</th><th>Comparison</th><th style="text-align:right;">Δ Impact</th><th>Verdict</th>
        </tr></thead>
        <tbody>
        {impact_rows if impact_rows else '<tr><td colspan="6" style="color: var(--text-dim);">No feature comparisons available yet.</td></tr>'}
        </tbody>
      </table>
      <h3 style="color: var(--purple); margin-top: 20px; font-size: 1rem;">🏆 BEST FEATURE COMBOS</h3>
      <table>
        <thead><tr><th>Combo</th><th>Record</th><th>Win Rate</th></tr></thead>
        <tbody>{combo_rows if combo_rows else '<tr><td colspan="3" style="color: var(--text-dim);">—</td></tr>'}</tbody>
      </table>
    </div>"""


# =============================================================================
# Arbitrage Stats Section — Win/Loss Tracking
# =============================================================================

def _build_arbitrage_stats_html_section(runs: list) -> str:
    """Build HTML section showing arbitrage win/loss stats (SHORT vs BUY)."""
    # Aggregate arbitrage stats from all runs
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

    if total_all == 0:
        return ""

    short_rate = round(short_wins / max(1, short_total) * 100, 1)
    buy_rate = round(buy_wins / max(1, buy_total) * 100, 1)
    total_rate = round(total_wins / max(1, total_all) * 100, 1)

    return f"""
    <div class="section" style="border-color: rgba(210,153,29,0.3);">
      <h2>💰 ARBITRAGE STATS — Win/Loss Tracking</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Separate tracking of arbitrage-flip outcomes. SHORT = bet against losing sigma position.
        BUY = bet on winning bucket after peak confirmed.
      </p>
      <div class="card-grid" style="margin-bottom: 20px;">
        <div class="card" style="border: 2px solid var(--red);">
          <div class="value" style="color: var(--red);">{short_rate}%</div>
          <div class="label">🔴 SHORT<br/>{short_wins}W/{short_losses}L</div>
        </div>
        <div class="card" style="border: 2px solid var(--green);">
          <div class="value" style="color: var(--green);">{buy_rate}%</div>
          <div class="label">🟢 BUY<br/>{buy_wins}W/{buy_losses}L</div>
        </div>
        <div class="card">
          <div class="value" style="color: var(--orange);">{total_rate}%</div>
          <div class="label">💰 Total Arbitrage<br/>{total_wins}W/{total_losses}L</div>
        </div>
      </div>
    </div>"""


def _build_madrid_arbitrage_highlight(runs: list) -> str:
    """Build a prominent Madrid arbitrage callout if actual_peak≈36.7°C.

    Checks the latest run for Madrid, ES with actual_peak data and highlights
    the arbitrage opportunity relative to the market line.
    """
    if not runs:
        return ""

    latest = runs[-1]
    preds = latest.get("predictions", {})
    madrid = preds.get("Madrid, ES")
    if not madrid:
        return ""

    strategies = madrid.get("strategies", {})
    sigma = strategies.get("sigma", {})
    actual_peak = sigma.get("actual_peak")
    if actual_peak is None:
        return ""

    spill = sigma.get("spill", 0)
    sigma_result = sigma.get("result", "")
    peak_time = madrid.get("peak_detected_at", "")
    bma_mean = madrid.get("bma_mean", "--")

    # Parse confirmed time
    confirmed_hour = ""
    if peak_time:
        try:
            dt = datetime.fromisoformat(peak_time)
            confirmed_hour = dt.strftime("%H:%M")
        except (ValueError, TypeError):
            pass

    time_str = f" (bekreftet {confirmed_hour})" if confirmed_hour else ""

    # Check if there's an arbitrage opportunity
    actual_rounded = round(actual_peak)
    if actual_rounded != spill:
        # Sigma lost — SHORT opportunity
        arb_action = "SHORT"
        arb_color = "#f85149"
        arb_icon = "🔴"
        rec_text = f"SELG {spill}°C → SHORT! Peak ble {actual_peak:.1f}°C (round={actual_rounded} ≠ spill={spill})"
    else:
        arb_action = "WIN"
        arb_color = "#3fb950"
        arb_icon = "✅"
        rec_text = f"✅ HOLD — Peak {actual_peak:.1f}°C traff spill {spill}°C"

    return f"""
    <div class="section" style="border-color: rgba(210,153,29,0.5); box-shadow: 0 0 20px rgba(210,153,29,0.1);">
      <h2>💰 MADRID ARBITRASJE — Peak Detected!</h2>
      <div style="display: flex; align-items: center; gap: 20px; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 280px;">
          <div style="font-size: 2.5rem; font-weight: 800; color: {arb_color};">
            {arb_icon} {actual_peak:.1f}°C
          </div>
          <div style="font-size: 1.2rem; font-weight: 700; margin-bottom: 8px;">
            Madrid, ES — FAKTISK PEAK{time_str}
          </div>
          <div style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 4px;">
            BMA predikert: {bma_mean}°C | Spill (sigma): {spill}°C
          </div>
          <div style="font-size: 0.9rem; font-weight: 600; color: {arb_color}; margin-top: 8px;">
            {rec_text}
          </div>
        </div>
        <div style="flex: 0 0 auto; text-align: center; padding: 12px 20px; background: rgba(210,153,29,0.1); border-radius: 12px;">
          <div style="font-size: 0.75rem; color: var(--text-dim); text-transform: uppercase; letter-spacing: 1px;">Marked Linje</div>
          <div style="font-size: 1.5rem; font-weight: 800; color: var(--orange);">37°C</div>
          <div style="font-size: 0.8rem; color: var(--text-dim); margin-top: 4px;">Sjekk Polymarket!</div>
        </div>
      </div>
      <p style="color: var(--text-dim); font-size: 0.8rem; margin-top: 12px;">
        📡 Peak bekreftet via archive data. Arbitrasje-vindu: markedet kan fremdeles trade feil side av denne temperaturen.
      </p>
    </div>"""


# =============================================================================
# Resolution Arbitrage Section — Post-Peak Market Scanner
# =============================================================================

def _build_resolution_arbitrage_html_section() -> str:
    """Build HTML section showing resolution arbitrage opportunities."""
    if not HAS_MARKET_EDGE:
        return ""

    try:
        opportunities = compute_resolution_arbitrage()
    except Exception:
        return ""

    return format_resolution_arbitrage_summary_html(opportunities)


# =============================================================================
# Market Edge Section — BMA vs Polymarket
# =============================================================================

def _build_market_edge_html_section(peak_data: dict | None = None) -> str:
   """Build HTML section showing BMA vs Polymarket — separate highest/lowest tables.

   Each market type gets its own section with title:
       🔺 HØYESTE TEMPERATUR
       🔻 LAVESTE TEMPERATUR
   """
   if peak_data is None:
       peak_data = {}
   if not HAS_MARKET_EDGE:
       return ""

   try:
       market_opps, _ = load_market_prices()
       bma_preds = load_bma_predictions()
       edges = compute_edges(market_opps, bma_preds, min_vol=0)
   except Exception:
       return ""

   if not edges:
       return """<div class="section">
     <h2>📊 MARKEDSSAMMENLIGNING — BMA vs Polymarket</h2>
     <p style="color: var(--text-dim);">Ingen matchende markeder funnet. Kjør <code>python _fetch_market_prices.py</code> for å hente markedspriser.</p>
   </div>"""

   # Split by market type
   highest, lowest, other = split_edges_by_type(edges)

   sections: list[str] = []

   # Summary cards
   sections.append(f"""
   <div class="section">
     <h2>📊 MARKEDSSAMMENLIGNING — BMA vs Polymarket</h2>
     <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
       Sammenligner BMA-ensemblets prediksjoner mot dagens Polymarket-priser.
       Markeder er separert i høyeste og laveste temperatur.
       Sortert etter BMA-konfidens (høyest først). Ingen trading-signaler — ren data.
     </p>
     <div class="card-grid" style="margin-bottom: 20px;">
       <div class="card">
         <div class="value" style="color: var(--blue);">{len(edges)}</div>
         <div class="label">Totale Markeder Matchet</div>
       </div>
       <div class="card">
         <div class="value" style="color: var(--red);">{len(highest)}</div>
         <div class="label">🔺 Høyeste</div>
       </div>
       <div class="card">
         <div class="value" style="color: var(--blue);">{len(lowest)}</div>
         <div class="label">🔻 Laveste</div>
       </div>
       <div class="card">
         <div class="value" style="color: var(--purple);">{len(bma_preds)}</div>
         <div class="label">Byer med BMA-data</div>
       </div>
     </div>
   </div>""")

   # Highest temperature section
   if highest:
       sections.append(build_market_type_section_html(
           highest, "HØYESTE TEMPERATUR", "🔺",
           "rgba(248,81,73,0.3)", n_show=20, peak_data=peak_data
       ))

   # Lowest temperature section
   if lowest:
       sections.append(build_market_type_section_html(
           lowest, "LAVESTE TEMPERATUR", "🔻",
           "rgba(88,166,255,0.3)", n_show=20, peak_data=peak_data
       ))

   if other:
       sections.append(build_market_type_section_html(
           other, "ANDRE TEMPERATURMARKEDER", "📊",
           "rgba(188,140,255,0.3)", n_show=20, peak_data=peak_data
       ))

   # Safe Winners section (near-resolved markets)
   safe_winners_section = build_safe_winners_html_section(edges)
   if safe_winners_section:
       sections.insert(0, safe_winners_section)  # Show first for visibility

   return "\n".join(sections)


# =============================================================================
# Live Temperature Fetch — Shared JavaScript helpers
# =============================================================================

def _load_resolved_market_outcomes() -> dict[str, int]:
    """Extract resolved market outcomes from _market_prices.json.
    
    Returns: {city_name: resolved_temperature_celsius}
    
    A market is "resolved" when any outcome has price > 0.99.
    The resolved temperature is extracted from the question text
    (e.g., "Will the highest temperature in Hong Kong be 35°C on...").
    """
    import re
    resolved: dict[str, int] = {}
    market_path = Path(_SCRIPT_DIR) / "_market_prices.json"
    if not market_path.exists():
        return resolved
    try:
        mp = json.loads(market_path.read_text(encoding="utf-8"))
        markets = mp if isinstance(mp, list) else mp.get("markets", [])
    except Exception:
        return resolved
    
    for m in markets:
        city = m.get("city", "")
        if not city or city == "Unknown":
            continue
        question = m.get("question", "")
        outcomes = m.get("outcomes", [])
        if m.get("question_type") != "highest":
            continue

        # Extract date from question for date-matched comparison
        date_str = None
        months_full = ["January","February","March","April","May","June",
                       "July","August","September","October","November","December"]
        months_abbr = ["Jan","Feb","Mar","Apr","May","Jun",
                       "Jul","Aug","Sep","Oct","Nov","Dec"]
        month_map = {}
        for i, m in enumerate(months_full):
            month_map[m.lower()] = i + 1
            month_map[months_abbr[i].lower()] = i + 1
        month_pattern = "|".join(months_full + months_abbr)
        dm = re.search(rf'({month_pattern})\s+(\d{{1,2}})(?:,\s*(\d{{4}}))?', question, re.IGNORECASE)
        if dm:
            month = month_map.get(dm.group(1).lower(), 1)
            day = int(dm.group(2))
            year = int(dm.group(3)) if dm.group(3) else 2026
            date_str = f"{year:04d}-{month:02d}-{day:02d}"
        else:
            dm2 = re.search(r'(\d{4})-(\d{2})-(\d{2})', question)
            if dm2:
                date_str = f"{dm2.group(1)}-{dm2.group(2)}-{dm2.group(3)}"

        # Check if any outcome is >0.99 (resolved YES)
        resolved_temp = None
        for o in outcomes:
            _price = o.get("price", 0)
            if _price is None:
                _price = 0.0
            if float(_price) > 0.99 and (o.get("label") or "").lower() == "yes":
                match = re.search(r'(\d+)°C', question)
                if match:
                    resolved_temp = int(match.group(1))
                break

        if resolved_temp is not None:
            # Key by (city, date) to prevent cross-date mismatches
            if date_str:
                key = f"{city}_{date_str}"
                if key not in resolved:
                    resolved[key] = resolved_temp
            # Also store by city alone for backward compat
            if city not in resolved:
                resolved[city] = resolved_temp

    return resolved


def _load_peak_verification_data() -> dict:
    """Load peak verification log data. Returns {city_name: entry, ...}."""
    if PEAK_VERIFICATION_LOG.exists():
        try:
            pv = json.loads(PEAK_VERIFICATION_LOG.read_text(encoding="utf-8"))
            return pv.get("verifications", {})
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _build_peak_verification_html_section() -> str:
    """Build HTML section: PEAK VERIFICATION - Var vs Polymarket."""
    verifications = _load_peak_verification_data()
    if not verifications:
        return ""

    rows = ""
    for city, v in sorted(verifications.items()):
        our_peak = v.get("our_peak", "?")
        market = v.get("market_resolved", "?")
        market_display = v.get("market_display") or v.get("bucket")
        gap = v.get("gap", 0)
        verdict = v.get("verdict", "?")
        unit = (v.get("unit") or "C").upper()
        unit_suffix = "°F" if unit == "F" else "°C"

        if verdict == "OK":
            status_icon = "OK"
            status_color = "#3fb950"
        elif verdict == "MINOR":
            status_icon = "MINOR"
            status_color = "#d2991d"
        elif verdict == "THRESHOLD_MARKET":
            status_icon = "THRESHOLD_MARKET"
            status_color = "#8b949e"
        else:
            status_icon = "STASJONSFEIL"
            status_color = "#f85149"

        our_str = f"{our_peak}{unit_suffix}" if isinstance(our_peak, (int, float)) else str(our_peak)
        if market_display:
            market_str = str(market_display)
        else:
            market_str = f"{market}{unit_suffix}" if isinstance(market, (int, float)) else str(market)
        gap_str = f"{gap:+.1f}{unit_suffix}" if isinstance(gap, (int, float)) else str(gap)

        rows += f"""<tr>
            <td><strong>{city}</strong></td>
            <td>{our_str}</td>
            <td>{market_str}</td>
            <td style="color:{status_color};font-weight:600;">{gap_str}</td>
            <td style="color:{status_color};font-weight:700;">{status_icon}</td>
        </tr>"""

    ok_count = sum(1 for v in verifications.values() if v.get("verdict") == "OK")
    minor_count = sum(1 for v in verifications.values() if v.get("verdict") == "MINOR")
    mismatch_count = sum(1 for v in verifications.values() if v.get("verdict") == "STATION_MISMATCH")

    return f"""
    <div class="section" style="border-color: rgba(188, 140, 255, 0.3);">
      <h2>PEAK VERIFICATION - Var vs Polymarket</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Cross-referencing our archive peak (Open-Meteo) against Polymarket resolved outcomes.
        OK = within 1.0°C | MINOR = 1.0–2.0°C (edge-affecting) | STASJONSFEIL = >2.0°C (likely wrong station). US (°F) markets are shown in °F.
      </p>
      <div class="card-grid" style="margin-bottom: 16px;">
        <div class="card" style="border: 1px solid #3fb950;">
          <div class="value" style="color: #3fb950;">{ok_count}</div>
          <div class="label">OK</div>
        </div>
        <div class="card" style="border: 1px solid #d2991d;">
          <div class="value" style="color: #d2991d;">{minor_count}</div>
          <div class="label">MINOR</div>
        </div>
        <div class="card" style="border: 1px solid #f85149;">
          <div class="value" style="color: #f85149;">{mismatch_count}</div>
          <div class="label">STASJONSFEIL</div>
        </div>
      </div>
      <table>
        <thead><tr><th>By</th><th>Var Peak</th><th>Marked</th><th>Gap</th><th>Status</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
    </div>"""


def _build_pm_strategy_html_section() -> str:
    """Build HTML section: ANBEFALT SPILL vs POLYMARKET (generated by _pm_strat_results.py)."""
    pm_section_path = Path(_SCRIPT_DIR) / "_pm_strat_section.html"
    if pm_section_path.exists():
        try:
            return pm_section_path.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    return ""


def _load_market_city_set() -> tuple[set[str], set[str], set[str]]:
    """Return (active_cities, resolved_cities, today_market_cities) from _market_prices.json."""
    active: set[str] = set()
    resolved: set[str] = set()
    today_market_cities: set[str] = set()
    market_path = Path(_SCRIPT_DIR) / "_market_prices.json"
    today_str = str(date.today())
    if market_path.exists():
        try:
            mp = json.loads(market_path.read_text(encoding="utf-8"))
            markets = mp if isinstance(mp, list) else mp.get("markets", [])
            for m in markets:
                city = m.get("city", "")
                if not city or city == "Unknown":
                    continue
                # Check if any outcome is >99% (resolved) or >0% (active)
                outcomes = m.get("outcomes", [])
                is_resolved = any((o.get("price") or 0) > 0.99 for o in outcomes)
                has_volume = m.get("volume", 0) > 0
                if has_volume:
                    active.add(city)
                if is_resolved:
                    resolved.add(city)
                # Track cities with today's markets
                if m.get('date') == today_str:
                    today_market_cities.add(city)
        except Exception:
            pass
    return active, resolved, today_market_cities


def _build_cities_js_array() -> str:
    """Build a JavaScript array of city coordinates from the defaults JSON.
    
    Includes has_market and is_resolved flags derived from _market_prices.json.
    """
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    active_markets, resolved_markets, today_market_cities = _load_market_city_set()
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
                    city_base = name.split(",")[0].strip()
                    # Only include cities with today's active markets
                    is_today = (
                        name in today_market_cities
                        or city_base in today_market_cities
                    )
                    if not is_today:
                        continue
                    pw = PEAK_WINDOWS.get(name, (14, 17))
                    has_mkt = (
                        name in active_markets
                        or city_base in active_markets
                    )
                    is_res = (
                        name in resolved_markets
                        or city_base in resolved_markets
                    )
                    has_mkt_js = "true" if has_mkt else "false"
                    is_res_js = "true" if is_res else "false"
                    entries.append(
                        f'  {{name: "{name}", lat: {lat}, lon: {lon}, tz: "{tz}", '
                        f'peakStart: {pw[0]}, peakEnd: {pw[1]}, '
                        f'has_market: {has_mkt_js}, is_resolved: {is_res_js}}}'
                    )
        except Exception:
            pass
    return "const CITIES = [\n" + ",\n".join(entries) + "\n];"


def _build_sparkline_data_js(city_table: dict, lead: int = 0) -> str:
    """Build JS object mapping city name -> {{lat, lon, bma_mean}} for sparklines."""
    entries: list[str] = []
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    city_meta: dict[str, dict] = {}
    if defaults_path.exists():
        try:
            defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
            for loc in defaults.get("default_locations", []):
                name = loc.get("name", "")
                if name:
                    city_meta[name] = {"lat": loc.get("lat", 0), "lon": loc.get("lon", 0)}
        except Exception:
            pass

    for city, leads in city_table.items():
        d = leads.get(lead)
        lat = city_meta.get(city, {}).get("lat", 0)
        lon = city_meta.get(city, {}).get("lon", 0)
        if d and d.get("bma_mean") is not None:
            entries.append(
                f'  "{city}": {{lat: {lat}, lon: {lon}, bma: {d["bma_mean"]:.1f}}}'
            )
    return "const SPARKLINE_DATA = {\n" + ",\n".join(entries) + "\n};"


def _build_sparkline_fetch_js() -> str:
    """Build JavaScript for fetching hourly temperature and rendering sparklines."""
    return """// ---- Sparkline: Peak Trend (Unicode block characters) ----
const SPARK_BLOCKS = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'];

async function fetchSparklineForCity(cityId, cityName) {
    const data = SPARKLINE_DATA[cityName];
    if (!data) return;

    const el = document.getElementById('spark-' + cityId);
    if (!el) return;

    const today = new Date().toISOString().slice(0, 10);
    const url = `https://api.open-meteo.com/v1/forecast?latitude=${data.lat}&longitude=${data.lon}&hourly=temperature_2m&past_days=1&forecast_days=1&timezone=UTC`;

    try {
        const resp = await fetch(url, { headers: { 'User-Agent': 'WeatherMonitor/1.0' } });
        const json = await resp.json();
        const temps = json.hourly?.temperature_2m;
        if (!temps || temps.length === 0) {
            el.innerHTML = '<span class="spark-loading">no data</span>';
            return;
        }

        // Filter to peak hours (roughly 10-18 local, or just use all 24h)
        const validTemps = temps.filter(t => t != null);
        if (validTemps.length === 0) {
            el.innerHTML = '<span class="spark-loading">no data</span>';
            return;
        }

        const tMin = Math.min(...validTemps);
        const tMax = Math.max(...validTemps);
        const tRange = tMax - tMin || 1;

        // Build sparkline string (last ~16 hours, or all)
        const displayTemps = validTemps.length > 18
            ? validTemps.slice(validTemps.length - 18)
            : validTemps;

        let spark = '';
        let peakIdx = -1;
        let closestDist = Infinity;

        for (let i = 0; i < displayTemps.length; i++) {
            const t = displayTemps[i];
            const normalized = (t - tMin) / tRange;
            const blockIdx = Math.min(7, Math.max(0, Math.round(normalized * 7)));
            spark += SPARK_BLOCKS[blockIdx + 1];

            // Track closest temp to BMA predicted peak
            const dist = Math.abs(t - data.bma);
            if (dist < closestDist) {
                closestDist = dist;
                peakIdx = i;
            }
        }

        // Insert peak marker (replace closest block with red marker)
        if (peakIdx >= 0 && peakIdx < spark.length) {
            const chars = [...spark];
            chars[peakIdx] = `<span class="peak-marker" title="BMA pred: ${data.bma}°C">█</span>`;
            spark = chars.join('');
        }

        const peakLabel = data.bma != null ? ` 🔴${data.bma.toFixed(0)}°C` : '';
        el.innerHTML = spark + peakLabel;

    } catch (e) {
        el.innerHTML = '<span class="spark-loading">err</span>';
    }
}

async function fetchAllSparklines() {
    const cells = document.querySelectorAll('.col-spark');
    const promises = [];
    cells.forEach(cell => {
        const row = cell.closest('tr');
        if (!row) return;
        const cityName = row.getAttribute('data-city');
        const cityId = cityName ? cityName.replace(/[^a-zA-Z0-9]/g, '_') : '';
        if (cityName && SPARKLINE_DATA[cityName]) {
            promises.push(fetchSparklineForCity(cityId, cityName));
        } else {
            cell.innerHTML = '<span class="spark-loading">—</span>';
        }
    });
    await Promise.allSettled(promises);
}
"""
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


def _build_expandable_market_section_html() -> str:
    """Build expandable market detail section showing Polymarket buckets per city."""
    if not HAS_MARKET_EDGE:
        return ""

    try:
        market_opps, _ = load_market_prices()
        bma_preds = load_bma_predictions()
    except Exception:
        return ""

    if not market_opps:
        return ""

    city_buckets: dict[str, list[dict]] = {}
    for opp in market_opps:
        city = opp.get("city", "Unknown")
        temp = opp.get("temp", 0)
        market_prob = opp.get("market_prob", 0)
        is_resolved = opp.get("is_resolved", False)
        volume = opp.get("volume", 0)
        bma_data = bma_preds.get(city)
        if bma_data is None:
            for bma_city, bd in bma_preds.items():
                if bma_city.split(",")[0].strip().lower() == city.lower():
                    bma_data = bd
                    break
        bma_pct = None
        if bma_data:
            bma_pct = compute_bma_prob(
                bma_data["bma_mean"], bma_data["bma_std"],
                temp, opp.get("type", "exact")
            )
        city_buckets.setdefault(city, []).append({
            "temp": temp, "market_prob": market_prob,
            "bma_prob": bma_pct, "is_resolved": is_resolved,
            "volume": volume,
        })

    rows = ""
    for city, buckets in sorted(city_buckets.items()):
        buckets.sort(key=lambda b: b["temp"])
        city_slug = re.sub(r'[^a-zA-Z0-9]+', '-', city).lower().strip('-')
        n_buckets = len(buckets)
        n_resolved = sum(1 for b in buckets if b["is_resolved"])
        total_vol = sum(b["volume"] for b in buckets)
        vol_str = f"${total_vol/1000:.0f}K" if total_vol >= 1000 else (f"${total_vol}" if total_vol > 0 else "—")
        winners = [b for b in buckets if b["market_prob"] > 99]
        win_info = f' <span style="color:#3fb950;font-size:0.7rem;">✅ → {fmt_temp(winners[0]["temp"], city)}</span>' if winners else ""

        rows += f"""<tr class="mkt-group" onclick="toggleMarketBuckets('{city_slug}')">
            <td><span class="expand-icon">▶</span></td>
            <td><strong>{city}</strong>{win_info}</td>
            <td>{n_buckets} buckets</td>
            <td>{vol_str}</td>
            <td>{n_resolved}/{n_buckets} resolved</td>
        </tr>"""

        for b in buckets:
            bma_str = f"{b['bma_prob']:.1f}%" if b["bma_prob"] is not None else "—"
            mkt_str = f"{b['market_prob']:.1f}%"
            resolved_icon = ' <span style="color:#3fb950;">✅</span>' if b["is_resolved"] else ""
            row_style = ' style="color:var(--green);"' if b["is_resolved"] and b["market_prob"] > 99 else (
                ' style="color:var(--red);"' if b["is_resolved"] and b["market_prob"] < 1 else ""
            )
            rows += f"""<tr class="mkt-bucket {city_slug}"{row_style}>
            <td></td>
            <td>{fmt_temp(b['temp'], city)}{resolved_icon}</td>
            <td>BMA: {bma_str}</td>
            <td>Mkt: {mkt_str}</td>
            <td></td>
        </tr>"""

    if not rows:
        return ""

    return f"""
    <div class="section">
      <h2>📋 MARKEDSDETALJER — Utvidbar Per By</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Klikk på en by for å se alle Polymarket-buckets. ✅ = resolved. Sortert alfabetisk.
      </p>
      <div style="max-height: 600px; overflow-y: auto;">
      <table>
        <thead><tr><th></th><th>By</th><th>Buckets</th><th>Volume</th><th>Resolved</th></tr></thead>
        <tbody>{rows}</tbody>
      </table>
      </div>
    </div>"""


def _generate_html_report() -> str:
    """Generate a self-contained HTML dashboard with dark theme and 3-strategy comparison."""
    log_data = _load_log()
    runs = log_data.get("runs", [])
    cum = log_data.get("cumulative", {})

    # Load peak data from pipeline
    peak_data = {}
    if runs:
        latest = runs[-1]
        for city, pdata in latest.get("predictions", {}).items():
            actual = pdata.get("strategies", {}).get("sigma", {}).get("actual_peak")
            if actual:
                peak_data[city] = actual

    best_per_city = _get_best_strategy_per_city(runs)

    # Aggregate per-strategy stats (recomputed from predictions, not the
    # run-level summary which can go stale before daily_close).
    _tally = _tally_from_predictions(runs)
    sigma_wins = _tally["sigma_wins"]
    sigma_losses = _tally["sigma_losses"]
    p5_wins = _tally["p5_wins"]
    p5_losses = _tally["p5_losses"]
    mean_wins = _tally["mean_wins"]
    mean_losses = _tally["mean_losses"]

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
        _rt = _tally_from_predictions([r])
        sw = _rt["sigma_wins"]
        sl = _rt["sigma_losses"]
        pw = _rt["p5_wins"]
        pl = _rt["p5_losses"]
        mw = _rt["mean_wins"]
        ml = _rt["mean_losses"]
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

    # The "AVGJORTE RESULTATER" table must use the run with the most resolved
    # predictions (ground-truth outcomes), not the most recent run.
    resolved_run = _pick_most_resolved_run(runs)

    if latest_run:
        top5_cities = latest_run.get("top_5_confidence", [])
        preds = latest_run.get("predictions", {})
        multi_day = latest_run.get("predictions_multi_day", {})
        target_date = latest_run.get("target_date", latest_run.get("run_date", ""))
        resolved_preds = resolved_run.get("predictions", {})
        resolved_target_date = resolved_run.get("target_date", resolved_run.get("run_date", ""))

        # ── Day 1: I DAG ──
        if top5_cities:
            top5_rows = _build_top5_rows_html(preds, top5_cities[:5], peak_data)
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
        <thead><tr><th>#</th><th>City</th><th>BMA μ</th><th>Sigma Pos</th><th>P5 Pos</th><th>Mean Pos</th><th>Conf</th><th>Models</th><th>📡 Peak | Marked</th><th>Sigma</th><th>P5</th><th>Mean</th><th>Rec</th></tr></thead>
        <tbody>{top5_rows}
        </tbody>
      </table>
      </div>
    </div>
    {flip_section}
    {divergence_section}
    {strategy_comparison}"""

        # ── RESOLVED RESULTS section (show all 51 cities' outcomes) ──
        resolved_market_outcomes = _load_resolved_market_outcomes()
        resolved_rows = ""
        resolved_cities = []
        for city, pdata in sorted(resolved_preds.items()):
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

                # Market deviation: compare our live peak with Polymarket resolved outcome
                market_cell = "—"
                avvik_cell = "—"
                city_base = city.split(",")[0].strip()  # "Taipei, TW" → "Taipei"
                market_temp = resolved_market_outcomes.get(city_base) or resolved_market_outcomes.get(city)
                if market_temp is not None and isinstance(actual, (int, float)):
                    market_cell = f'{market_temp}°C'
                    gap = round(actual - market_temp, 1)
                    gap_color = "#f85149" if abs(gap) > 2.0 else ("#d2991d" if abs(gap) > 1.0 else "#3fb950")
                    avvik_icon = "⚠️" if abs(gap) > 2.0 else ("🟡" if abs(gap) > 1.0 else "✅")
                    avvik_cell = f'<span style="color:{gap_color};font-weight:600;">{avvik_icon} {gap:+.1f}°C</span>'
                elif market_temp is not None:
                    market_cell = f'{market_temp}°C'
                    avvik_cell = '<span style="color:var(--text-dim);">—</span>'

                resolved_rows += f"""
                <tr>
                    <td><strong>{city}</strong></td>
                    <td>{sigma_spill}°C</td>
                    <td>{_ri(sigma.get('result', ''))} ({actual_str})</td>
                    <td>{p5_spill}°C</td>
                    <td>{_ri(p5s.get('result', ''))}</td>
                    <td>{mean_spill}°C</td>
                    <td>{_ri(means.get('result', ''))}</td>
                    <td>{market_cell}</td>
                    <td>{avvik_cell}</td>
                </tr>"""

            predictions_html += f"""
   <div class="section">
      <h2>📊 AVGJORTE RESULTATER ({len(resolved_cities)} byer, {resolved_target_date})</h2>
      <p style="color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px;">
        Resolved against archive data. ✅ WIN = round(actual peak) == spill bucket.
        📡 = vår live peak | Marked = Polymarket utfall | ⚠️ Avvik = stasjonsfeil hvis >2°C
      </p>
     <div style="max-height: 600px; overflow-y: auto;">
     <table>
       <thead><tr><th>By</th><th>Sigma Spill</th><th>Sigma Utfall</th><th>P5 Spill</th><th>P5 Utfall</th><th>Mean Spill</th><th>Mean Utfall</th><th>Marked</th><th>⚠️ Avvik</th></tr></thead>
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
    
    # Extract last pipeline run time from log
    last_pipeline_str = "—"
    if runs:
        last_run = runs[-1]
        lu = last_run.get("last_updated") or last_run.get("run_started", "")
        if lu:
            try:
                dt = datetime.fromisoformat(str(lu).replace("Z", "+00:00"))
                last_pipeline_str = dt.strftime("%H:%M UTC")
            except Exception:
                pass

    # ── Today vs Tomorrow separation ──
    today_tomorrow_section = _build_today_tomorrow_section(runs)

    # ── Edge-maximizing metric sections ──
    city_win_rate_section = _build_city_win_rate_section(runs)
    model_agreement_section = _build_model_agreement_section(runs)
    range_accuracy_section = _build_range_accuracy_section(runs)
    optimal_strat_section = _build_optimal_strategy_by_confidence_section(runs)
    cum_edge_section = _build_cumulative_edge_section(runs)
    region_section = _build_region_performance_section(runs)
    uhi_section = _build_uhi_accuracy_section(runs)
    strat_rec_section = _build_strat_rec_html_section(best_per_city)

    # ── Market Edge Section (BMA vs Polymarket) ──
    market_edge_section = _build_market_edge_html_section(peak_data)

    # ── Expandable Market Detail Section ──
    expandable_market_section = _build_expandable_market_section_html()

    # ── Edge Validation Section (Real vs Imagined) ──
    edge_validation_section = _build_edge_validation_html_section(runs)

    # ── Resolution Arbitrage Section (Post-Peak) ──
    resolution_arb_section = _build_resolution_arbitrage_html_section()

    # ── Arbitrage Stats Section (Win/Loss Tracking) ──
    arbitrage_stats_section = _build_arbitrage_stats_html_section(runs)

    # ── Madrid Arbitrage Highlight ──
    madrid_arb_section = _build_madrid_arbitrage_highlight(runs)

    # ── Peak Verification: Var vs Polymarket ──
    peak_verification_section = _build_peak_verification_html_section()

    # ── PM Strategy Results: Anbefalt Spill vs Polymarket ──
    pm_strategy_section = _build_pm_strategy_html_section()

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
  .col-peak {{ font-weight: 600; font-size: 0.8rem; }}
  .col-trend {{ font-weight: 700; font-size: 1rem; text-align: center; }}
  .col-spark {{ font-family: monospace; font-size: 0.85rem; white-space: nowrap; text-align: center; min-width: 90px; }}
  .mkt-group {{ cursor: pointer; transition: background 0.15s; }}
  .mkt-group:hover {{ background: var(--bg-card-hover) !important; }}
  .expand-icon {{ display: inline-block; width: 16px; transition: transform 0.2s; font-size: 0.7rem; margin-right: 4px; }}
  .mkt-group.expanded .expand-icon {{ transform: rotate(90deg); }}
  .mkt-bucket {{ display: none; font-size: 0.78rem; background: rgba(22, 27, 34, 0.4); }}
  .mkt-bucket.show {{ display: table-row; }}
  .mkt-bucket:hover {{ background: rgba(28, 35, 51, 0.6); }}
  .mkt-bucket td {{ padding: 5px 8px; border-bottom: 1px solid rgba(48, 54, 61, 0.3); color: var(--text-dim); }}
  .mkt-bucket td:first-child {{ padding-left: 32px; }}
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

function toggleMarketBuckets(slug) {{
  var bucketRows = document.querySelectorAll('tr.mkt-bucket.' + slug);
  var cityRow = document.querySelector('tr.mkt-group[onclick*="' + slug + '"]');
  var anyVisible = false;
  bucketRows.forEach(function(r) {{
    if (r.classList.contains('show')) anyVisible = true;
  }});
  bucketRows.forEach(function(r) {{
    if (anyVisible) {{
      r.classList.remove('show');
    }} else {{
      r.classList.add('show');
    }}
  }});
  if (cityRow) {{
    if (anyVisible) {{
      cityRow.classList.remove('expanded');
    }} else {{
      cityRow.classList.add('expanded');
    }}
  }}
}}

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
  <div class="subtitle" id="last-updated">{'⏳ Ingen data enda — første pipeline-kjøring kl 06:00 UTC' if not has_data else f'🤖 Siste pipeline: {last_pipeline_str} | Neste pipeline-oppdatering: …'}</div>
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

  <!-- TODAY VS TOMORROW SEPARATION -->
  {today_tomorrow_section}

  <!-- RESOLUTION ARBITRAGE: Post-Peak -->
  {resolution_arb_section}

  <!-- MADRID ARBITRAGE HIGHLIGHT -->
  {madrid_arb_section}

  <!-- PEAK VERIFICATION: Var vs Polymarket -->
  {peak_verification_section}

  <!-- PM STRATEGY RESULTS: Anbefalt Spill vs Polymarket -->
  {pm_strategy_section}

  <!-- ARBITRAGE STATS: Win/Loss Tracking -->
  {arbitrage_stats_section}

  <!-- MARKET EDGE: BMA vs Polymarket -->
  {market_edge_section}

  <!-- EXPANDABLE MARKET DETAIL -->
  {expandable_market_section}

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

  <!-- EDGE-MAXIMIZING METRICS -->
  {city_win_rate_section}
  {model_agreement_section}
  {range_accuracy_section}
  {optimal_strat_section}
  {cum_edge_section}
  {region_section}
  {uhi_section}
  {strat_rec_section}
  {edge_validation_section}

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

    # Compute per-city historical best strategy
    best_per_city = _get_best_strategy_per_city(runs)

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

    # ---- Load market price lookup for edge computation ----
    market_lookup: dict[tuple[str, int], float] = {}
    if HAS_MARKET_EDGE:
        try:
            market_lookup = build_market_lookup()
        except Exception:
            pass

    # ---- Compute today vs tomorrow resolved win rates ----
    def _compute_lead_rates(city_tbl: dict, ld: int):
        """Compute resolved win rates for a given lead_days value."""
        counts = {"sigma": {"wins": 0, "losses": 0}, "p5": {"wins": 0, "losses": 0}, "mean": {"wins": 0, "losses": 0}}
        for city, leads in city_tbl.items():
            d = leads.get(ld)
            if d is None:
                continue
            for sn in ("sigma", "p5", "mean"):
                r = d.get(f"{sn}_result", "")
                if r == "WIN":
                    counts[sn]["wins"] += 1
                elif r == "LOSS":
                    counts[sn]["losses"] += 1
        return counts

    def _rate_str(wins, losses):
        total = wins + losses
        return f"{round(wins / max(1, total) * 100, 1)}%" if total > 0 else "—"

    today_rates = _compute_lead_rates(city_table, 0)
    tomorrow_rates = _compute_lead_rates(city_table, 1)

    today_sigma_wr = _rate_str(today_rates["sigma"]["wins"], today_rates["sigma"]["losses"])
    today_p5_wr = _rate_str(today_rates["p5"]["wins"], today_rates["p5"]["losses"])
    today_mean_wr = _rate_str(today_rates["mean"]["wins"], today_rates["mean"]["losses"])

    tomorrow_sigma_wr = _rate_str(tomorrow_rates["sigma"]["wins"], tomorrow_rates["sigma"]["losses"])
    tomorrow_p5_wr = _rate_str(tomorrow_rates["p5"]["wins"], tomorrow_rates["p5"]["losses"])
    tomorrow_mean_wr = _rate_str(tomorrow_rates["mean"]["wins"], tomorrow_rates["mean"]["losses"])

    # Edge decay
    decay = 0
    t_sigma_total = today_rates["sigma"]["wins"] + today_rates["sigma"]["losses"]
    m_sigma_total = tomorrow_rates["sigma"]["wins"] + tomorrow_rates["sigma"]["losses"]
    if t_sigma_total > 0 and m_sigma_total > 0:
        t_sr = round(today_rates["sigma"]["wins"] / t_sigma_total * 100, 1)
        m_sr = round(tomorrow_rates["sigma"]["wins"] / m_sigma_total * 100, 1)
        decay = round(t_sr - m_sr, 1)
        decay_text = f"📊 EDGE DECAY: Tomorrow predictions are {abs(decay)}% {'less' if decay >= 0 else 'more'} accurate (Sigma)"
        decay_color = "#f85149" if decay > 5 else ("#d2991d" if decay > 0 else "#3fb950")
    else:
        decay_text = ""
        decay_color = "#8b949e"

    # ── Resolution Arbitrage Section (Post-Peak) ──
    resolution_arb_section = _build_resolution_arbitrage_html_section()

    summary_bar_html = ""
    if t_sigma_total > 0 or m_sigma_total > 0:
        summary_bar_html = f"""
      <div class="section" style="max-width: 900px; margin: 0 auto 20px;">
        <h2>📊 Today Win Rates (Resolved)</h2>
        <div class="grid-2">
          <div>
            <h3 style="color: var(--green); font-size: 0.9rem;">📊 TODAY (lead_days=0)</h3>
        <table><thead><tr><th>Strategy</th><th>W/L</th><th>Rate</th></tr></thead>
        <tbody>
          <tr><td>Sigma</td><td>{today_rates['sigma']['wins']}W/{today_rates['sigma']['losses']}L</td><td>{today_sigma_wr}</td></tr>
          <tr><td>P5</td><td>{today_rates['p5']['wins']}W/{today_rates['p5']['losses']}L</td><td>{today_p5_wr}</td></tr>
          <tr><td>Mean</td><td>{today_rates['mean']['wins']}W/{today_rates['mean']['losses']}L</td><td>{today_mean_wr}</td></tr>
        </tbody></table>
      </div>
    </div>
  </div>"""

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

    # ---- Build table rows (deduplicated: ONE row per city, prefer lead_days=1) ----
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

        # Deduplicate: pick best lead day per city (prefer 1 for active markets, 0 fallback)
        available_leads = [ld for ld in sorted_leads if city_table[city].get(ld) is not None]
        if not available_leads:
            continue
        ld = 0

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

        sigma_cell = f'{d["sigma_spill"]}°C {_ri(d["sigma_result"])}'
        p5_cell = f'{d["p5_spill"]}°C {_ri(d["p5_result"])}'
        mean_cell = f'{d["mean_spill"]}°C {_ri(d["mean_result"])}'

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

        # Compute market price for this city's sigma spill temperature (data only, no signals)
        market_cell = "—"
        if HAS_MARKET_EDGE and isinstance(sigma_spill, (int, float)) and market_lookup:
            city_lower = city.lower()
            mkt_prob = market_lookup.get((city_lower, int(sigma_spill)))
            if mkt_prob is None:
                city_base = city_lower.split(",")[0].strip()
                mkt_prob = market_lookup.get((city_base, int(sigma_spill)))
                if mkt_prob is None:
                    city_no_paren = re.sub(r'\s*\(.*?\)\s*', '', city_base).strip()
                    mkt_prob = market_lookup.get((city_no_paren, int(sigma_spill)))
            if mkt_prob is not None:
                market_cell = f"{mkt_prob:.1f}%"

        safe_city_id = re.sub(r'[^a-zA-Z0-9]', '_', city)

        # Show actual_peak from log data immediately (don't wait for live fetch)
        peak_display = f'📡 {actual_peak:.1f}°C ✅' if isinstance(actual_peak, (int, float)) else '⏳'

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
            <td class="col-peak" id="peak-{safe_city_id}">{peak_display}</td>
            <td class="col-trend" id="trend-{safe_city_id}">—</td>
            <td class="col-live" id="live-{safe_city_id}">—</td>
            <td class="col-market">{market_cell}</td>
            <td class="col-rec {rec_class}">{rec}</td>
            <td class="col-strat">{_build_strat_rec_cell(city, best_per_city)}</td>
            <td class="col-local">{local_time_str}</td>
        </tr>
"""

    cities_js = _build_cities_js_array()

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
  .col-peak {{ font-weight: 600; font-size: 0.8rem; }}
  .col-trend {{ font-weight: 700; font-size: 1rem; text-align: center; }}
  .col-spark {{ font-family: monospace; font-size: 0.85rem; letter-spacing: -1px; white-space: nowrap; text-align: center; min-width: 90px; }}
  .col-spark .peak-marker {{ color: var(--red); font-weight: 700; }}
  .spark-loading {{ color: var(--text-dim); font-size: 0.7rem; }}
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
  <button class="live-btn" onclick="fetchLivePeak()" id="fetch-btn">🔄 Hent Nåværende Temperatur & Døgnmaks</button>
  <span class="live-status" id="fetch-status"></span>
  <span class="live-updated" id="live-updated"></span>
</div>
<div class="container">

  {resolution_arb_section}

  {summary_bar_html}

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
          <th onclick="sortTable(9)">📡 Foreløpig Peak</th>
          <th onclick="sortTable(10)">📈 Trend</th>
          <th onclick="sortTable(11)">🔴 Live</th>
          <th onclick="sortTable(12)">Marked Pris</th>
          <th onclick="sortTable(13)">🎯 Anbefalt Strategi</th>
          <th onclick="sortTable(14)">🕐 Lokal</th>
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

// ---- Live Peak Detection (Hourly Archive API) ----
const SPARK_BLOCKS = ['▁','▂','▃','▄','▅','▆','▇','█'];

function isUSCity(cityName) {{
    return /, US$/.test(cityName);
}}

function cToF(c) {{
    return c * 9.0 / 5.0 + 32.0;
}}

function renderSparkline(hourlyTemps, isUS) {{
    if (!hourlyTemps || hourlyTemps.length < 2) return '—';

    // Get last 8 readings
    const recent = hourlyTemps.slice(-8);
    const min = Math.min(...recent);
    const max = Math.max(...recent);
    const range = max - min || 1;

    let sparkline = '';
    for (let i = 0; i < recent.length; i++) {{
        const idx = Math.min(7, Math.max(0, Math.floor((recent[i] - min) / range * 7)));
        const isRising = i > 0 && recent[i] > recent[i-1];
        const color = isRising ? '#4caf50' : '#f44336';
        sparkline += '<span style="color:' + color + '">' + SPARK_BLOCKS[idx] + '</span>';
    }}

    // Show current temp label (F for US, C for rest)
    const lastTemp = recent[recent.length - 1];
    const label = isUS ? cToF(lastTemp).toFixed(0) + '°F' : lastTemp.toFixed(0) + '°C';
    return sparkline + ' <span style="font-size:0.65rem;color:var(--text-dim);">' + label + '</span>';
}}

function updateCityRow(cityName, maxTemp, trend, peakStatus, sparklineHtml) {{
    const slug = cityName.replace(/[^a-zA-Z0-9]/g, '_');
    const peakEl = document.getElementById('peak-' + slug);
    const trendEl = document.getElementById('trend-' + slug);
    if (peakEl) {{
        peakEl.innerHTML = '📡 ' + maxTemp.toFixed(1) + '°C ' + peakStatus;
    }}
    if (trendEl) {{
        if (sparklineHtml) {{
            trendEl.innerHTML = sparklineHtml;
        }} else {{
            trendEl.textContent = trend;
            trendEl.style.color = trend === '↑' ? 'var(--red)' : (trend === '↓' ? 'var(--blue)' : 'var(--text-dim)');
        }}
    }}
}}

// City-level peak tracking state (persisted across fetchLivePeak calls)
const cityPeakState = {{}};

function computeAllCitiesConfidence(city, temps, localHour) {{
    const peakStart = city.peakStart || 14;
    const peakEnd = city.peakEnd || 17;
    const dailyMax = data.daily?.temperature_2m_max?.[1];
    const maxTemp = (dailyMax != null) ? dailyMax : Math.max.apply(null, temps);
    const latestTemp = temps[temps.length - 1];

    // Count consecutive declines
    let consecutiveDeclines = 0;
    for (let i = temps.length - 1; i >= 1; i--) {{
        if (temps[i] < temps[i-1] - 0.1) {{
            consecutiveDeclines++;
        }} else {{
            break;
        }}
    }}

    let confidence = 0;
    if (localHour > peakEnd) confidence += 60;
    else if (localHour >= peakStart) confidence += 30;
    if (consecutiveDeclines >= 3) confidence += 25;
    else if (consecutiveDeclines >= 1) confidence += 10;
    const gap = maxTemp - latestTemp;
    if (gap > 1.0) confidence += 15;
    else if (gap > 0.3) confidence += 5;

    return Math.min(98, confidence);
}}

async function fetchLivePeak() {{
    const today = new Date().toISOString().slice(0, 10);
    const statusEl = document.getElementById('fetch-status');

    // Only fetch cities with active markets, or all if none filtered
    const activeCities = CITIES.filter(c => c.has_market === true);
    const fetchCities = activeCities.length > 0 ? activeCities : CITIES;
    const total = fetchCities.length;
    let done = 0;

    for (const city of fetchCities) {{
        done++;
        if (statusEl) statusEl.textContent = '⏳ Henter peak ' + done + '/' + total + '...';
        try {{
            const url = 'https://api.open-meteo.com/v1/forecast?latitude=' + city.lat +
                '&longitude=' + city.lon + '&hourly=temperature_2m&daily=temperature_2m_max&past_days=1&forecast_days=1' +
                '&timezone=' + encodeURIComponent(city.tz);
            const resp = await fetch(url, {{ headers: {{ 'User-Agent': 'WeatherMonitor/1.0' }} }});
            const data = await resp.json();
            if (data.error) {{
                updateCityRow(city.name, 0, '—', '⚠️ Rate limit');
                await new Promise(r => setTimeout(r, 500));
                continue;
            }}

            const temps = data.hourly.temperature_2m.filter(function(t) {{ return t !== null; }});
            if (temps.length < 2) {{
                await new Promise(r => setTimeout(r, 500));
                continue;
            }}

            // Use official daily max from forecast API (index 1 = today when past_days=1).
            // This matches the archive API value and is more accurate than max(hourly).
            const dailyMax = data.daily?.temperature_2m_max?.[1];
            const maxTemp = (dailyMax != null) ? dailyMax : Math.max.apply(null, temps);
            const latestTemp = temps[temps.length - 1];
            const prevTemp = temps[temps.length - 2];
            const trend = latestTemp > prevTemp ? '↑' : (latestTemp < prevTemp ? '↓' : '→');

            // Get local hour
            const times = data.hourly.time;
            const lastTime = times[times.length - 1];
            const hour = parseInt(lastTime.split('T')[1].split(':')[0]);
            const peakStart = city.peakStart || 14;
            const peakEnd = city.peakEnd || 17;

            // Rule 4: Coastal cities shift peak ~1h earlier
            const coastalTzs = ['Asia/Taipei', 'Asia/Hong_Kong', 'Asia/Manila', 'Asia/Singapore',
                'Pacific/Auckland', 'America/New_York', 'America/Los_Angeles',
                'America/Miami', 'Europe/London', 'America/Panama'];
            const adjustedPeakEnd = coastalTzs.includes(city.tz) ? peakEnd - 1 : peakEnd;

            const inPeakWindow = (hour >= peakStart && hour <= adjustedPeakEnd);
            const pastPeakWindow = (hour > adjustedPeakEnd);
            const isLateDay = hour > 18;

            // Track state for this city
            if (!cityPeakState[city.name]) cityPeakState[city.name] = {{ lastNewMax: 0 }};
            const st = cityPeakState[city.name];

            // Track last new max
            const maxIdx = temps.lastIndexOf(maxTemp);
            const maxHour = parseInt(times[maxIdx].split('T')[1].split(':')[0]);
            if (maxHour >= peakStart && maxHour <= adjustedPeakEnd) {{
                st.lastNewMax = Date.now();
            }}
            const noNewMax2h = st.lastNewMax > 0 && (Date.now() - st.lastNewMax) > 2 * 3600 * 1000;

            // Count consecutive declines
            let consecDec = 0;
            for (let i = temps.length - 1; i >= 1; i--) {{
                if (temps[i] < temps[i-1] - 0.1) consecDec++;
                else break;
            }}
            const declineConfirmed = consecDec >= 3 && latestTemp < maxTemp - 0.3;

            // Precise peak detection
            var peakStatus;
            if (isLateDay || pastPeakWindow) {{
                peakStatus = '🔴 PEAK NÅDD';
            }} else if (declineConfirmed && inPeakWindow) {{
                peakStatus = '✅ PEAK BEKREFTET';
            }} else if (noNewMax2h && hour >= peakStart && temps.length >= 8) {{
                peakStatus = '🟡 PEAK SANSYNLIG';
            }} else if (inPeakWindow && trend === '↑') {{
                peakStatus = '🟢 STIGER';
            }} else if (inPeakWindow) {{
                peakStatus = '🟠 NÆR PEAK';
            }} else {{
                peakStatus = '⏳ VENTER';
            }}

            // Compute confidence
            const conf = computeAllCitiesConfidence(city, temps, hour);
            peakStatus += ' [' + conf + '%]';

            const usFlag = isUSCity(city.name);
            const sparkHtml = renderSparkline(temps, usFlag);
            updateCityRow(city.name, maxTemp, trend, peakStatus, sparkHtml);
        }} catch (e) {{
            updateCityRow(city.name, 0, '—', '⚠️ Rate limit');
        }}
        if (done < total) await new Promise(r => setTimeout(r, 500));
    }}

    if (statusEl) statusEl.textContent = '✅ Peak-data oppdatert (' + total + ' byer)';
    const btn = document.getElementById('fetch-btn');
    if (btn) {{
        btn.disabled = true;
        btn.textContent = '⏳ Vent 60s...';
        setTimeout(() => {{
            btn.disabled = false;
            btn.textContent = '🔄 Hent Nåværende Temperatur & Døgnmaks';
        }}, 60000);
    }}

    const updatedEl = document.getElementById('live-updated');
    if (updatedEl) updatedEl.textContent = new Date().toLocaleTimeString('no-NO');

    // Adaptive scheduling: if no cities in peak window, slow down
    const anyInWindow = fetchCities.some(c => {{
        try {{
            const opts = {{ timeZone: c.tz || 'UTC', hour: '2-digit', hour12: false }};
            const h = parseInt(new Date().toLocaleString('en-US', opts).replace(/^0/, ''), 10);
            return h >= (c.peakStart || 14) && h <= (c.peakEnd || 17);
        }} catch(e) {{ return false; }}
    }});
    // No auto-refetch — manual button click only
}}

// Manual fetch only — no auto-fetch on page load

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
            var rankCell = row.querySelector('.col-rank');
            if (rankCell) {{
                // Keep expand icon, prepend rank number
                var iconSpan = rankCell.querySelector('.expand-icon');
                rankCell.innerHTML = '';
                if (iconSpan) rankCell.appendChild(iconSpan);
                rankCell.appendChild(document.createTextNode(rank++));
            }}
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
        if (colIdx === 0 || colIdx === 2 || colIdx === 3 || colIdx === 8) {{
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


def _generate_index_html() -> str:
    """Generate the landing page (index.html) with links to all dashboards."""
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>VaerMonitor — Hjem</title>
<style>
  :root {{
    --bg: #0d1117; --bg-card: #161b22; --bg-card-hover: #1c2333;
    --border: #30363d; --text: #c9d1d9; --text-dim: #8b949e;
    --green: #3fb950; --red: #f85149; --orange: #d2991d;
    --blue: #58a6ff; --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif;
    background: var(--bg); color: var(--text); min-height: 100vh;
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    line-height: 1.6;
  }}
  .container {{ text-align: center; max-width: 600px; padding: 40px 20px; }}
  h1 {{ font-size: 2.5rem; color: var(--blue); margin-bottom: 8px; font-weight: 800; }}
  .subtitle {{ color: var(--text-dim); font-size: 1rem; margin-bottom: 36px; }}
  .nav-grid {{
    display: flex; flex-direction: column; gap: 14px;
    margin-bottom: 40px;
  }}
  .nav-card {{
    display: flex; align-items: center; gap: 16px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 20px 28px;
    text-decoration: none; color: var(--text);
    transition: all 0.2s; cursor: pointer;
  }}
  .nav-card:hover {{ background: var(--bg-card-hover); border-color: var(--blue); transform: translateY(-2px); box-shadow: 0 4px 20px rgba(88, 166, 255, 0.1); }}
  .nav-icon {{ font-size: 2rem; min-width: 48px; text-align: center; }}
  .nav-text {{ text-align: left; }}
  .nav-text h3 {{ font-size: 1.1rem; font-weight: 700; margin-bottom: 2px; }}
  .nav-text p {{ color: var(--text-dim); font-size: 0.82rem; }}
  footer {{ color: var(--text-dim); font-size: 0.75rem; }}
</style>
</head>
<body>
<div class="container">
  <h1>🌤️ VaerMonitor</h1>
  <p class="subtitle">BMA Multi-Model Ensemble · Temperatur Peak Prediksjon · 51 Byer</p>
  <div class="nav-grid">
    <a href="_quality_report.html" class="nav-card">
      <span class="nav-icon">📊</span>
      <span class="nav-text"><h3>Kvalitetsrapport</h3><p>3-strategi dashboard · Sigma / P5 / Mean · Edge Validation</p></span>
    </a>
    <a href="_all_cities.html" class="nav-card">
      <span class="nav-icon">🌍</span>
      <span class="nav-text"><h3>Alle 51 Byer</h3><p>Full by-tabell · Live temp · Sparklines · Marked Edge</p></span>
    </a>
    <a href="_peak_detection.html" class="nav-card">
      <span class="nav-icon">📈</span>
      <span class="nav-text"><h3>Live Peak Detection</h3><p>Sanntids-overvakning · Auto-velg byer i peak-vindu · Trend-piler · Peak-las</p></span>
    </a>
    <a href="brukermanual.html" class="nav-card">
      <span class="nav-icon">📖</span>
      <span class="nav-text"><h3>Brukermanual</h3><p>Norsk brukermanual · Oppsett · Funksjoner · Feilsøking</p></span>
    </a>
  </div>
  <footer>Generert: {now_str} · GitHub Pages Deploy</footer>
</div>
</body>
</html>"""


# Peak windows (local hour ranges) for each city
PEAK_WINDOWS: dict[str, tuple[int, int]] = {
    "Taipei, TW": (14, 16), "Hong Kong, HK": (14, 16), "Shanghai, CN": (14, 16),
    "Seoul (Incheon), KR": (14, 16), "Kuala Lumpur, MY": (14, 16), "Madrid, ES": (15, 18),
    "Paris, FR": (15, 18), "Munich, DE": (15, 18), "Wellington, NZ": (14, 17),
    "Shenzhen, CN": (14, 16), "Singapore, SG": (14, 16), "Guangzhou, CN": (14, 16),
    "New York, US": (14, 17), "London, UK": (15, 18), "Milan, IT": (15, 18),
    "Los Angeles, US": (14, 17), "Tokyo, JP": (14, 16), "Helsinki, FI": (15, 18),
    "Chongqing, CN": (14, 16), "Chengdu, CN": (14, 16), "Wuhan, CN": (14, 16),
    "Qingdao, CN": (14, 16), "Jeddah, SA": (14, 17), "Istanbul, TR": (14, 17),
    "Ankara, TR": (14, 17), "Busan, KR": (14, 16), "Dallas, US": (14, 17),
    "Houston, US": (14, 17), "Atlanta, US": (14, 17), "Lucknow, IN": (14, 17),
    "Manila, PH": (14, 16), "Karachi, PK": (14, 17), "Beijing, CN": (14, 16),
    "Chicago, US": (14, 17), "Toronto, CA": (14, 17), "Austin, US": (14, 17),
    "Amsterdam, NL": (15, 18), "Warsaw, PL": (15, 18), "Miami, US": (14, 17),
    "Cape Town, ZA": (14, 17), "Tel Aviv, IL": (14, 17),
    "Buenos Aires, AR": (14, 17), "Denver, US": (14, 17),
    "San Francisco, US": (14, 17), "Mexico City, MX": (14, 17),
    "Seattle, US": (14, 17), "Sao Paulo, BR": (14, 17), "Zhengzhou, CN": (14, 16),
    "Moscow, RU": (15, 18), "Panama City, PA": (14, 17), "Jinan, CN": (14, 16),
}


def _generate_peak_detection_html() -> str:
    """Generate a live-updating peak detection page with city toggle cards."""
    defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
    cities_js_entries: list[str] = []
    auto_select_cities: list[str] = []
    _default_locs: list[dict] = []
    if defaults_path.exists():
        try:
            defaults = json.loads(defaults_path.read_text(encoding="utf-8"))
            _default_locs = defaults.get("default_locations", [])
            for loc in _default_locs:
                name = loc.get("name", "")
                lat = loc.get("lat", 0)
                lon = loc.get("lon", 0)
                tz = loc.get("tz", "UTC")
                if name:
                    pw = PEAK_WINDOWS.get(name, (14, 17))
                    cities_js_entries.append(
                        f'  {{name: "{name}", lat: {lat}, lon: {lon}, tz: "{tz}", peakStart: {pw[0]}, peakEnd: {pw[1]}}}'
                    )
        except Exception:
            pass
    cities_js_array = "const ALL_CITIES = [\n" + ",\n".join(cities_js_entries) + "\n];"

    # Load actual peak data from quality log for embedding
    log_data = _load_log()
    actual_peak_entries: list[str] = []
    market_lookup_js: dict[str, float] = {}
    if log_data.get("runs"):
        latest_run = log_data["runs"][-1]
        preds = latest_run.get("predictions", {})
        for city, pdata in preds.items():
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            actual_peak = sigma.get("actual_peak")
            peak_detected_at = pdata.get("peak_detected_at")
            recommendation = pdata.get("recommendation", "")
            if actual_peak is not None:
                actual_peak_entries.append(
                    f'  "{city}": {{actual_peak: {actual_peak}, '
                    f'peak_detected_at: "{peak_detected_at or ""}", '
                    f'recommendation: "{recommendation}", '
                    f'spill: {sigma.get("spill", 0)}}}'
                )
    actual_peak_js = "const ACTUAL_PEAK_DATA = {\n" + ",\n".join(actual_peak_entries) + "\n};" if actual_peak_entries else "const ACTUAL_PEAK_DATA = {};"

    # Load market prices for arbitrage badges
    market_prices_entries: list[str] = []
    try:
        market_prices_path = Path(_SCRIPT_DIR) / "_market_prices.json"
        if market_prices_path.exists():
            mp_data = json.loads(market_prices_path.read_text(encoding="utf-8"))
            for opp in mp_data if isinstance(mp_data, list) else mp_data.get("opportunities", []):
                city = opp.get("city", "")
                temp = opp.get("temperature")
                price = opp.get("price")
                if city and temp is not None and price is not None:
                    market_prices_entries.append(f'  "{city}_{temp}": {price}')
    except Exception:
        pass
    market_prices_js = "const MARKET_PRICES = {\n" + ",\n".join(market_prices_entries) + "\n};" if market_prices_entries else "const MARKET_PRICES = {};"

    # ── Compute auto-select cities (in live peak window: peak_start-1h to peak_end+1h) ──
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    try:
        from zoneinfo import ZoneInfo
        _now_utc = datetime.now(timezone.utc)
        for loc in _default_locs:
            name = loc.get("name", "")
            tz_str = loc.get("tz", "UTC")
            pw = PEAK_WINDOWS.get(name, (14, 17))
            peak_start = pw[0]
            peak_end = pw[1]
            try:
                tz_obj = ZoneInfo(tz_str)
                local_dt = _now_utc.astimezone(tz_obj)
                local_hour = local_dt.hour
                # Live peak window: 1 hour before peak_start to 1 hour after peak_end
                if (peak_start - 1) <= local_hour <= (peak_end + 1):
                    auto_select_cities.append(name)
            except Exception:
                pass
    except Exception:
        pass
    auto_select_js = (
        "const AUTO_SELECT_CITIES = [\"" +
        "\", \"".join(auto_select_cities) +
        "\"];\nconst AUTO_SELECT_GENERATED = \"" + now_str + "\";"
    ) if auto_select_cities else "const AUTO_SELECT_CITIES = [];\nconst AUTO_SELECT_GENERATED = \"" + now_str + "\";"

    return f"""<!DOCTYPE html>
<html lang="no">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Live Peak Detection — VaerMonitor</title>
<style>
  :root {{
    --bg: #0d1117; --bg-card: #161b22; --bg-card-hover: #1c2333;
    --border: #30363d; --text: #c9d1d9; --text-dim: #8b949e;
    --green: #3fb950; --red: #f85149; --orange: #d2991d;
    --blue: #58a6ff; --purple: #bc8cff;
  }}
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, sans-serif; background: var(--bg); color: var(--text); min-height: 100vh; line-height: 1.6; }}
  .container {{ max-width: 1400px; margin: 0 auto; padding: 20px; }}
  header {{ text-align: center; padding: 24px 20px; border-bottom: 1px solid var(--border); margin-bottom: 20px; }}
  header h1 {{ font-size: 1.6rem; color: var(--blue); font-weight: 700; }}
  header .subtitle {{ color: var(--text-dim); font-size: 0.85rem; margin-top: 4px; }}
  .back-link {{ display: inline-block; margin-top: 10px; color: var(--text-dim); text-decoration: none; font-size: 0.8rem; }}
  .back-link:hover {{ color: var(--blue); }}
  .controls {{ display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: center; margin-bottom: 20px; }}
  .controls button {{
    background: var(--bg-card); border: 1px solid var(--border); color: var(--text);
    padding: 10px 20px; border-radius: 8px; cursor: pointer; font-size: 0.85rem;
    font-weight: 600; transition: all 0.2s;
  }}
  .controls button:hover {{ background: var(--bg-card-hover); border-color: var(--blue); }}
  .controls button.active {{ background: rgba(88, 166, 255, 0.15); border-color: var(--blue); color: var(--blue); }}
  .controls .status {{ color: var(--text-dim); font-size: 0.85rem; margin-left: 12px; }}
  .status-bar {{ text-align: center; padding: 10px 20px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; margin-bottom: 16px; font-size: 0.85rem; color: var(--text-dim); }}
  .city-selector {{ display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 20px; max-height: 200px; overflow-y: auto; padding: 12px; background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px; justify-content: center; }}
  .city-selector label {{
    display: flex; align-items: center; gap: 4px; padding: 4px 10px;
    background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
    cursor: pointer; font-size: 0.78rem; transition: all 0.15s; user-select: none;
  }}
  .city-selector label:hover {{ border-color: var(--blue); }}
  .city-selector input[type="checkbox"] {{ accent-color: var(--blue); }}
  .cards-grid {{
    display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
    gap: 16px;
  }}
  .peak-card {{
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 10px;
    padding: 18px; transition: all 0.3s;
  }}
  .peak-card.status-peak {{ border-color: var(--green); box-shadow: 0 0 12px rgba(63, 185, 80, 0.15); }}
  .peak-card.status-rising {{ border-color: var(--orange); }}
  .peak-card.status-waiting {{ border-color: var(--border); }}
  .peak-card.status-unknown {{ border-color: var(--border); opacity: 0.6; }}
  /* Peak window border colors */
  .peak-card.peak-window-wait {{ border-color: #9e9e9e; }}
  .peak-card.peak-window-near {{ border-color: #2e7d32; box-shadow: 0 0 12px rgba(46, 125, 50, 0.2); }}
  .peak-card.peak-window-in {{ border-color: #f57f17; box-shadow: 0 0 14px rgba(245, 127, 23, 0.25); }}
  .peak-card.peak-window-past {{ border-color: #c62828; }}
  /* Peak window status bar */
  .peak-window-bar {{
    font-size: 0.8rem; font-weight: 600; padding: 6px 10px; margin-bottom: 10px;
    border-radius: 6px; display: flex; align-items: center; gap: 6px;
  }}
  .peak-window-bar.wait {{ background: rgba(158, 158, 158, 0.12); color: #9e9e9e; }}
  .peak-window-bar.near {{ background: rgba(46, 125, 50, 0.1); color: #2e7d32; }}
  .peak-window-bar.in {{ background: rgba(245, 127, 23, 0.15); color: #e65100; }}
  .peak-window-bar.past {{ background: rgba(198, 40, 40, 0.1); color: #c62828; }}
  .peak-window-time {{ font-weight: 800; }}
  .peak-window-label {{ font-weight: 800; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }}
  .card-header h3 {{ font-size: 1.05rem; font-weight: 700; }}
  .card-status {{
    font-size: 0.75rem; font-weight: 700; padding: 4px 12px; border-radius: 20px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .card-status.peak {{ background: rgba(63, 185, 80, 0.15); color: var(--green); }}
  .card-status.rising {{ background: rgba(210, 153, 29, 0.15); color: var(--orange); }}
  .card-status.waiting {{ background: rgba(139, 148, 158, 0.15); color: var(--text-dim); }}
  .card-status.unknown {{ background: rgba(139, 148, 158, 0.1); color: var(--text-dim); }}
  .card-temp {{ font-size: 2.4rem; font-weight: 800; margin-bottom: 4px; }}
  .card-meta {{ display: flex; gap: 16px; font-size: 0.78rem; color: var(--text-dim); margin-bottom: 12px; flex-wrap: wrap; }}
  .card-meta span {{ white-space: nowrap; }}
  .sparkline-canvas {{ width: 100%; height: 80px; margin-bottom: 8px; border-radius: 4px; background: rgba(13, 17, 23, 0.5); }}
  .card-info {{ display: flex; justify-content: space-between; font-size: 0.75rem; color: var(--text-dim); }}
  .card-info .conf {{ font-weight: 600; }}
  .card-info .conf.high {{ color: var(--green); }}
  .card-info .conf.medium {{ color: var(--orange); }}
  .card-info .conf.low {{ color: var(--red); }}
  .actual-peak-bar {{
    font-size: 0.85rem; font-weight: 700; padding: 8px 12px; margin-bottom: 10px;
    border-radius: 6px; display: flex; align-items: center; gap: 8px;
    background: rgba(63, 185, 80, 0.12); color: var(--green);
    border: 1px solid rgba(63, 185, 80, 0.3);
  }}
  .actual-peak-bar.win {{ background: rgba(63, 185, 80, 0.12); color: var(--green); border-color: rgba(63, 185, 80, 0.3); }}
  .actual-peak-bar.loss {{ background: rgba(248, 81, 73, 0.12); color: var(--red); border-color: rgba(248, 81, 73, 0.3); }}
  .arb-badge {{
    display: inline-block; padding: 2px 8px; border-radius: 10px;
    font-size: 0.7rem; font-weight: 800; margin-left: 6px;
    background: rgba(210, 153, 29, 0.2); color: var(--orange);
    text-transform: uppercase; letter-spacing: 0.5px;
  }}
  .empty-state {{ text-align: center; padding: 60px 20px; color: var(--text-dim); }}
  .empty-state .icon {{ font-size: 3rem; margin-bottom: 12px; }}
  footer {{ text-align: center; padding: 16px; color: var(--text-dim); font-size: 0.75rem; border-top: 1px solid var(--border); margin-top: 20px; }}
</style>
</head>
<body>
<header>
  <h1>📈 Live Peak Detection</h1>
  <div class="subtitle">Sanntids-overvakning · Velg byer for a spore temperaturtopper</div>
  <a href="index.html" class="back-link">← Tilbake til VaerMonitor</a>
</header>

<div class="container">
  <div class="status-bar" id="status-bar">
    Laster... byer i peak-vindu auto-velges automatisk
  </div>

  <div class="controls">
    <button id="btn-select-all" onclick="selectAll()">Velg Alle</button>
    <button id="btn-deselect-all" onclick="deselectAll()">Fjern Alle</button>
    <button id="btn-start" onclick="startMonitoring()" style="background: rgba(88,166,255,0.15); border-color: var(--blue); color: var(--blue);">Start Overvakning</button>
    <button id="btn-stop" onclick="stopMonitoring()" style="display:none;">Stopp</button>
    <span class="status" id="monitor-status"></span>
  </div>

  <div class="city-selector" id="city-selector">
  </div>

  <div class="cards-grid" id="cards-grid">
    <div class="empty-state">
      <div class="icon">📈</div>
      <p>Velg byer over og trykk "Start Overvakning" for a begynne.</p>
      <p style="font-size:0.8rem; margin-top:8px;">Temperatur hentes hvert 3. minutt fra Open-Meteo.</p>
    </div>
  </div>
</div>

<footer>
  Live Peak Detection · Open-Meteo API · GitHub Pages Deploy
</footer>

<script>
{cities_js_array}
{actual_peak_js}
{market_prices_js}
{auto_select_js}

// ---- State ----
const activeCities = new Set();  // city names currently monitored
const monitoringIntervals = {{}};  // per-city interval IDs for adaptive polling
const PEAK_POLL_MS = 180000;     // 3 min — cities in peak window
const NORMAL_POLL_MS = 600000;   // 10 min — cities outside peak window
const cityData = {{}};
const MAX_DATA_POINTS = 30;
const cityLookup = new Map();    // name -> city object for fast lookup
const smsSentToday = {{}};       // cityName -> true if SMS already sent today
const recSpills = {{}};          // cityName -> {{strategy: "mean", spill: 22}} loaded from quality log

// ---- Build city selector checkboxes ----
(function buildSelector() {{
    const container = document.getElementById('city-selector');
    ALL_CITIES.forEach(city => {{
        cityLookup.set(city.name, city);
        const label = document.createElement('label');
        const isAuto = AUTO_SELECT_CITIES.includes(city.name);
        label.innerHTML = '<input type="checkbox" value="' + city.name + '" onchange="onCityToggle(this)"' + (isAuto ? ' checked' : '') + '> ' + (isAuto ? '🔴 ' : '') + city.name;
        container.appendChild(label);
    }});
    // Update status bar
    updateStatusBar();
}})();

function getCheckedCities() {{
    const checks = document.querySelectorAll('#city-selector input[type="checkbox"]:checked');
    return Array.from(checks).map(c => c.value);
}}

function updateStatusBar() {{
    const total = activeCities.size;
    const autoCount = AUTO_SELECT_CITIES.length;
    if (total > 0) {{
        document.getElementById('status-bar').innerHTML =
            '🟢 Overvåker <b>' + total + '</b> byer' +
            (autoCount > 0 ? ' | 🔴 <b>' + autoCount + '</b> i peak-vindu' : '') +
            ' | Generert: ' + AUTO_SELECT_GENERATED +
            ' | Poll: 3 min (peak) / 10 min (utenfor)';
    }} else if (autoCount > 0) {{
        document.getElementById('status-bar').innerHTML =
            '🔴 <b>' + autoCount + '</b> byer i peak-vindu — kryss av for å starte overvåkning | Generert: ' + AUTO_SELECT_GENERATED;
    }} else {{
        document.getElementById('status-bar').textContent =
            'Ingen byer i peak-vindu akkurat nå. Velg byer manuelt. | Generert: ' + AUTO_SELECT_GENERATED;
    }}
}}

function onCityToggle(checkbox) {{
    const cityName = checkbox.value;
    if (checkbox.checked) {{
        startCity(cityName);
    }} else {{
        stopCity(cityName);
    }}
}}

function selectAll() {{
    document.querySelectorAll('#city-selector input[type="checkbox"]').forEach(cb => {{ cb.checked = true; startCity(cb.value); }});
}}

function deselectAll() {{
    document.querySelectorAll('#city-selector input[type="checkbox"]').forEach(cb => {{ cb.checked = false; stopCity(cb.value); }});
}}

async function fetchCurrentTemp(city) {{
    try {{
        const resp = await fetch(
            'https://api.open-meteo.com/v1/forecast?latitude=' + city.lat + '&longitude=' + city.lon + '&current=temperature_2m&daily=temperature_2m_max&timezone=' + encodeURIComponent(city.tz),
            {{ headers: {{ 'User-Agent': 'WeatherMonitor/1.0' }} }}
        );
        const data = await resp.json();
        if (data.error) throw new Error(data.reason);
        const temp = data.current?.temperature_2m;
        const time = data.current?.time || new Date().toISOString();
        const dailyMax = data.daily?.temperature_2m_max?.[0] ?? null;
        return {{ temp, time, dailyMax }};
    }} catch (e) {{
        console.error('Fetch failed for ' + city.name + ':', e);
        return null;
    }}
}}

function initCityData(cityName) {{
    if (!cityData[cityName]) {{
        cityData[cityName] = {{ temps: [], timestamps: [], bmaLine: null }};
    }}
}}

function drawSparkline(cityName, canvasId) {{
    const canvas = document.getElementById(canvasId);
    if (!canvas) return;
    const ctx = canvas.getContext('2d');
    const data = cityData[cityName];
    if (!data || data.temps.length < 2) {{
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.fillStyle = '#8b949e';
        ctx.font = '11px monospace';
        ctx.fillText('Venter pa data...', 8, canvas.height / 2 + 4);
        return;
    }}

    const w = canvas.width;
    const h = canvas.height;
    const margin = {{ top: 6, right: 10, bottom: 6, left: 10 }};
    const pw = w - margin.left - margin.right;
    const ph = h - margin.top - margin.bottom;

    const temps = data.temps;
    const tMin = Math.min(...temps) - 0.5;
    const tMax = Math.max(...temps) + 0.5;
    const tRange = tMax - tMin || 1;

    ctx.clearRect(0, 0, w, h);

    // Grid lines
    ctx.strokeStyle = 'rgba(48, 54, 61, 0.5)';
    ctx.lineWidth = 0.5;
    for (let i = 0; i <= 4; i++) {{
        const y = margin.top + (ph / 4) * i;
        ctx.beginPath();
        ctx.moveTo(margin.left, y);
        ctx.lineTo(w - margin.right, y);
        ctx.stroke();
    }}

    // BMA line
    if (data.bmaLine !== null) {{
        const bmaY = margin.top + ph - ((data.bmaLine - tMin) / tRange) * ph;
        ctx.strokeStyle = 'rgba(188, 140, 255, 0.7)';
        ctx.lineWidth = 1.5;
        ctx.setLineDash([4, 4]);
        ctx.beginPath();
        ctx.moveTo(margin.left, bmaY);
        ctx.lineTo(w - margin.right, bmaY);
        ctx.stroke();
        ctx.setLineDash([]);

        ctx.fillStyle = '#bc8cff';
        ctx.font = '9px monospace';
        ctx.fillText('BMA ' + data.bmaLine.toFixed(1), w - margin.right - 55, bmaY - 4);
    }}

    // Temperature line
    ctx.strokeStyle = '#58a6ff';
    ctx.lineWidth = 2;
    ctx.beginPath();
    for (let i = 0; i < temps.length; i++) {{
        const x = margin.left + (pw / (MAX_DATA_POINTS - 1)) * i;
        const y = margin.top + ph - ((temps[i] - tMin) / tRange) * ph;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
    }}
    ctx.stroke();

    // Current temp dot
    const lastX = margin.left + (pw / (MAX_DATA_POINTS - 1)) * (temps.length - 1);
    const lastY = margin.top + ph - ((temps[temps.length - 1] - tMin) / tRange) * ph;
    ctx.fillStyle = '#58a6ff';
    ctx.beginPath();
    ctx.arc(lastX, lastY, 4, 0, Math.PI * 2);
    ctx.fill();
    ctx.strokeStyle = '#fff';
    ctx.lineWidth = 1;
    ctx.stroke();

    // Min/max labels
    ctx.fillStyle = '#8b949e';
    ctx.font = '9px monospace';
    ctx.fillText(tMax.toFixed(1) + '', 2, margin.top + 10);
    ctx.fillText(tMin.toFixed(1) + '', 2, h - margin.bottom);
}}

// ---- Precise Peak Confidence (Research-Based) ----
function computePeakConfidence(currentTemp, maxTemp, localHour, peakStart, peakEnd, consecutiveDeclines) {{
    let confidence = 0;

    // Time factor: Rule 1 — peak window
    if (localHour > peakEnd) confidence += 60;       // Rule 3: Late Day
    else if (localHour >= peakStart) confidence += 30; // In peak window

    // Decline factor: Rule 2 — 3+ consecutive declines
    if (consecutiveDeclines >= 3) confidence += 25;
    else if (consecutiveDeclines >= 1) confidence += 10;

    // Gap factor: how far below max
    const gap = maxTemp - currentTemp;
    if (gap > 1.0) confidence += 15;
    else if (gap > 0.3) confidence += 5;

    return Math.min(98, confidence);
}}

// ---- Precise Peak Detection (5 Research-Based Rules) ----
function computePeakStatus(cityName, currentTemp, cityMeta) {{
    const data = cityData[cityName];
    if (!data || data.temps.length < 3) return {{ status: 'unknown', label: 'VENTER', cssClass: 'unknown', confidence: 0, peakReached: false }};

    const temps = data.temps;
    const peakStart = cityMeta.peakStart || 14;
    const peakEnd = cityMeta.peakEnd || 17;
    const tz = cityMeta.tz || 'UTC';

    // Get local hour
    let localHour = new Date().getHours(); // fallback
    try {{
        const opts = {{ timeZone: tz, hour: '2-digit', hour12: false }};
        localHour = parseInt(new Date().toLocaleString('en-US', opts).replace(/^0/, ''), 10);
    }} catch(e) {{}}

    // Track consecutive declines (Rule 2)
    if (!data.consecutiveDeclines) data.consecutiveDeclines = 0;
    if (temps.length >= 2) {{
        const last = temps[temps.length - 1];
        const prev = temps[temps.length - 2];
        if (last < prev - 0.1) {{
            data.consecutiveDeclines++;
        }} else if (last > prev + 0.1) {{
            data.consecutiveDeclines = 0;
        }}
    }}

    const allTimeMax = Math.max(...temps);
    const trend6 = temps.length >= 6 ? temps[temps.length - 1] - temps[temps.length - 6] : 0;

    // Rule 5: No new max for 2+ hours after peak start
    if (!data.lastNewMaxTime) data.lastNewMaxTime = null;
    if (Math.abs(currentTemp - allTimeMax) < 0.2) {{
        data.lastNewMaxTime = Date.now();
    }}
    const noNewMaxMs = data.lastNewMaxTime ? Date.now() - data.lastNewMaxTime : Infinity;
    const noNewMax2h = noNewMaxMs > 2 * 3600 * 1000;

    // Rule 4: Cloud cover / coastal shift — coastal cities peak ~1h earlier
    const isCoastal = ['Asia/Taipei', 'Asia/Hong_Kong', 'Asia/Manila', 'Asia/Singapore',
        'Pacific/Auckland', 'America/New_York', 'America/Los_Angeles',
        'America/Miami', 'Europe/London', 'America/Panama'].includes(tz);
    const adjustedPeakEnd = isCoastal ? peakEnd - 1 : peakEnd;

    // Rule 1: Peak window check
    const inWindow = localHour >= peakStart && localHour <= adjustedPeakEnd;
    const pastWindow = localHour > adjustedPeakEnd;

    // Rule 3: Late Day Rule — if > 18:00, peak is 95%+ likely
    const isLateDay = localHour > 18;

    // Rule 2: Decline confirmation — 3+ consecutive declines + temp < max - 0.3
    const declineConfirmed = data.consecutiveDeclines >= 3 && currentTemp < allTimeMax - 0.3;

    // Consolidate peak detection
    let peakReached = false;
    let statusLabel = 'VENTER';
    let statusCss = 'waiting';

    if (isLateDay || pastWindow) {{
        // Rule 3 + Rule 1-past-window: peak almost certainly reached
        peakReached = true;
        statusLabel = '🔴 PEAK NÅDD';
        statusCss = 'peak';
    }} else if (declineConfirmed && inWindow) {{
        // Rule 2: confirmed decline in window
        peakReached = true;
        statusLabel = '✅ PEAK BEKREFTET';
        statusCss = 'peak';
    }} else if (noNewMax2h && localHour >= peakStart && temps.length >= 8) {{
        // Rule 5: No new max for 2h+ after 14:00 local
        peakReached = true;
        statusLabel = '🟡 PEAK SANSYNLIG';
        statusCss = 'peak';
    }} else if (inWindow && trend6 > 0.2 && !declineConfirmed) {{
        statusLabel = '🟢 STIGER';
        statusCss = 'rising';
    }} else if (inWindow) {{
        statusLabel = '🟠 NÆR PEAK';
        statusCss = 'rising';
    }}

    const confidence = computePeakConfidence(
        currentTemp, allTimeMax, localHour, peakStart, adjustedPeakEnd, data.consecutiveDeclines
    );

    return {{
        status: peakReached ? 'peak' : (trend6 > 0.2 ? 'rising' : 'waiting'),
        label: statusLabel,
        cssClass: statusCss,
        confidence: confidence / 100,
        peakReached: peakReached,
        localHour: localHour,
    }};
}}

// ---- Peak Window Status ----
function getPeakStatus(peakStart, peakEnd, tz) {{
    try {{
        const now = new Date();
        const opts = {{ timeZone: tz, hour: '2-digit', hour12: false }};
        const localHourStr = now.toLocaleString('en-US', opts);
        const currentHour = parseInt(localHourStr.replace(/^0/, ''));

        if (isNaN(currentHour)) return {{ status: 'WAIT', colorClass: 'wait', label: 'VENTER', icon: '⚪', borderClass: 'peak-window-wait' }};

        if (currentHour >= peakStart && currentHour < peakEnd) {{
            const remainingMin = (peakEnd - currentHour) * 60 - now.toLocaleString('en-US', {{ timeZone: tz, minute: '2-digit', hour12: false }}).split(':')[1];
            const remaining = peakEnd - currentHour;
            return {{ status: 'IN', colorClass: 'in', label: 'I PEAK-VINDU', icon: '🟡', borderClass: 'peak-window-in', detail: remaining + 't igjen' }};
        }}
        if (currentHour >= peakStart - 3 && currentHour < peakStart) {{
            const until = peakStart - currentHour;
            const untilMin = until * 60 - parseInt(now.toLocaleString('en-US', {{ timeZone: tz, minute: '2-digit', hour12: false }}).split(':')[1]) || 0;
            const detail = until === 1 ? 'om ' + Math.max(0, (60 - parseInt(now.toLocaleString('en-US', {{ timeZone: tz, minute: '2-digit', hour12: false }}).split(':')[1]) || 0)) + 'min' : 'om ' + until + 't';
            return {{ status: 'NEAR', colorClass: 'near', label: 'NÆRMER SEG', icon: '🟢', borderClass: 'peak-window-near', detail: detail }};
        }}
        if (currentHour >= peakEnd) {{
            const ago = currentHour - peakEnd;
            return {{ status: 'PAST', colorClass: 'past', label: 'PASSERT', icon: '🔴', borderClass: 'peak-window-past', detail: ago + 't siden' }};
        }}
        return {{ status: 'WAIT', colorClass: 'wait', label: 'VENTER', icon: '⚪', borderClass: 'peak-window-wait', detail: 'om ' + (peakStart - currentHour) + 't' }};
    }} catch(e) {{
        return {{ status: 'WAIT', colorClass: 'wait', label: 'VENTER', icon: '⚪', borderClass: 'peak-window-wait' }};
    }}
}}

function formatPeakWindow(peakStart, peakEnd) {{
    const s = String(peakStart).padStart(2, '0');
    const e = String(peakEnd).padStart(2, '0');
    return s + ':00-' + e + ':00';
}}

function updateCard(cityName, result) {{
    const card = document.getElementById('card-' + cityName.replace(/[^a-zA-Z0-9]/g, '_'));
    if (!card) return;

    const tempEl = card.querySelector('.card-temp');
    const statusEl = card.querySelector('.card-status');
    const updatedEl = card.querySelector('.card-updated');
    const confEl = card.querySelector('.card-info .conf');
    const dataInfoEl = card.querySelector('.card-info span:last-child');
    const peakWindowEl = card.querySelector('.peak-window-bar');

    if (result && result.temp !== null && result.temp !== undefined) {{
        const data = cityData[cityName];
        data.temps.push(result.temp);
        data.timestamps.push(result.time || new Date().toISOString());
        if (data.temps.length > MAX_DATA_POINTS) {{
            data.temps.shift();
            data.timestamps.shift();
        }}

        // Trend arrow: compare last 2 temps (or last 6 for longer trend)
        let trendArrow = '';
        let trendColor = '';
        if (data.temps.length >= 2) {{
            const shortDiff = data.temps[data.temps.length - 1] - data.temps[data.temps.length - 2];
            const longDiff = data.temps.length >= 6 ? data.temps[data.temps.length - 1] - data.temps[data.temps.length - 6] : shortDiff;
            if (Math.abs(shortDiff) < 0.15) {{ trendArrow = '→ stabil'; trendColor = '#8b949e'; }}
            else if (shortDiff > 0) {{ trendArrow = '↑ +' + shortDiff.toFixed(1) + '°C'; trendColor = '#3fb950'; }}
            else {{ trendArrow = '↓ ' + shortDiff.toFixed(1) + '°C'; trendColor = '#f85149'; }}
        }}

        tempEl.innerHTML = result.temp.toFixed(1) + '°C <span style="font-size:0.65rem;color:' + trendColor + ';font-weight:600;">' + trendArrow + '</span>';
        const cityMeta = result._city || {{ peakStart: 14, peakEnd: 17, tz: 'UTC' }};
        const peakInfo = computePeakStatus(cityName, result.temp, cityMeta);

        // Track all-day max — use API daily max when available, fall back to local tracking
        const apiDailyMax = result.dailyMax;
        if (apiDailyMax !== null && apiDailyMax !== undefined) {{
            if (data.allDayMax === undefined || data.allDayMax === null || apiDailyMax > data.allDayMax) {{
                data.allDayMax = apiDailyMax;
            }}
        }} else if (data.allDayMax === undefined || data.allDayMax === null || result.temp > data.allDayMax) {{
            data.allDayMax = result.temp;
        }}
        const allDayMaxEl = card.querySelector('.card-alltime-max');
        if (allDayMaxEl && data.allDayMax != null) {{
            const maxColor = data.allDayMax >= 40 ? '#f85149' : data.allDayMax >= 35 ? '#d2991d' : '#d2991d';
            allDayMaxEl.innerHTML = 'Døgnmaks hittil: <span style="color:' + maxColor + ';">' + data.allDayMax.toFixed(1) + '°C</span>';
            if (apiDailyMax !== null && apiDailyMax !== undefined) {{
                allDayMaxEl.innerHTML += ' <span style="font-size:0.6rem;color:var(--green);">📡 API</span>';
            }}
            const city = result._city || {{}};
            const ps = city.peakStart || 14;
            const pe = city.peakEnd || 17;
            if (peakInfo.localHour !== undefined && (peakInfo.localHour < ps || peakInfo.localHour > pe)) {{
                allDayMaxEl.innerHTML += ' <span style="font-size:0.65rem;color:var(--text-dim);">⚠️ utenfor vindu</span>';
            }}
        }}

        // Track peak resolution: if peakReached, mark city as resolved
        if (peakInfo.peakReached && result._city) {{
            result._city._peak_resolved = true;
        }}

        // Add lock indicator for confirmed peaks
        let lockBadge = '';
        if (peakInfo.peakReached) {{
            lockBadge = ' 🔒';
        }}

        statusEl.textContent = peakInfo.label + lockBadge;
        statusEl.className = 'card-status ' + peakInfo.cssClass;
        card.className = 'peak-card status-' + peakInfo.cssClass;

        const confPct = Math.round(peakInfo.confidence * 100);
        confEl.textContent = 'Konf: ' + confPct + '%';
        confEl.className = 'conf ' + (peakInfo.confidence >= 0.8 ? 'high' : peakInfo.confidence >= 0.6 ? 'medium' : 'low');

        updatedEl.textContent = 'Oppdatert: ' + new Date().toLocaleTimeString('no-NO');
        if (dataInfoEl) dataInfoEl.textContent = 'Data: ' + data.temps.length + ' pkt';
        drawSparkline(cityName, 'spark-' + cityName.replace(/[^a-zA-Z0-9]/g, '_'));

        // SMS Alert: daily max exceeded recommended spill (once per city per day)
        const rs = recSpills[cityName];
        if (rs && data.allDayMax != null && data.allDayMax > rs.spill && !smsSentToday[cityName]) {{
            smsSentToday[cityName] = true;
            const spillEl = card.querySelector('.card-rec-spill');
            if (spillEl) spillEl.innerHTML = 'Anbefalt spill: <b>' + rs.spill + 'C</b> (' + rs.strategy + ') <span style="color:var(--red);font-weight:700;">ALARM! Dognmaks > spill</span>';
            console.log('SMS ALERT: ' + cityName + ' daily max ' + data.allDayMax.toFixed(1) + 'C > recommended spill ' + rs.spill + 'C (' + rs.strategy + ')');
        }}

        // Update peak window status
        if (peakWindowEl && result._city) {{
            const city = result._city;
            const pwStatus = getPeakStatus(city.peakStart || 14, city.peakEnd || 17, city.tz);
            peakWindowEl.className = 'peak-window-bar ' + pwStatus.colorClass;
            peakWindowEl.innerHTML = pwStatus.icon + ' <span class="peak-window-time">⏰ Peak: ' + formatPeakWindow(city.peakStart || 14, city.peakEnd || 17) + ' ' + (city.tz.split('/')[1] || city.tz) + '</span> | <span class="peak-window-label">' + pwStatus.label + '</span>' + (pwStatus.detail ? ' <span>(' + pwStatus.detail + ')</span>' : '');
            card.classList.add(pwStatus.borderClass);
        }}
    }} else {{
        updatedEl.textContent = 'Kunne ikke hente';
    }}
}}

function buildCardHTML(city) {{
    const safeId = city.name.replace(/[^a-zA-Z0-9]/g, '_');
    const pwStatus = getPeakStatus(city.peakStart || 14, city.peakEnd || 17, city.tz);
    const tzShort = (city.tz || 'UTC').split('/')[1] || city.tz || 'UTC';

    // Check for actual peak data from quality log
    let actualPeakHTML = '';
    const apData = ACTUAL_PEAK_DATA[city.name];
    if (apData && apData.actual_peak != null) {{
        const hourMatch = (apData.peak_detected_at || '').match(/T(\\d{{2}}):/);
        const confirmedTime = hourMatch ? hourMatch[1] + ':00' : '';
        const timeLabel = confirmedTime ? ' (bekreftet ' + confirmedTime + ')' : '';
        const isWin = apData.actual_peak != null && apData.spill != null && Math.round(apData.actual_peak) === apData.spill;
        const barClass = isWin ? 'win' : 'loss';
        const winIcon = isWin ? '✅' : '❌';

        // Check for arbitrage opportunity (market prices available)
        let arbBadge = '';
        if (apData.spill != null) {{
            const mktKey = city.name.split(',')[0].trim() + '_' + apData.spill;
            if (MARKET_PRICES[mktKey] !== undefined) {{
                arbBadge = '<span class="arb-badge">💰 Arbitrasje-mulighet!</span>';
            }}
        }}

        actualPeakHTML = '<div class="actual-peak-bar ' + barClass + '">' +
          winIcon + ' FAKTISK PEAK: ' + apData.actual_peak.toFixed(1) + '°C' + timeLabel +
          arbBadge +
        '</div>';
    }}

    return '<div class="peak-card status-unknown ' + pwStatus.borderClass + '" id="card-' + safeId + '">' +
      '<div class="card-header">' +
        '<h3>' + pwStatus.icon + ' ' + city.name + '</h3>' +
        '<span class="card-status unknown">VENTER</span>' +
      '</div>' +
      '<div class="peak-window-bar ' + pwStatus.colorClass + '">' +
        pwStatus.icon + ' <span class="peak-window-time">⏰ Peak: ' + formatPeakWindow(city.peakStart || 14, city.peakEnd || 17) + ' ' + tzShort + '</span> | <span class="peak-window-label">' + pwStatus.label + '</span>' + (pwStatus.detail ? ' <span>(' + pwStatus.detail + ')</span>' : '') +
      '</div>' +
      actualPeakHTML +
      '<div class="card-temp">—°C</div>' +
      '<div class="card-alltime-max" style="font-size:0.82rem;font-weight:600;color:var(--orange);margin-bottom:4px;">Døgnmaks hittil: —</div>' +
      '<div class="card-rec-spill" style="font-size:0.78rem;color:var(--purple);margin-bottom:4px;">Anbefalt spill: laster...</div>' +
      '<div class="card-meta">' +
        '<span>' + city.lat.toFixed(2) + ', ' + city.lon.toFixed(2) + '</span>' +
        '<span>' + city.tz + '</span>' +
        '<span class="card-updated">—</span>' +
      '</div>' +
      '<canvas id="spark-' + safeId + '" class="sparkline-canvas" width="320" height="80"></canvas>' +
      '<div class="card-info">' +
        '<span class="conf">Konf: —%</span>' +
        '<span>Data: 0 pkt</span>' +
      '</div>' +
    '</div>';
}}

function isInPeakWindow(city) {{
    try {{
        const opts = {{ timeZone: city.tz || 'UTC', hour: '2-digit', hour12: false }};
        const localHour = parseInt(new Date().toLocaleString('en-US', opts).replace(/^0/, ''), 10);
        if (isNaN(localHour)) return false;
        const ps = city.peakStart || 14;
        const pe = city.peakEnd || 17;
        return localHour >= ps && localHour <= pe;
    }} catch(e) {{ return false; }}
}}

// ---- Per-City Start/Stop (independent — no reset of other cities) ----

function ensureCardExists(city) {{
    const safeId = city.name.replace(/[^a-zA-Z0-9]/g, '_');
    let card = document.getElementById('card-' + safeId);
    if (!card) {{
        const grid = document.getElementById('cards-grid');
        const emptyState = grid.querySelector('.empty-state');
        if (emptyState) emptyState.remove();
        const tempDiv = document.createElement('div');
        tempDiv.innerHTML = buildCardHTML(city);
        grid.appendChild(tempDiv.firstElementChild);
        card = document.getElementById('card-' + safeId);
    }}
    if (card) {{
        const canvas = document.getElementById('spark-' + safeId);
        if (canvas) {{
            canvas.width = canvas.offsetWidth || 320;
            canvas.height = 80;
        }}
    }}
    return card;
}}

function removeCard(cityName) {{
    const safeId = cityName.replace(/[^a-zA-Z0-9]/g, '_');
    const card = document.getElementById('card-' + safeId);
    if (card) card.remove();
    const grid = document.getElementById('cards-grid');
    if (grid.children.length === 0) {{
        grid.innerHTML = '<div class="empty-state"><div class="icon">📈</div><p>Velg byer over for a begynne.</p><p style="font-size:0.8rem; margin-top:8px;">Byer i peak-vindu auto-velges.</p></div>';
    }}
}}

function startCity(cityName) {{
    if (activeCities.has(cityName)) return;
    const city = cityLookup.get(cityName);
    if (!city) return;

    initCityData(cityName);
    activeCities.add(cityName);
    ensureCardExists(city);

    fetch('_model_quality_log.json')
        .then(r => r.json())
        .then(log => {{
            const runs = log.runs || [];
            if (runs.length > 0) {{
                const p = (runs[runs.length-1].predictions || {{}})[cityName];
                if (p && p.bma_mean != null) cityData[cityName].bmaLine = p.bma_mean;
                // Load recommended strategy spill for this city
                const strats = p?.strategies || {{}};
                const sigma = strats.sigma || {{}};
                const p5 = strats.p5 || {{}};
                const meanS = strats.mean || {{}};
                // Determine best strategy (simple heuristic: prefer mean if available, else sigma)
                if (meanS.spill != null) {{
                    recSpills[cityName] = {{strategy: 'mean', spill: meanS.spill}};
                }} else if (sigma.spill != null) {{
                    recSpills[cityName] = {{strategy: 'sigma', spill: sigma.spill}};
                }}
                // Update the card's spill display
                const safeId = cityName.replace(/[^a-zA-Z0-9]/g, '_');
                const spillEl = document.getElementById('card-' + safeId)?.querySelector('.card-rec-spill');
                if (spillEl && recSpills[cityName]) {{
                    const rs = recSpills[cityName];
                    spillEl.innerHTML = 'Anbefalt spill: <b>' + rs.spill + 'C</b> (' + rs.strategy + ')';
                }}
            }}
        }}).catch(() => {{}});

    fetchOneCity(city);

    const pollMs = isInPeakWindow(city) ? PEAK_POLL_MS : NORMAL_POLL_MS;
    monitoringIntervals[cityName] = setInterval(() => {{
        if (!activeCities.has(cityName)) {{ clearInterval(monitoringIntervals[cityName]); delete monitoringIntervals[cityName]; return; }}
        const curPoll = isInPeakWindow(city) ? PEAK_POLL_MS : NORMAL_POLL_MS;
        if (curPoll !== pollMs && monitoringIntervals[cityName]) {{
            clearInterval(monitoringIntervals[cityName]);
            monitoringIntervals[cityName] = setInterval(() => fetchOneCity(city), curPoll);
        }}
        fetchOneCity(city);
    }}, pollMs);

    updateStatusBar();
}}

function stopCity(cityName) {{
    activeCities.delete(cityName);
    if (monitoringIntervals[cityName]) {{
        clearInterval(monitoringIntervals[cityName]);
        delete monitoringIntervals[cityName];
    }}
    const safeId = cityName.replace(/[^a-zA-Z0-9]/g, '_');
    const card = document.getElementById('card-' + safeId);
    if (card) {{
        const statusEl = card.querySelector('.card-status');
        if (statusEl) {{ statusEl.textContent = 'PAUSET'; statusEl.className = 'card-status unknown'; }}
    }}
    updateStatusBar();
}}

async function fetchOneCity(city) {{
    try {{
        const result = await fetchCurrentTemp(city);
        if (result) {{
            result._city = city;
            updateCard(city.name, result);
        }}
    }} catch(e) {{ /* skip */ }}
}}

// Legacy buttons — start/stop all checked
function startMonitoring() {{
    const checked = getCheckedCities();
    checked.forEach(name => startCity(name));
}}

function stopMonitoring() {{
    const all = Array.from(activeCities);
    all.forEach(name => stopCity(name));
}}

// ---- Dynamic Peak Window Management ----
const manuallySelectedCities = new Set();  // Cities user manually checked

function computeCurrentPeakWindowCities() {{
    // Browser-side timezone math — same logic as Python pipeline (0 API calls)
    const now = new Date();
    const result = [];
    ALL_CITIES.forEach(city => {{
        try {{
            const opts = {{ timeZone: city.tz || 'UTC', hour: '2-digit', hour12: false }};
            const localHour = parseInt(now.toLocaleString('en-US', opts).replace(/^0/, ''), 10);
            if (isNaN(localHour)) return;
            const ps = city.peakStart || 14;
            const pe = city.peakEnd || 17;
            // Live peak window: 1 hour before peak_start to 1 hour after peak_end
            if ((ps - 1) <= localHour && localHour <= (pe + 1)) {{
                result.push(city.name);
            }}
        }} catch(e) {{}}
    }});
    return result;
}}

function syncAutoCities() {{
    const currentPeakCities = new Set(computeCurrentPeakWindowCities());
    const nowActive = new Set(activeCities);

    // 1. Start cities that entered the peak window (and not manually stopped)
    currentPeakCities.forEach(name => {{
        if (!nowActive.has(name) && !manuallySelectedCities.has(name)) {{
            // Auto-start new city — check its box too
            const cb = document.querySelector('#city-selector input[value="' + name + '"]');
            if (cb && !cb.checked) {{
                cb.checked = true;
                // Mark label with indicator
                const label = cb.parentElement;
                if (label && !label.textContent.startsWith('🔴')) {{
                    label.innerHTML = '<input type="checkbox" value="' + name + '" onchange="onCityToggle(this)" checked> 🔴 ' + name;
                }}
            }}
            startCity(name);
        }}
    }});

    // 2. Remove cities that left the peak window (auto-managed only, keep manual)
    nowActive.forEach(name => {{
        if (!currentPeakCities.has(name) && !manuallySelectedCities.has(name)) {{
            const cb = document.querySelector('#city-selector input[value="' + name + '"]');
            if (cb) {{
                cb.checked = false;
                const label = cb.parentElement;
                if (label) {{
                    label.innerHTML = '<input type="checkbox" value="' + name + '" onchange="onCityToggle(this)"> ' + name;
                }}
            }}
            // Fully remove card (not just pause) — city left peak window
            stopCity(name);
            removeCard(name);
        }}
    }});

    updateStatusBar();
}}

function onCityToggle(checkbox) {{
    const cityName = checkbox.value;
    if (checkbox.checked) {{
        manuallySelectedCities.add(cityName);  // User manually checked it
        // Update label to show it's selected
        const label = checkbox.parentElement;
        if (label && !label.textContent.startsWith('🔴')) {{
            label.innerHTML = '<input type="checkbox" value="' + cityName + '" onchange="onCityToggle(this)" checked> 🔴 ' + cityName;
        }}
        startCity(cityName);
    }} else {{
        manuallySelectedCities.delete(cityName);  // User manually unchecked
        stopCity(cityName);
    }}
}}

// ---- Init: auto-start + periodic sync ----
(function initDynamicAuto() {{
    // Initial auto-start
    setTimeout(() => {{
        const peakCities = computeCurrentPeakWindowCities();
        peakCities.forEach(name => {{
            const cb = document.querySelector('#city-selector input[value="' + name + '"]');
            if (cb && !cb.checked) cb.checked = true;
            startCity(name);
        }});
        updateStatusBar();
    }}, 500);

    // Re-sync every 2 minutes — add new, remove stale (0 API calls)
    setInterval(() => {{
        syncAutoCities();
    }}, 120000);
}})();

window.addEventListener('resize', () => {{
    activeCities.forEach(cityName => {{
        const safeId = cityName.replace(/[^a-zA-Z0-9]/g, '_');
        const canvas = document.getElementById('spark-' + safeId);
        if (canvas) {{
            canvas.width = canvas.offsetWidth || 320;
            drawSparkline(cityName, 'spark-' + safeId);
        }}
    }});
}});
</script>
</body>
</html>"""


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
    parser.add_argument(
        "--index",
        action="store_true",
        help="Generate landing page (index.html) with links to all dashboards",
    )
    parser.add_argument(
        "--peak",
        action="store_true",
        help="Generate live peak detection page (_peak_detection.html)",
    )
    args = parser.parse_args()

    if args.index:
        html = _generate_index_html()
        INDEX_FILE.write_text(html, encoding="utf-8")
        print(f"Index page written to: {INDEX_FILE}")
    elif args.peak:
        html = _generate_peak_detection_html()
        PEAK_DETECTION_FILE.write_text(html, encoding="utf-8")
        print(f"Peak detection page written to: {PEAK_DETECTION_FILE}")
    elif args.all_cities:
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
