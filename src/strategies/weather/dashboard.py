"""
Weather Trading Dashboard — NiceGUI + Plotly.

Standalone dashboard for the multi-model BMA weather trading strategy.
Can run independently or be embedded as a tab in the main GUI.

Start:
    python -m src.strategies.weather.dashboard
    python -m src.strategies.weather.dashboard --dry-run --cities NYC,London

Open: http://localhost:8090
"""

from __future__ import annotations

import argparse
import math
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import plotly.graph_objects as go
from nicegui import app, ui

# =============================================================================
# Constants
# =============================================================================

APP_TITLE = "🌤️ Vær-Trading Dashboard"
APP_PORT = 8090
APP_HOST = "127.0.0.1"
REFRESH_INTERVAL_SEC = 30.0

# Model colors for ensemble visualization
MODEL_COLORS: dict[str, str] = {
    "ecmwf_ifs": "#1e40af",   # ECMWF — deep blue
    "gfs":        "#dc2626",   # GFS — red
    "icon":       "#7c3aed",   # ICON — purple
    "gem":        "#059669",   # GEM — green
    "ukmo":       "#ea580c",   # UKMO — orange
    "jma":        "#0891b2",   # JMA — cyan
    "hrrr":       "#d97706",   # HRRR — amber
    "aifs":       "#4f46e5",   # AIFS — indigo
}

MODEL_DISPLAY_NAMES: dict[str, str] = {
    "ecmwf_ifs": "ECMWF",
    "gfs":        "GFS",
    "icon":       "ICON",
    "gem":        "GEM",
    "ukmo":       "UKMO",
    "jma":        "JMA",
    "hrrr":       "HRRR",
    "aifs":       "AIFS",
}

# City data — flags, ICAO codes, coordinates
CITY_DATA: dict[str, dict[str, Any]] = {
    "new york city":  {"flag": "🇺🇸", "icao": "KLGA", "region": "Nord-Amerika"},
    "los angeles":    {"flag": "🇺🇸", "icao": "KLAX", "region": "Nord-Amerika"},
    "chicago":        {"flag": "🇺🇸", "icao": "KORD", "region": "Nord-Amerika"},
    "miami":          {"flag": "🇺🇸", "icao": "KMIA", "region": "Nord-Amerika"},
    "dallas":         {"flag": "🇺🇸", "icao": "KDAL", "region": "Nord-Amerika"},
    "denver":         {"flag": "🇺🇸", "icao": "KBKF", "region": "Nord-Amerika"},
    "phoenix":        {"flag": "🇺🇸", "icao": "KPHX", "region": "Nord-Amerika"},
    "seattle":        {"flag": "🇺🇸", "icao": "KSEA", "region": "Nord-Amerika"},
    "san francisco":  {"flag": "🇺🇸", "icao": "KSFO", "region": "Nord-Amerika"},
    "atlanta":        {"flag": "🇺🇸", "icao": "KATL", "region": "Nord-Amerika"},
    "boston":         {"flag": "🇺🇸", "icao": "KBOS", "region": "Nord-Amerika"},
    "las vegas":      {"flag": "🇺🇸", "icao": "KLAS", "region": "Nord-Amerika"},
    "london heathrow":{"flag": "🇬🇧", "icao": "EGLL", "region": "Europa"},
    "paris":          {"flag": "🇫🇷", "icao": "LFPG", "region": "Europa"},
    "berlin":         {"flag": "🇩🇪", "icao": "EDDB", "region": "Europa"},
    "madrid":         {"flag": "🇪🇸", "icao": "LEMD", "region": "Europa"},
    "rome":           {"flag": "🇮🇹", "icao": "LIRF", "region": "Europa"},
    "amsterdam":      {"flag": "🇳🇱", "icao": "EHAM", "region": "Europa"},
    "tokyo":          {"flag": "🇯🇵", "icao": "RJTT", "region": "Asia"},
    "singapore":      {"flag": "🇸🇬", "icao": "WSSS", "region": "Asia"},
    "sydney":         {"flag": "🇦🇺", "icao": "YSSY", "region": "Oseania"},
    "dubai":          {"flag": "🇦🇪", "icao": "OMDB", "region": "Midtøsten"},
    "mumbai":         {"flag": "🇮🇳", "icao": "VABB", "region": "Asia"},
    "saopaulo":       {"flag": "🇧🇷", "icao": "SBGR", "region": "Sør-Amerika"},
    "mexico city":    {"flag": "🇲🇽", "icao": "MMMX", "region": "Nord-Amerika"},
    "toronto":        {"flag": "🇨🇦", "icao": "CYYZ", "region": "Nord-Amerika"},
    "seoul":          {"flag": "🇰🇷", "icao": "RKSI", "region": "Asia"},
    "moscow":         {"flag": "🇷🇺", "icao": "UUEE", "region": "Europa"},
    "stockholm":      {"flag": "🇸🇪", "icao": "ESSA", "region": "Europa"},
    "oslo":           {"flag": "🇳🇴", "icao": "ENGM", "region": "Europa"},
}

# =============================================================================
# Dashboard State
# =============================================================================


@dataclass
class DashboardState:
    """Mutable state for the weather dashboard."""

    active_cities: int = 12
    total_cities: int = 30
    open_positions: int = 8
    total_exposure_usd: float = 2450.0
    daily_pnl_usd: float = 342.18
    win_rate_30d: float = 0.87
    sharpe_90d: float = 2.4
    ecmwf_age_hours: float = 2.0
    gfs_age_hours: float = 1.0
    total_bankroll: float = 5000.0
    allocated_usd: float = 2450.0
    available_usd: float = 2300.0
    in_orders_usd: float = 250.0
    var_95: float = 324.0
    expected_shortfall: float = 412.0
    max_drawdown: float = 0.082
    daily_pnl_history: list[float] = field(default_factory=lambda: [
        0.0, 45.2, -12.3, 67.8, -23.4, 89.1, 34.5, -8.9, 56.7, 42.3,
        -15.6, 78.9, 23.4, -5.6, 91.2, 34.5, -19.8, 67.3, 52.1, 28.9,
    ])

    # City bucket data — map of city -> list of bucket dicts
    city_buckets: dict[str, list[dict[str, Any]]] = field(default_factory=dict)

    # Positions
    positions: list[dict[str, Any]] = field(default_factory=list)

    # Log entries
    log_entries: list[tuple[str, str]] = field(default_factory=list)

    # System status
    data_source_status: dict[str, str] = field(default_factory=lambda: {
        "Open-Meteo": "green",
        "ECMWF IFS": "green",
        "GFS": "green",
        "METAR": "green",
        "Satellite": "yellow",
        "CLOB": "green",
        "Redis": "red",
        "PostgreSQL": "red",
    })

    model_ages: dict[str, str] = field(default_factory=lambda: {
        "ECMWF 00Z": "2.3t siden",
        "GFS 06Z": "1.1t siden",
        "HRRR": "14 min siden",
    })

    api_limits: dict[str, str] = field(default_factory=lambda: {
        "Open-Meteo": "847 / 10 000",
        "CLOB": "234 / 9 000",
        "METAR": "45 / ∞",
    })


_state = DashboardState()


# =============================================================================
# Mock / Simulation Data
# =============================================================================

def _generate_mock_city_data() -> dict[str, list[dict[str, Any]]]:
    """Generate realistic mock bucket data for demo cities."""
    cities = [
        ("new york city", 78.0, 3.5, [82, 84, 86, 88, 90, 92]),
        ("chicago", 74.5, 4.2, [80, 82, 84, 86, 88]),
        ("los angeles", 82.0, 2.8, [85, 88, 90, 92, 95]),
        ("miami", 90.0, 1.8, [88, 90, 92, 94, 96]),
        ("london heathrow", 22.0, 3.0, [26, 28, 30, 32]),
        ("tokyo", 31.0, 2.5, [30, 32, 34, 36]),
        ("dallas", 96.0, 3.0, [95, 98, 100, 102, 105]),
        ("phoenix", 105.0, 2.0, [102, 105, 108, 110, 112]),
        ("denver", 88.0, 5.0, [85, 88, 90, 92, 95]),
        ("seattle", 72.0, 3.5, [70, 72, 74, 76, 78]),
        ("paris", 25.0, 2.8, [24, 26, 28, 30, 32]),
        ("sydney", 20.0, 3.0, [22, 24, 26, 28, 30]),
    ]

    result: dict[str, list[dict[str, Any]]] = {}
    for city, mean_f, std_f, buckets_f in cities:
        import random
        random.seed(hash(city) % 2**31)

        bucket_rows = []
        for bf in buckets_f:
            z = (bf - mean_f) / max(std_f, 0.5)
            model_p = 1.0 - _norm_cdf(z)
            model_p = max(0.02, min(0.98, model_p))
            # Market underestimates by 10-25%
            market_p = model_p * (0.75 + random.uniform(0, 0.15))
            edge = model_p - market_p
            pos = None
            if edge > 0.08:
                pos = f"YES ${random.randint(40, 150)}"
            bucket_rows.append({
                "label": f">{bf}°F" if bf > 100 else f">{bf}°F",
                "market_pct": round(market_p * 100),
                "model_pct": round(model_p * 100),
                "edge_pct": round(edge * 100),
                "edge_color": "green" if edge > 0.06 else ("yellow" if edge > 0.02 else "red"),
                "position": pos,
            })
        result[city] = bucket_rows
    return result


def _generate_mock_positions() -> list[dict[str, Any]]:
    """Generate mock open positions."""
    import random
    random.seed(42)
    positions = []
    cities = ["New York", "Chicago", "Miami", "Dallas", "London", "Tokyo", "Phoenix", "Denver"]
    for i, city in enumerate(cities):
        entry = round(0.30 + random.uniform(0, 0.25), 2)
        current = round(entry + random.uniform(-0.20, 0.25), 2)
        pnl = round((current - entry) * random.randint(50, 200), 2)
        positions.append({
            "market": f"{city} — Maks temp",
            "bucket": f">{75 + i * 3}°F",
            "side": "YES",
            "size": f"${random.randint(50, 200)}",
            "entry": f"${entry:.2f}",
            "current": f"${current:.2f}",
            "pnl": f"${pnl:+,.2f}",
            "pnl_color": "green" if pnl > 0 else "red",
            "time_left": f"{random.randint(1, 5)}d {random.randint(0, 23)}t",
        })
    return positions


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    a1, a2, a3, a4, a5 = 0.254829592, -0.284496736, 1.421413741, -1.453152027, 1.061405429
    p = 0.2316419
    sign = 1.0 if x >= 0 else -1.0
    x_abs = abs(x)
    t = 1.0 / (1.0 + p * x_abs)
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x_abs * x_abs / 2.0)
    return 0.5 * (1.0 + sign * y)


# =============================================================================
# Plotly Charts
# =============================================================================

def _create_thermometer_chart(city: str, mean_f: float, std_f: float,
                               buckets_f: list[float]) -> go.Figure:
    """Create a vertical thermometer visualization for a city."""
    import random
    random.seed(hash(city) % 2**31)

    fig = go.Figure()

    # Background gradient (simulated thermometer tube)
    temp_range = list(range(int(mean_f - 20), int(mean_f + 25)))
    colors_scale = []
    for t in temp_range:
        if t < mean_f - 10:
            colors_scale.append((0.0, 0.3 + (t - min(temp_range)) / len(temp_range) * 0.5, 1.0))
        elif t < mean_f:
            colors_scale.append((0.2 + (t - mean_f + 10) / 10 * 0.8, 0.8, 0.3))
        elif t < mean_f + 10:
            colors_scale.append((1.0, 0.7 - (t - mean_f) / 10 * 0.5, 0.1))
        else:
            colors_scale.append((1.0, 0.2, 0.0))

    # Observed max line
    fig.add_trace(go.Scatter(
        x=[0, 1],
        y=[mean_f, mean_f],
        mode="lines",
        line=dict(color="white", width=2, dash="solid"),
        name="Observert maks",
        showlegend=False,
    ))

    # Predicted max line (dashed)
    fig.add_trace(go.Scatter(
        x=[0, 1],
        y=[mean_f + random.uniform(-2, 3), mean_f + random.uniform(-2, 3)],
        mode="lines",
        line=dict(color="rgba(59,130,246,0.7)", width=2, dash="dash"),
        name="Predikert maks",
        showlegend=False,
    ))

    # Bucket markers
    for bf in buckets_f:
        marker_color = "green" if bf > mean_f + std_f else ("yellow" if bf > mean_f else "red")
        fig.add_trace(go.Scatter(
            x=[0.5],
            y=[bf],
            mode="markers",
            marker=dict(color=marker_color, size=10, symbol="diamond"),
            name=f">{bf}°F",
            showlegend=False,
            hovertemplate=f"Bucket: >{bf}°F<extra></extra>",
        ))

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=5, r=5, t=5, b=5),
        height=180,
        width=60,
        xaxis=dict(showgrid=False, zeroline=False, showticklabels=False, range=[-0.2, 1.2]),
        yaxis=dict(showgrid=True, gridcolor="rgba(255,255,255,0.08)", range=[mean_f - 20, mean_f + 25]),
        showlegend=False,
    )
    return fig


def _create_ensemble_distribution_chart(city: str, mean_f: float, std_f: float) -> go.Figure:
    """Plotly histogram + KDE over ensemble temperature predictions."""
    import random
    random.seed(hash(city + "ens") % 2**31)

    # Simulate individual model predictions
    models = ["ecmwf_ifs", "gfs", "icon", "gem", "ukmo", "jma", "hrrr", "aifs"]
    individual_preds: dict[str, float] = {}
    for m in models:
        individual_preds[m] = mean_f + random.uniform(-1.5 * std_f, 1.5 * std_f)

    fig = go.Figure()

    # Individual model points
    for m, pred in individual_preds.items():
        color = MODEL_COLORS.get(m, "#888888")
        name = MODEL_DISPLAY_NAMES.get(m, m)
        fig.add_trace(go.Scatter(
            x=[pred], y=[1],
            mode="markers+text",
            marker=dict(color=color, size=14, symbol="circle"),
            text=[name[:6]],
            textposition="top center",
            name=name,
            showlegend=False,
            hoverinfo="text",
            hovertext=f"{name}: {pred:.1f}°F",
        ))

    # KDE curve (normal approximation)
    x_range = [mean_f - 4 * std_f + i * 0.2 for i in range(int(8 * std_f / 0.2))]  # noqa
    kde_y = [math.exp(-0.5 * ((x - mean_f) / std_f) ** 2) for x in x_range]
    fig.add_trace(go.Scatter(
        x=x_range, y=kde_y,
        mode="lines",
        fill="tozeroy",
        fillcolor="rgba(59,130,246,0.15)",
        line=dict(color="#3b82f6", width=2),
        name="BMA-fordeling",
    ))

    # Market implied distribution (wider, lower peak)
    market_mean = mean_f * 0.97
    market_std = std_f * 1.2
    market_y = [math.exp(-0.5 * ((x - market_mean) / market_std) ** 2) * 0.7 for x in x_range]
    fig.add_trace(go.Scatter(
        x=x_range, y=market_y,
        mode="lines",
        line=dict(color="#ef4444", width=2, dash="dash"),
        name="Polymarket implisitt",
    ))

    # Vertical line at mean
    fig.add_vline(x=mean_f, line_dash="dot", line_color="white", opacity=0.4,
                  annotation_text=f"{mean_f:.1f}°F")

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=5, r=5, t=35, b=5),
        height=280,
        title=dict(text=f"Ensemble-fordeling — {city.title()}", font=dict(size=13, color="#e2e8f0")),
        xaxis=dict(title="Temperatur (°F)", gridcolor="rgba(255,255,255,0.06)", color="#94a3b8"),
        yaxis=dict(showticklabels=False, showgrid=False, zeroline=False),
        legend=dict(font=dict(color="#94a3b8", size=10), orientation="h", yanchor="bottom", y=1.02),
        hovermode="x",
    )
    return fig


def _create_bucket_edge_chart(buckets: list[dict[str, Any]]) -> go.Figure:
    """Bar chart comparing model vs market probability per bucket."""
    labels = [b["label"] for b in buckets]
    model_pcts = [b["model_pct"] for b in buckets]
    market_pcts = [b["market_pct"] for b in buckets]
    edges = [b["edge_pct"] for b in buckets]

    fig = go.Figure()

    fig.add_trace(go.Bar(
        name="Modell (BMA)",
        x=labels,
        y=model_pcts,
        marker_color="#3b82f6",
        text=[f"{v}%" for v in model_pcts],
        textposition="outside",
        textfont=dict(size=10, color="#93c5fd"),
    ))

    fig.add_trace(go.Bar(
        name="Marked (Polymarket)",
        x=labels,
        y=market_pcts,
        marker_color="#ef4444",
        text=[f"{v}%" for v in market_pcts],
        textposition="outside",
        textfont=dict(size=10, color="#fca5a5"),
    ))

    # Edge indicators
    for i, (label, edge) in enumerate(zip(labels, edges)):
        color = "#22c55e" if edge > 6 else ("#eab308" if edge > 2 else "#ef4444")
        fig.add_annotation(
            x=label, y=max(model_pcts[i], market_pcts[i]) + 8,
            text=f"+{edge}%" if edge > 0 else f"{edge}%",
            showarrow=False,
            font=dict(color=color, size=11, family="monospace"),
        )

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=5, r=5, t=5, b=5),
        height=260,
        barmode="group",
        bargap=0.15,
        xaxis=dict(gridcolor="rgba(255,255,255,0.04)", color="#94a3b8"),
        yaxis=dict(title="Sannsynlighet (%)", gridcolor="rgba(255,255,255,0.06)",
                    color="#94a3b8", range=[0, max(max(model_pcts), max(market_pcts)) + 20]),
        legend=dict(font=dict(color="#94a3b8", size=10), orientation="h"),
    )
    return fig


def _create_calibration_heatmap(buckets: list[dict[str, Any]]) -> go.Figure:
    """Brier score heatmap — last 30 days per bucket."""
    import random
    random.seed(12345)

    bucket_labels = [b["label"] for b in buckets]
    days = [f"D-{29 - i}" for i in range(30)]

    z = []
    for _ in bucket_labels:
        row = []
        for _ in days:
            score = random.uniform(0.02, 0.25)
            row.append(score)
        z.append(row)

    fig = go.Figure(data=go.Heatmap(
        z=z,
        x=days,
        y=bucket_labels,
        colorscale=[
            [0.0, "#22c55e"],
            [0.3, "#eab308"],
            [0.6, "#f97316"],
            [1.0, "#ef4444"],
        ],
        zmin=0.0,
        zmax=0.25,
        text=[[f"{val:.3f}" for val in row] for row in z],
        texttemplate="%{text}",
        textfont=dict(size=8, color="white"),
        hoverongaps=False,
        colorbar=dict(title="Brier Score", titlefont=dict(color="#94a3b8", size=10),
                       tickfont=dict(color="#94a3b8", size=9)),
    ))

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=5, r=5, t=30, b=5),
        height=220,
        title=dict(text="Kalibrering — Brier Score (30d)", font=dict(size=12, color="#e2e8f0")),
        xaxis=dict(title="", tickfont=dict(size=7, color="#64748b"), nticks=10),
        yaxis=dict(title="", tickfont=dict(size=10, color="#94a3b8")),
    )
    return fig


def _create_daily_pnl_chart(pnl_history: list[float]) -> go.Figure:
    """Plot daily P&L line chart."""
    days = list(range(1, len(pnl_history) + 1))
    cumulative = []
    cum = 0
    for v in pnl_history:
        cum += v
        cumulative.append(cum)

    fig = go.Figure()

    fig.add_trace(go.Bar(
        x=days,
        y=pnl_history,
        name="Daglig P&L",
        marker_color=["#22c55e" if v >= 0 else "#ef4444" for v in pnl_history],
        opacity=0.6,
    ))

    fig.add_trace(go.Scatter(
        x=days,
        y=cumulative,
        mode="lines+markers",
        name="Kumulativ",
        line=dict(color="#3b82f6", width=2.5),
        marker=dict(size=5, color="#60a5fa"),
    ))

    fig.add_hline(y=0, line_dash="dot", line_color="rgba(255,255,255,0.2)")

    fig.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        margin=dict(l=5, r=5, t=5, b=5),
        height=240,
        xaxis=dict(title="Dag", gridcolor="rgba(255,255,255,0.04)", color="#94a3b8"),
        yaxis=dict(title="USD", gridcolor="rgba(255,255,255,0.06)", color="#94a3b8"),
        legend=dict(font=dict(color="#94a3b8", size=10), orientation="h"),
        hovermode="x",
    )
    return fig


# =============================================================================
# UI Components
# =============================================================================

def _dark_card() -> ui.card:
    """Create a standard dark-themed card."""
    return ui.card().style(
        "background: rgba(15,23,42,0.9); "
        "border: 1px solid rgba(255,255,255,0.08); "
        "border-radius: 12px; padding: 16px;"
    )


def _kpi_card(title: str, value: str, color: str, subtitle: str = "",
              icon: str = "") -> None:
    """Render a KPI summary card."""
    colors = {
        "green":  ("#22c55e", "rgba(34,197,94,0.1)"),
        "blue":   ("#3b82f6", "rgba(59,130,246,0.1)"),
        "purple": ("#a855f7", "rgba(168,85,247,0.1)"),
        "orange": ("#f97316", "rgba(249,115,22,0.1)"),
        "red":    ("#ef4444", "rgba(239,68,68,0.1)"),
        "yellow": ("#eab308", "rgba(234,179,8,0.1)"),
    }
    fg, bg = colors.get(color, colors["blue"])

    with ui.card().style(
        f"background: {bg}; border: 1px solid rgba(255,255,255,0.06); "
        "border-radius: 12px; padding: 14px 16px; min-width: 160px; flex: 1;"
    ):
        with ui.row().classes("items-center gap-2"):
            if icon:
                ui.icon(icon).style(f"color: {fg}; font-size: 1.1rem;")
            ui.label(title).style("color: #94a3b8; font-size: 0.72rem; font-weight: 500;")
        ui.label(value).style(
            f"color: {fg}; font-size: 1.6rem; font-weight: 700; margin-top: 2px;"
        )
        if subtitle:
            ui.label(subtitle).style("color: #64748b; font-size: 0.68rem; margin-top: 2px;")


def _section_header(title: str, icon: str = "") -> None:
    """Render a section divider with title."""
    with ui.row().classes("items-center gap-2 w-full mt-2 mb-1"):
        if icon:
            ui.icon(icon).style("color: #60a5fa; font-size: 1.1rem;")
        ui.label(title).style(
            "color: #e2e8f0; font-size: 0.95rem; font-weight: 600;"
        )
    ui.separator().style("background: rgba(255,255,255,0.06); margin: 4px 0 8px 0;")


def _status_dot(color: str) -> str:
    """Return colored dot HTML."""
    dot_colors = {
        "green":  "#22c55e",
        "yellow": "#eab308",
        "red":    "#ef4444",
        "gray":   "#64748b",
    }
    c = dot_colors.get(color, "#64748b")
    return f'<span style="color:{c};font-size:1.1em;">●</span>'


# =============================================================================
# Dashboard Builder
# =============================================================================


class WeatherDashboard:
    """Main weather dashboard class — builds and manages the NiceGUI UI."""

    def __init__(self, dry_run: bool = True, cities_filter: list[str] | None = None,
                 capital: float = 5000.0, headless: bool = False) -> None:
        self.dry_run = dry_run
        self.cities_filter = cities_filter or []
        self.capital = capital
        self.headless = headless
        self._state = _state
        self._state.total_bankroll = capital

        # Refs for dynamic UI elements
        self._kpi_labels: dict[str, ui.label] = {}
        self._city_card_containers: dict[str, ui.column] = {}
        self._city_expanded: dict[str, bool] = {}
        self._positions_table: ui.table | None = None
        self._log_container: ui.column | None = None
        self._data_source_grid: ui.row | None = None

    def build(self) -> None:
        """Build the entire dashboard UI."""
        # --- Header ---
        with ui.header().style(
            "background: linear-gradient(135deg, #0f172a 0%, #1e1b4b 100%); "
            "padding: 10px 24px; border-bottom: 1px solid rgba(59,130,246,0.15);"
        ):
            with ui.row().classes("items-center w-full"):
                ui.label(APP_TITLE).style(
                    "font-size: 1.25rem; font-weight: 700; "
                    "background: linear-gradient(90deg, #60a5fa, #818cf8); "
                    "-webkit-background-clip: text; -webkit-text-fill-color: transparent;"
                )
                ui.space()

                # Dry-run badge
                if self.dry_run:
                    ui.badge("🧪 DRY-RUN").style(
                        "background: rgba(234,179,8,0.15); color: #eab308; "
                        "padding: 4px 14px; border-radius: 14px; font-size: 0.8rem; font-weight: 600;"
                    )
                else:
                    ui.badge("🔴 LIVE").style(
                        "background: rgba(239,68,68,0.15); color: #ef4444; "
                        "padding: 4px 14px; border-radius: 14px; font-size: 0.8rem; font-weight: 600;"
                    )

                ui.button("🔄 Oppdater", on_click=self._manual_refresh).props("flat").style(
                    "color: #60a5fa;"
                )

        # --- Scrollable Content ---
        with ui.column().classes("w-full overflow-y-auto").style("padding: 0 24px 24px 24px;"):

            # =================================================================
            # SECTION A: KPI Overview
            # =================================================================
            self._build_kpi_row()

            # =================================================================
            # SECTION B: City Cards Grid
            # =================================================================
            _section_header("🏙️ By-oversikt", icon="location_city")
            self._build_city_grid()

            # =================================================================
            # SECTION D: Portfolio Overview
            # =================================================================
            _section_header("📊 Porteføljeoversikt", icon="account_balance_wallet")
            self._build_portfolio_section()

            # =================================================================
            # SECTION E: System Status
            # =================================================================
            _section_header("🖥️ Systemstatus", icon="monitor_heart")
            self._build_system_status()

        # --- Footer ---
        with ui.footer().style(
            "background: rgba(15,23,42,0.95); border-top: 1px solid rgba(255,255,255,0.06); "
            "padding: 8px 24px;"
        ):
            with ui.row().classes("items-center w-full"):
                ui.label("Vær-Trading Bot v2.0 · BMA Multi-Model + METAR + Satellite").style(
                    "color: #64748b; font-size: 0.72rem;"
                )
                ui.space()
                ui.label(f"Kapital: ${self.capital:,.0f}").style(
                    "color: #64748b; font-size: 0.72rem;"
                )

        # --- Auto-refresh timer ---
        ui.timer(REFRESH_INTERVAL_SEC, self._auto_refresh)

    # =========================================================================
    # Section A: KPI Row
    # =========================================================================

    def _build_kpi_row(self) -> None:
        """Build the top KPI overview row."""
        s = self._state
        with ui.row().classes("w-full gap-3 mt-3 flex-wrap"):
            active_label = f"{s.active_cities} av {s.total_cities}"
            dot_color = "green" if s.active_cities > 10 else "yellow"
            _kpi_card("Aktive byer", active_label, dot_color,
                       subtitle="med BMA-dekning", icon="location_city")

            _kpi_card("Åpne posisjoner",
                       f"{s.open_positions} posisjoner",
                       "blue",
                       subtitle=f"${s.total_exposure_usd:,.0f} eksponert",
                       icon="shopping_cart")

            pnl_color = "green" if s.daily_pnl_usd >= 0 else "red"
            pnl_sign = "+" if s.daily_pnl_usd >= 0 else ""
            _kpi_card("Dagens P&L",
                       f"{pnl_sign}${s.daily_pnl_usd:,.2f}",
                       pnl_color,
                       subtitle="realisert + urealisert",
                       icon="today")

            _kpi_card("Win Rate (30d)",
                       f"{s.win_rate_30d:.0%}",
                       "purple",
                       subtitle=f"{int(s.win_rate_30d * 100)}% treff",
                       icon="military_tech")

            _kpi_card("Sharpe (90d)",
                       f"{s.sharpe_90d:.1f}",
                       "orange",
                       subtitle="mot S&P 0.8",
                       icon="trending_up")

            ecmwf_age = f"{s.ecmwf_age_hours:.0f}t"
            gfs_age = f"{s.gfs_age_hours:.0f}t"
            _kpi_card("Modell-alder",
                       f"ECMWF: {ecmwf_age}",
                       "yellow",
                       subtitle=f"GFS: {gfs_age} siden",
                       icon="schedule")

    # =========================================================================
    # Section B: City Cards Grid
    # =========================================================================

    def _build_city_grid(self) -> None:
        """Build the city cards in a responsive grid."""
        # Generate mock data if empty
        if not self._state.city_buckets:
            self._state.city_buckets = _generate_mock_city_data()

        # Filter cities
        active_cities = list(self._state.city_buckets.keys())
        if self.cities_filter:
            active_cities = [c for c in active_cities
                             if any(f.lower() in c.lower() for f in self.cities_filter)]

        # Grid container
        with ui.row().classes("w-full flex-wrap gap-3 mt-2") as grid:
            for city in active_cities[:12]:  # Max 12 cards shown
                with ui.column().classes("w-72") as card_container:
                    self._city_card_containers[city] = card_container
                    self._build_single_city_card(city, self._state.city_buckets[city])

    def _build_single_city_card(self, city: str, buckets: list[dict[str, Any]]) -> None:
        """Build a single city card with thermometer, bucket table, and ensemble mini."""
        city_info = CITY_DATA.get(city, {"flag": "🌐", "icao": "????", "region": "Ukjent"})
        flag = city_info["flag"]
        icao = city_info["icao"]
        display_name = city.title()

        # Determine temperature from first bucket
        import random
        random.seed(hash(city) % 2**31)
        mean_f = 75 + random.uniform(-15, 25)
        metar_age = random.randint(1, 45)

        with _dark_card():
            # --- Header ---
            with ui.row().classes("items-center w-full"):
                ui.label(f"{flag} {display_name}").style(
                    "font-size: 0.85rem; font-weight: 700; color: #e2e8f0;"
                )
                ui.space()
                ui.badge(icao).style(
                    "background: rgba(59,130,246,0.15); color: #60a5fa; "
                    "font-size: 0.65rem; padding: 2px 8px; border-radius: 6px;"
                )

            # --- Temperature row ---
            with ui.row().classes("items-center gap-3 mt-2"):
                # Big temperature
                ui.label(f"{mean_f:.1f}°F").style(
                    "font-size: 1.8rem; font-weight: 800; color: #f1f5f9;"
                )
                with ui.column().classes("gap-0"):
                    ui.label(f"METAR · {metar_age} min siden").style(
                        "color: #64748b; font-size: 0.62rem;"
                    )
                    # Mini ensemble dots
                    with ui.row().classes("gap-1 mt-0.5"):
                        models_sample = ["ecmwf_ifs", "gfs", "icon", "gem", "ukmo", "hrrr"]
                        for m in models_sample:
                            color = MODEL_COLORS.get(m, "#888")
                            ui.html(
                                f'<span style="display:inline-block;width:6px;height:6px;'
                                f'border-radius:50%;background:{color};margin:0 1px;" '
                                f'title="{MODEL_DISPLAY_NAMES.get(m,m)}"></span>'
                            )
                        ui.label("8 modeller").style("color: #475569; font-size: 0.58rem; margin-left: 4px;")

            # --- Bucket mini-table ---
            with ui.card().style(
                "background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.04); "
                "border-radius: 8px; padding: 8px; margin-top: 8px;"
            ):
                # Table header
                with ui.row().classes("w-full items-center text-xs").style("border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 4px;"):
                    ui.label("Bucket").style("color: #64748b; width: 55px; font-size: 0.6rem;")
                    ui.label("Marked").style("color: #64748b; width: 45px; font-size: 0.6rem; text-align: right;")
                    ui.label("Modell").style("color: #64748b; width: 45px; font-size: 0.6rem; text-align: right;")
                    ui.label("Edge").style("color: #64748b; width: 50px; font-size: 0.6rem; text-align: right;")
                    ui.label("Pos").style("color: #64748b; width: 70px; font-size: 0.6rem; text-align: right;")

                for b in buckets[:6]:
                    edge_color = b.get("edge_color", "red")
                    edge_pct = b.get("edge_pct", 0)
                    edge_sign = "+" if edge_pct >= 0 else ""
                    pos = b.get("position") or "—"
                    pos_color = "#22c55e" if pos != "—" else "#475569"

                    with ui.row().classes("w-full items-center text-xs mt-0.5"):
                        ui.label(b["label"]).style(
                            "color: #94a3b8; width: 55px; font-size: 0.6rem; font-family: monospace;"
                        )
                        ui.label(f"{b['market_pct']}%").style(
                            "color: #fca5a5; width: 45px; font-size: 0.6rem; text-align: right;"
                        )
                        ui.label(f"{b['model_pct']}%").style(
                            "color: #93c5fd; width: 45px; font-size: 0.6rem; text-align: right;"
                        )
                        edge_display = f"{edge_sign}{edge_pct}%"
                        edge_emoji = "🟢" if edge_pct > 6 else ("🟡" if edge_pct > 2 else "⚪")
                        ui.label(f"{edge_display} {edge_emoji}").style(
                            f"color: {'#22c55e' if edge_pct > 6 else ('#eab308' if edge_pct > 2 else '#64748b')}; "
                            "width: 50px; font-size: 0.6rem; text-align: right;"
                        )
                        ui.label(pos).style(
                            f"color: {pos_color}; width: 70px; font-size: 0.6rem; text-align: right; font-family: monospace;"
                        )

            # --- Action buttons ---
            with ui.row().classes("w-full gap-2 mt-2"):
                ui.button("📊 Detaljer", on_click=lambda c=city: self._toggle_city_detail(c)).props(
                    "flat size=sm dense"
                ).style("color: #60a5fa; font-size: 0.7rem;")
                ui.button("⏸ Pause by", on_click=lambda c=city: self._pause_city(c)).props(
                    "flat size=sm dense"
                ).style("color: #94a3b8; font-size: 0.7rem;")

    def _toggle_city_detail(self, city: str) -> None:
        """Toggle expanded detail panel for a city."""
        # For now, show a notification — full expandable panel would require
        # a more complex reactive approach with ui.dialog
        buckets = self._state.city_buckets.get(city, [])
        if not buckets:
            ui.notify(f"Ingen data for {city}", type="warning")
            return

        with ui.dialog() as dialog, ui.card().style(
            "background: rgba(15,23,42,0.98); border: 1px solid rgba(59,130,246,0.2); "
            "border-radius: 16px; padding: 20px; max-width: 900px; width: 85vw;"
        ):
            city_info = CITY_DATA.get(city, {"flag": "🌐", "icao": "????", "region": "Ukjent"})
            ui.label(f"{city_info['flag']} {city.title()} · {city_info['icao']}").style(
                "font-size: 1.1rem; font-weight: 700; color: #e2e8f0;"
            )

            with ui.tabs() as detail_tabs:
                tab_ens = ui.tab("📈 Ensemble")
                tab_bucket = ui.tab("📊 Bucket Edge")
                tab_cal = ui.tab("🔥 Kalibrering")
                tab_book = ui.tab("📖 Ordrebok")

            with ui.tab_panels(detail_tabs, value=tab_ens).classes("w-full mt-2"):
                with ui.tab_panel(tab_ens):
                    import random
                    random.seed(hash(city + "detail") % 2**31)
                    mean_f = 75 + random.uniform(-15, 25)
                    std_f = 3.0 + random.uniform(0, 3)
                    chart = _create_ensemble_distribution_chart(city, mean_f, std_f)
                    ui.plotly(chart).classes("w-full")

                with ui.tab_panel(tab_bucket):
                    chart = _create_bucket_edge_chart(buckets)
                    ui.plotly(chart).classes("w-full")

                with ui.tab_panel(tab_cal):
                    chart = _create_calibration_heatmap(buckets)
                    ui.plotly(chart).classes("w-full")

                with ui.tab_panel(tab_book):
                    self._build_order_book_panel(buckets)

            ui.button("Lukk", on_click=dialog.close).props("flat").style("color: #94a3b8;")

        dialog.open()

    def _build_order_book_panel(self, buckets: list[dict[str, Any]]) -> None:
        """Build a simple order book panel."""
        import random
        random.seed(999)

        with ui.card().style(
            "background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.04); "
            "border-radius: 8px; padding: 12px;"
        ):
            ui.label("Live Ordrebok (simulert)").style("color: #94a3b8; font-size: 0.75rem; margin-bottom: 6px;")
            with ui.row().classes("w-full text-xs").style("border-bottom: 1px solid rgba(255,255,255,0.05); padding-bottom: 4px;"):
                ui.label("Pris").style("color: #64748b; width: 60px;")
                ui.label("Størrelse").style("color: #64748b; width: 70px;")
                ui.label("Side").style("color: #64748b; width: 50px;")

            for b in buckets[:4]:
                price = b["market_pct"] / 100
                ask_size = random.randint(100, 5000)
                bid_size = random.randint(100, 5000)
                with ui.row().classes("w-full text-xs mt-0.5"):
                    ui.label(f"${price:.4f}").style("color: #e2e8f0; width: 60px; font-family: monospace;")
                    ui.label(f"{ask_size:,}").style("color: #fca5a5; width: 70px; font-family: monospace;")
                    ui.badge("SELL").style("background: rgba(239,68,68,0.15); color: #ef4444; font-size: 0.55rem;")
                with ui.row().classes("w-full text-xs"):
                    ui.label(f"${price:.4f}").style("color: #e2e8f0; width: 60px; font-family: monospace;")
                    ui.label(f"{bid_size:,}").style("color: #86efac; width: 70px; font-family: monospace;")
                    ui.badge("BUY").style("background: rgba(34,197,94,0.15); color: #22c55e; font-size: 0.55rem;")

    def _pause_city(self, city: str) -> None:
        """Pause/resume trading for a city."""
        ui.notify(f"⏸ {city.title()}: trading satt på pause", type="warning")

    # =========================================================================
    # Section D: Portfolio Overview
    # =========================================================================

    def _build_portfolio_section(self) -> None:
        """Build portfolio overview with positions, exposure, risk, and capital."""
        s = self._state
        if not s.positions:
            s.positions = _generate_mock_positions()

        with ui.row().classes("w-full gap-3 flex-wrap"):
            # --- Positions Table (left) ---
            with ui.column().classes("flex-1").style("min-width: 550px;"):
                with _dark_card():
                    ui.label("📌 Åpne Posisjoner").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    columns = [
                        {"name": "market", "label": "Marked", "field": "market", "align": "left"},
                        {"name": "bucket", "label": "Bucket", "field": "bucket", "align": "left"},
                        {"name": "side", "label": "Side", "field": "side", "align": "center"},
                        {"name": "size", "label": "Str.", "field": "size", "align": "right"},
                        {"name": "entry", "label": "Entry", "field": "entry", "align": "right"},
                        {"name": "current", "label": "Nå", "field": "current", "align": "right"},
                        {"name": "pnl", "label": "P&L", "field": "pnl", "align": "right"},
                        {"name": "time_left", "label": "Tid", "field": "time_left", "align": "right"},
                    ]
                    rows = [
                        {
                            "market": p["market"][:30],
                            "bucket": p["bucket"],
                            "side": p["side"],
                            "size": p["size"],
                            "entry": p["entry"],
                            "current": p["current"],
                            "pnl": p["pnl"],
                            "time_left": p["time_left"],
                        }
                        for p in s.positions
                    ]
                    self._positions_table = ui.table(
                        columns=columns, rows=rows,
                    ).classes("w-full").style("font-size: 0.7rem;")

            # --- Risk + Capital (right) ---
            with ui.column().classes("w-80"):
                with _dark_card():
                    ui.label("🛡️ Risikometrikker").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    metrics = [
                        ("VaR (95%)", f"${s.var_95:,.0f}", "orange"),
                        ("Expected Shortfall", f"${s.expected_shortfall:,.0f}", "red"),
                        ("Max Drawdown", f"{s.max_drawdown:.1%}", "red" if s.max_drawdown > 0.1 else "yellow"),
                    ]
                    for label, value, color in metrics:
                        with ui.row().classes("w-full items-center justify-between mt-1"):
                            ui.label(label).style("color: #94a3b8; font-size: 0.72rem;")
                            ui.label(value).style(
                                f"color: {'#ef4444' if color == 'red' else ('#eab308' if color == 'yellow' else '#f97316')}; "
                                "font-weight: 600; font-size: 0.75rem;"
                            )

                with _dark_card():
                    ui.label("💰 Kapital").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    cap_items = [
                        ("Total", f"${s.total_bankroll:,.0f}", "#e2e8f0"),
                        ("Allokert", f"${s.allocated_usd:,.0f}", "#60a5fa"),
                        ("Tilgjengelig", f"${s.available_usd:,.0f}", "#22c55e"),
                        ("I ordrer", f"${s.in_orders_usd:,.0f}", "#eab308"),
                    ]
                    for label, value, color in cap_items:
                        with ui.row().classes("w-full items-center justify-between mt-1"):
                            ui.label(label).style("color: #94a3b8; font-size: 0.72rem;")
                            ui.label(value).style(f"color: {color}; font-weight: 600; font-size: 0.75rem;")

                    # Simple bar: allocated vs available
                    total = max(s.total_bankroll, 1)
                    alloc_pct = s.allocated_usd / total * 100
                    avail_pct = s.available_usd / total * 100
                    orders_pct = s.in_orders_usd / total * 100

                    with ui.row().classes("w-full mt-2").style("height: 6px; border-radius: 3px; overflow: hidden;"):
                        ui.html(
                            f'<div style="height:6px;background:#3b82f6;width:{alloc_pct:.0f}%;display:inline-block;"></div>'
                            f'<div style="height:6px;background:#eab308;width:{orders_pct:.0f}%;display:inline-block;"></div>'
                            f'<div style="height:6px;background:rgba(255,255,255,0.08);width:{avail_pct:.0f}%;display:inline-block;"></div>'
                        )

                # Daily P&L Chart
                with _dark_card():
                    chart = _create_daily_pnl_chart(s.daily_pnl_history)
                    ui.plotly(chart).classes("w-full")

    # =========================================================================
    # Section E: System Status
    # =========================================================================

    def _build_system_status(self) -> None:
        """Build system status section."""
        s = self._state

        with ui.row().classes("w-full gap-3 flex-wrap mt-2"):
            # --- Data Source Status Grid ---
            with ui.column().classes("flex-1").style("min-width: 400px;"):
                with _dark_card():
                    ui.label("🔌 Datakilde-status").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    with ui.row().classes("w-full flex-wrap gap-2"):
                        for source, status in s.data_source_status.items():
                            dot = _status_dot(status)
                            color_map = {"green": "#22c55e", "yellow": "#eab308", "red": "#ef4444"}
                            fg = color_map.get(status, "#64748b")
                            with ui.card().style(
                                f"background: rgba(255,255,255,0.02); border: 1px solid rgba(255,255,255,0.04); "
                                "border-radius: 8px; padding: 6px 10px;"
                            ):
                                ui.html(f'{dot} <span style="color:{fg};font-size:0.7rem;">{source}</span>')

            # --- Model ages + API limits ---
            with ui.column().classes("w-72"):
                with _dark_card():
                    ui.label("🕐 Modell-alder").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    for model, age in s.model_ages.items():
                        with ui.row().classes("w-full items-center justify-between mt-1"):
                            ui.label(model).style("color: #94a3b8; font-size: 0.72rem;")
                            ui.label(age).style("color: #e2e8f0; font-size: 0.72rem; font-family: monospace;")

                with _dark_card():
                    ui.label("📊 API Rate Limits").style(
                        "font-size: 0.85rem; font-weight: 700; color: #e2e8f0; margin-bottom: 8px;"
                    )
                    for api, limit in s.api_limits.items():
                        with ui.row().classes("w-full items-center justify-between mt-1"):
                            ui.label(api).style("color: #94a3b8; font-size: 0.72rem;")
                            ui.label(limit).style("color: #e2e8f0; font-size: 0.72rem; font-family: monospace;")

        # --- Log stream ---
        with _dark_card().classes("w-full mt-3"):
            with ui.row().classes("w-full items-center justify-between mb-2"):
                ui.label("📋 Sanntids-logg").style(
                    "font-size: 0.85rem; font-weight: 700; color: #e2e8f0;"
                )
                with ui.row().classes("gap-2"):
                    filt = ui.select(
                        options=["ALL", "INFO", "WARNING", "ERROR"],
                        value="ALL",
                    ).classes("w-24").style("font-size: 0.65rem;")
                    ui.button("🗑️ Tøm", on_click=lambda: self._clear_logs()).props(
                        "flat size=sm dense"
                    ).style("color: #64748b; font-size: 0.65rem;")

            self._log_container = ui.column().classes("w-full").style(
                "max-height: 200px; overflow-y: auto; "
                "background: rgba(0,0,0,0.3); border: 1px solid rgba(255,255,255,0.05); "
                "border-radius: 8px; padding: 8px;"
            )
            self._populate_logs()

    def _populate_logs(self) -> None:
        """Fill log container with current entries."""
        if self._log_container is None:
            return

        # Add some initial logs if empty
        if not self._state.log_entries:
            now = datetime.now(timezone.utc)
            sample_logs = [
                ("INFO", "Dashboard startet — laster by-data..."),
                ("INFO", "BMA ensemble initialisert: 8 modeller, 30 byer"),
                ("INFO", "METAR feed aktiv: 12 stasjoner oppdatert"),
                ("INFO", "ECMWF 00Z run lastet: 51 medlemmer"),
                ("WARNING", "Satellite cloud cover: GOES-16 data 45 min gammel"),
                ("INFO", "CLOB tilkoblet: 8 markeder overvåkes"),
                ("ERROR", "Redis utilgjengelig — kjører uten cache"),
                ("INFO", "Scan fullført: 12 byer, 0 nye markeder funnet"),
                ("INFO", "Signal: NYC >86°F edge +17% — Kelly size $120"),
                ("INFO", "Ordre plassert: YES NYC >86°F @ $0.38 — $120"),
            ]
            self._state.log_entries = [(lvl, msg) for lvl, msg in sample_logs]

        self._log_container.clear()
        with self._log_container:
            for level, message in self._state.log_entries[-20:]:
                color_map = {
                    "INFO": "#94a3b8",
                    "WARNING": "#fbbf24",
                    "ERROR": "#f87171",
                    "CRITICAL": "#ef4444",
                }
                color = color_map.get(level, "#94a3b8")
                timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
                ui.html(
                    f'<div style="font-family:monospace;font-size:0.65rem;padding:1px 0;color:{color};">'
                    f'<span style="color:#64748b;">[{timestamp}]</span> '
                    f'<span style="font-weight:600;">[{level}]</span> '
                    f'{message}</div>'
                )

    def _clear_logs(self) -> None:
        """Clear log entries."""
        self._state.log_entries.clear()
        self._populate_logs()

    # =========================================================================
    # Refresh
    # =========================================================================

    async def _auto_refresh(self) -> None:
        """Periodic auto-refresh callback — updates state and re-renders data."""
        import random
        # Simulate small changes
        s = self._state
        s.daily_pnl_usd += random.uniform(-5, 10)
        s.daily_pnl_usd = round(s.daily_pnl_usd, 2)
        s.ecmwf_age_hours += REFRESH_INTERVAL_SEC / 3600
        s.gfs_age_hours += REFRESH_INTERVAL_SEC / 3600

        # Refresh positions
        for p in s.positions:
            current_val = float(p["entry"].replace("$", "")) + random.uniform(-0.03, 0.04)
            p["current"] = f"${current_val:.2f}"
            pnl = (current_val - float(p["entry"].replace("$", ""))) * float(p["size"].replace("$", ""))
            p["pnl"] = f"${pnl:+,.2f}"

        # Update positions table
        if self._positions_table is not None:
            rows = [
                {
                    "market": p["market"][:30],
                    "bucket": p["bucket"],
                    "side": p["side"],
                    "size": p["size"],
                    "entry": p["entry"],
                    "current": p["current"],
                    "pnl": p["pnl"],
                    "time_left": p["time_left"],
                }
                for p in s.positions
            ]
            self._positions_table.update_rows(rows)

        # Add a periodic log
        if random.random() < 0.3:
            s.log_entries.append(("INFO", f"Auto-refresh: {s.active_cities} byer aktive, "
                                          f"{s.open_positions} posisjoner"))
            self._populate_logs()

    async def _manual_refresh(self) -> None:
        """Manual refresh triggered by button."""
        ui.notify("🔄 Oppdaterer dashboard...", type="info")
        await self._auto_refresh()
        ui.notify("✅ Dashboard oppdatert!", type="positive")


# =============================================================================
# Standalone Entry Point
# =============================================================================


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Polymarket Weather Trading Dashboard",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=True,
        help="Kjør i dry-run modus (standard: på)",
    )
    parser.add_argument(
        "--live", action="store_true", default=False,
        help="Kjør i LIVE modus (overstyrer --dry-run)",
    )
    parser.add_argument(
        "--cities", type=str, default="",
        help="Komma-separert liste over byer å vise (f.eks. 'NYC,London,Tokyo')",
    )
    parser.add_argument(
        "--capital", type=float, default=5000.0,
        help="Total bankroll i USDC (standard: 5000)",
    )
    parser.add_argument(
        "--no-dashboard", action="store_true", default=False,
        help="Headless modus — ikke start GUI (for CLI-bruk)",
    )
    parser.add_argument(
        "--port", type=int, default=APP_PORT,
        help=f"Port for dashboard (standard: {APP_PORT})",
    )
    parser.add_argument(
        "--host", type=str, default=APP_HOST,
        help=f"Host for dashboard (standard: {APP_HOST})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Entry point: python -m src.strategies.weather.dashboard"""
    args = parse_args(argv)

    dry_run = not args.live
    cities_filter = [c.strip() for c in args.cities.split(",") if c.strip()] if args.cities else []

    if args.no_dashboard:
        print("🌤️ Vær-Trading Bot v2.0 — Headless modus")
        print(f"   Dry-run: {dry_run}")
        print(f"   Kapital: ${args.capital:,.0f}")
        if cities_filter:
            print(f"   Byer: {', '.join(cities_filter)}")
        print("   Dashboard deaktivert. Bruk uten --no-dashboard for GUI.")
        # In headless mode, we'd start the monitor/strategy directly
        return

    dashboard = WeatherDashboard(
        dry_run=dry_run,
        cities_filter=cities_filter,
        capital=args.capital,
        headless=False,
    )

    @app.on_startup
    def on_startup() -> None:
        print(f"🌤️ Vær-Trading Dashboard startet — http://{args.host}:{args.port}")

    dashboard.build()

    ui.run(
        host=args.host,
        port=args.port,
        title=APP_TITLE,
        dark=True,
        favicon="🌤️",
        show=True,
        reload=False,
    )


if __name__ == "__main__":
    main()
