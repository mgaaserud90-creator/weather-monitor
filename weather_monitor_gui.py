#!/usr/bin/env python3
"""
Weather Monitor GUI — tkinter-based graphical interface for the
weather market monitor. Wraps all CLI functionality into a user-friendly GUI.

Reuses backend logic from weather_monitor_cli.py:
  - LocationManager (JSON persistence for saved locations)
  - WeatherAnalyzer (BMA Multi-Model Ensemble)
  - geocode_city (Open-Meteo geocoding)

Usage:
    cd polymarket-arb-bot
    python weather_monitor_gui.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re as _re_mod
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
from tkinter import (
    Tk,
    Toplevel,
    Frame,
    Label,
    Button,
    Entry,
    Listbox,
    StringVar,
    IntVar,
    BooleanVar,
    DoubleVar,
    messagebox,
    VERTICAL,
    HORIZONTAL,
    END,
    N,
    S,
    E,
    W,
    YES,
    BOTH,
    LEFT,
    RIGHT,
    TOP,
    BOTTOM,
    X,
    Y,
)
from tkinter import ttk
import tkinter as tk
from typing import Any

# ---------------------------------------------------------------------------
# Ensure the polymarket-arb-bot package root is on sys.path
# ---------------------------------------------------------------------------
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)


# ---------------------------------------------------------------------------
# Import backend classes from the CLI module (same directory)
# ---------------------------------------------------------------------------
from weather_monitor_cli import (  # noqa: E402  # isort:skip
    AnalysisResult,
    BucketComparison,
    LocationManager,
    PeakState,
    SavedLocation,
    WeatherAnalyzer,
    compute_live_confidence,
    detect_peak_state,
    geocode_city,
    LOCATIONS_FILE,
)


# =============================================================================
# Async Runner — runs asyncio coroutines in a background daemon thread
# =============================================================================


class AsyncRunner:
    """Manages a dedicated asyncio event loop in a daemon thread.

    Use ``submit(coro, callback)`` to schedule a coroutine.  The *callback*
    receives a single argument: the result of the coroutine, or the exception
    that was raised.
    """

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    # -----------------------------------------------------------------------
    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # -----------------------------------------------------------------------
    def submit(self, coro: Any, callback: Any) -> None:
        """Schedule *coro* on the background loop; call *callback* when done."""

        async def _wrapped() -> Any:
            return await coro

        future = asyncio.run_coroutine_threadsafe(_wrapped(), self._loop)

        def _poll() -> None:
            if future.done():
                try:
                    result = future.result()
                except Exception as exc:
                    result = exc
                callback(result)
            else:
                # Not done yet — check again in 100 ms
                self._loop.call_soon_threadsafe(lambda: None)  # no-op wakeup
                # We need a tkinter root reference; stored on the class later
                if AsyncRunner._tk_root is not None:
                    AsyncRunner._tk_root.after(100, _poll)

        if AsyncRunner._tk_root is not None:
            AsyncRunner._tk_root.after(100, _poll)
        else:
            # Fallback: block-wait (should not happen once GUI is up)
            callback(future.result())

    # -----------------------------------------------------------------------
    def shutdown(self) -> None:
        """Stop the background event loop."""
        self._loop.call_soon_threadsafe(self._loop.stop)

    # Class-level reference to the Tk root for `after` scheduling.
    _tk_root: Tk | None = None


# =============================================================================
# GUI Application
# =============================================================================


class WeatherMonitorGUI:
    """Main GUI window for the Weather Monitor."""

    # -------------------------------------------------------------------
    def __init__(self, root: Tk) -> None:
        self.root = root
        self.root.title("🌤️ VærMonitor — BMA Ensemble Analyse")
        self.root.geometry("1100x750")
        self.root.minsize(900, 600)

        # Center on screen
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        x = (sw - 1100) // 2
        y = (sh - 750) // 2
        self.root.geometry(f"+{x}+{y}")

        # Let AsyncRunner know our root for `after` scheduling
        AsyncRunner._tk_root = self.root

        # Backend state (mirrors WeatherMonitorCLI)
        self._loc_mgr = LocationManager()
        self._analyzer = WeatherAnalyzer()
        self._geocode_city = geocode_city

        # Runtime state
        self._last_analyses: dict[int, Any] = {}  # dict[int, AnalysisResult]
        self._initialized = False

        # Monitoring state
        self._monitored_cities: list[dict[str, Any]] = []
        self._monitoring_active: bool = False
        self._monitoring_thread: threading.Thread | None = None
        self._confidence_history: dict[str, list[float]] = {}  # city -> last 10 confidences
        self._peak_max: dict[str, float] = {}  # city -> all-time max confidence
        self._suggested_temps: dict[str, float] = {}  # city -> suggested bet temp from BMA analysis
        self._momentant_over_alerted: dict[str, bool] = {}  # city -> already alerted "momentant over"
        self._current_conditions: dict[str, dict[str, Any]] = {}  # city -> current humidity, wind, cloud
        self._api_daily_max: dict[str, dict[str, Any] | None] = {}  # city -> {"max_c": ..., "peak_time": ...} from Open-Meteo

        # Real-time observation tracking (new peak detection system)
        self._obs_history: dict[str, list[tuple[datetime, float]]] = {}  # city -> [(ts, temp_c), ...]
        self._today_max: dict[str, tuple[float, datetime] | None] = {}  # city -> (max_temp_c, time)
        self._peak_state: dict[str, PeakState] = {}  # city -> current peak state
        self._peak_confirmed: dict[str, tuple[float, datetime]] = {}  # city -> (confirmed_temp, time)
        self._last_temp_fetch: dict[str, float] = {}  # city -> last fetch timestamp (epoch)
        self._last_bma_result: dict[str, dict[str, Any]] = {}  # city -> last BMA result dict
        self._last_alert_level: dict[str, str] = {}  # city -> last alert level sent (avoid duplicates)

        # Date selection state (lead_days: 0=today, 1=tomorrow, ...)
        self._analysis_lead_days: int = 1  # default: tomorrow
        self._compare_lead_days: int = 1
        self._date_options: list[str] = []
        self._date_lead_map: dict[str, int] = {}

        # Async runner
        self._async = AsyncRunner()

        # Status tracking
        self._status_openmeteo = "⏳"

        # Populate date options for dropdowns (MUST be before _build_ui!)
        self._populate_date_options()

        # Build the UI
        self._build_ui()

        # Start background initialization
        self._async.submit(self._initialize_backend(), self._on_init_done)

        # Clean shutdown
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ===================================================================
    # Backend Initialization
    # ===================================================================

    async def _initialize_backend(self) -> dict[str, str]:
        """Initialize the weather analyzer backend. Returns status dict."""
        status: dict[str, str] = {}
        try:
            await self._analyzer.initialize()
            status["openmeteo"] = "✅"
        except Exception as exc:
            status["openmeteo"] = f"❌ {exc}"

        return status

    def _on_init_done(self, result: Any) -> None:
        """Callback after backend initialization completes."""
        if isinstance(result, Exception):
            messagebox.showerror("Initialiseringsfeil", str(result))
            return

        self._initialized = True
        status = result if isinstance(result, dict) else {}
        self._status_openmeteo = status.get("openmeteo", "❌")
        self._update_status_bar()
        self._refresh_location_list()

    # ===================================================================
    # UI Construction
    # ===================================================================

    def _build_ui(self) -> None:
        """Build the complete GUI layout."""
        # --- Style ---
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except Exception:
            pass
        style.configure("Header.TLabel", font=("Segoe UI", 14, "bold"))
        style.configure("Status.TLabel", font=("Segoe UI", 9))
        style.configure("Green.TLabel", foreground="#2e7d32")
        style.configure("Red.TLabel", foreground="#c62828")

        # --- Main container ---
        main_frame = ttk.Frame(self.root)
        main_frame.pack(fill=BOTH, expand=YES)

        # --- Notebook (tabs) ---
        self._notebook = ttk.Notebook(main_frame)
        self._notebook.pack(fill=BOTH, expand=YES, padx=5, pady=(5, 0))

        # Build each tab
        self._build_locations_tab()
        self._build_cities_tab()
        self._build_analysis_tab()
        self._build_monitoring_tab()

        # --- Status Bar ---
        self._status_frame = ttk.Frame(main_frame, relief="sunken", borderwidth=1)
        self._status_frame.pack(fill=X, side=BOTTOM, padx=5, pady=5)

        self._status_label = ttk.Label(
            self._status_frame,
            text="Initialiserer...",
            style="Status.TLabel",
            padding=(8, 4),
        )
        self._status_label.pack(side=LEFT)

        self._status_openmeteo_label = ttk.Label(
            self._status_frame, text="Open-Meteo: ⏳", style="Status.TLabel", padding=(8, 4)
        )
        self._status_openmeteo_label.pack(side=LEFT)

        self._status_time_label = ttk.Label(
            self._status_frame,
            text="",
            style="Status.TLabel",
            padding=(8, 4),
        )
        self._status_time_label.pack(side=RIGHT)

    # ===================================================================
    # Tab 1: Locations (Lokasjoner)
    # ===================================================================

    def _build_locations_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(tab, text="📍 Lokasjoner")

        # --- Left panel: listbox + buttons ---
        left = ttk.Frame(tab)
        left.pack(side=LEFT, fill=BOTH, expand=YES, padx=(0, 5))

        ttk.Label(left, text="Lagrede lokasjoner:", style="Header.TLabel").pack(anchor=W)

        list_frame = ttk.Frame(left)
        list_frame.pack(fill=BOTH, expand=YES, pady=5)

        self._loc_listbox = Listbox(
            list_frame,
            height=15,
            selectmode="single",
            font=("Consolas", 10),
            activestyle="none",
        )
        scrollbar = ttk.Scrollbar(list_frame, orient=VERTICAL, command=self._loc_listbox.yview)
        self._loc_listbox.configure(yscrollcommand=scrollbar.set)
        self._loc_listbox.pack(side=LEFT, fill=BOTH, expand=YES)
        scrollbar.pack(side=RIGHT, fill=Y)

        self._loc_count_label = ttk.Label(left, text="0/100 lokasjoner")
        self._loc_count_label.pack(anchor=W, pady=(0, 5))

        btn_frame = ttk.Frame(left)
        btn_frame.pack(fill=X)
        ttk.Button(btn_frame, text="🗑️ Fjern valgt", command=self._remove_location).pack(
            side=LEFT, padx=(0, 5)
        )
        ttk.Button(btn_frame, text="❌ Tøm alle", command=self._clear_locations).pack(side=LEFT, padx=(0, 5))
        ttk.Button(btn_frame, text="🔄 Standard", command=self._reset_to_defaults).pack(side=LEFT)

        # --- Right panel: add controls ---
        right = ttk.Frame(tab)
        right.pack(side=RIGHT, fill=Y, padx=(5, 0))

        # -- Add by city name --
        city_frame = ttk.LabelFrame(right, text="Legg til by (geokoding)", padding=10)
        city_frame.pack(fill=X, pady=(0, 10))

        ttk.Label(city_frame, text="By:").pack(anchor=W)
        self._city_entry = ttk.Entry(city_frame, width=30)
        self._city_entry.pack(fill=X, pady=(2, 5))
        self._city_entry.bind("<Return>", lambda e: self._add_by_city())
        ttk.Button(city_frame, text="🔍 Legg til by", command=self._add_by_city).pack(fill=X)

        # -- Add by coordinates --
        coord_frame = ttk.LabelFrame(right, text="Legg til koordinater", padding=10)
        coord_frame.pack(fill=X)

        ttk.Label(coord_frame, text="Navn:").pack(anchor=W)
        self._coord_name_entry = ttk.Entry(coord_frame, width=30)
        self._coord_name_entry.pack(fill=X, pady=(2, 5))

        row = ttk.Frame(coord_frame)
        row.pack(fill=X)
        ttk.Label(row, text="Lat:").pack(side=LEFT)
        self._lat_entry = ttk.Entry(row, width=12)
        self._lat_entry.pack(side=LEFT, padx=(2, 8))
        ttk.Label(row, text="Lon:").pack(side=LEFT)
        self._lon_entry = ttk.Entry(row, width=12)
        self._lon_entry.pack(side=LEFT, padx=(2, 0))

        ttk.Button(
            coord_frame,
            text="📍 Legg til koordinater",
            command=self._add_by_coords,
        ).pack(fill=X, pady=(8, 0))

    # ===================================================================
    # Tab 2: City Overview (Byer)
    # ===================================================================

    def _build_cities_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(tab, text="🌍 Byer")

        # Control row
        ctrl = ttk.Frame(tab)
        ctrl.pack(fill=X, pady=(0, 10))

        ttk.Label(ctrl, text="Alle tilgjengelige byer for analyse og overvåkning",
                  font=("Segoe UI", 10)).pack(side=LEFT)

        # City list table
        table_frame = ttk.Frame(tab)
        table_frame.pack(fill=BOTH, expand=YES)

        columns = ("name", "tz", "coords")
        self._cities_tree = ttk.Treeview(
            table_frame, columns=columns, show="headings", selectmode="browse", height=20
        )
        self._cities_tree.heading("name", text="By")
        self._cities_tree.heading("tz", text="Tidssone")
        self._cities_tree.heading("coords", text="Koordinater")

        self._cities_tree.column("name", width=250, minwidth=150)
        self._cities_tree.column("tz", width=200, minwidth=100)
        self._cities_tree.column("coords", width=200, minwidth=100)

        vsb = ttk.Scrollbar(table_frame, orient=VERTICAL, command=self._cities_tree.yview)
        self._cities_tree.configure(yscrollcommand=vsb.set)
        self._cities_tree.pack(side=LEFT, fill=BOTH, expand=YES)
        vsb.pack(side=RIGHT, fill=Y)

        # Summary label
        self._cities_summary = ttk.Label(tab, text="", padding=(0, 5))
        self._cities_summary.pack(anchor=W)

    # ===================================================================
    # Tab 3: Analysis (Analyse)
    # ===================================================================

    def _build_analysis_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(tab, text="📊 Analyse")

        # --- Top controls ---
        ctrl = ttk.Frame(tab)
        ctrl.pack(fill=X, pady=(0, 5))

        ttk.Label(ctrl, text="Lokasjon:").pack(side=LEFT, padx=(0, 5))
        self._analysis_loc_var = StringVar()
        self._analysis_loc_combo = ttk.Combobox(
            ctrl, textvariable=self._analysis_loc_var, state="readonly", width=35
        )
        self._analysis_loc_combo.pack(side=LEFT, padx=(0, 10))
        self._analysis_loc_combo.bind("<<ComboboxSelected>>", self._on_analysis_loc_selected)

        self._analysis_btn = ttk.Button(
            ctrl, text="🚀 Kjør analyse", command=self._run_analysis
        )
        self._analysis_btn.pack(side=LEFT, padx=(0, 10))

        self._analysis_progress = ttk.Progressbar(ctrl, mode="indeterminate", length=200)
        self._analysis_progress.pack(side=LEFT)

        # --- Bulk analysis button row ---
        bulk_ctrl = ttk.Frame(tab)
        bulk_ctrl.pack(fill=X, pady=(0, 5))

        self._bulk_btn = ttk.Button(
            bulk_ctrl,
            text="🚀 Bulk Analyse (Topp 5 Konfidens)",
            command=self._run_bulk_analysis,
        )
        self._bulk_btn.pack(side=LEFT, padx=(0, 10))

        ttk.Label(bulk_ctrl, text="viser").pack(side=LEFT, padx=(0, 5))

        self._bulk_count_var = StringVar(value="5")
        self._bulk_count_spinbox = ttk.Spinbox(
            bulk_ctrl,
            from_=1,
            to=51,
            width=4,
            textvariable=self._bulk_count_var,
        )
        self._bulk_count_spinbox.pack(side=LEFT, padx=(0, 5))

        ttk.Label(bulk_ctrl, text="byer (1-51)").pack(side=LEFT, padx=(0, 10))

        self._bulk_progress = ttk.Progressbar(bulk_ctrl, mode="determinate", length=350)
        self._bulk_progress.pack(side=LEFT, padx=(0, 10))

        self._bulk_status = ttk.Label(bulk_ctrl, text="")
        self._bulk_status.pack(side=LEFT)

        # --- Date selector ---
        date_frame = ttk.Frame(tab)
        date_frame.pack(fill=X, pady=(0, 10))
        ttk.Label(date_frame, text="📅 Dato for analyse:").pack(side=LEFT, padx=(0, 5))
        self._analysis_date_var, self._analysis_date_combo = self._get_date_var_and_combo(
            date_frame, self._analysis_lead_days
        )
        self._analysis_date_combo.pack(side=LEFT)
        self._analysis_date_combo.bind(
            "<<ComboboxSelected>>",
            lambda e: self._status_label.configure(
                text="📅 Dato endret — trykk 'Kjør analyse' for å oppdatere"
            ),
        )
        ttk.Label(
            date_frame,
            text="  (åpner analyse for valgt dato)",
            foreground="#888",
            font=("Segoe UI", 9),
        ).pack(side=LEFT)

        # --- Results area (scrollable canvas) ---

        self._analysis_canvas_frame = ttk.Frame(tab)
        self._analysis_canvas_frame.pack(fill=BOTH, expand=YES)

        self._analysis_canvas = tk.Canvas(
            self._analysis_canvas_frame, highlightthickness=0, bg="#f5f5f5"
        )
        self._analysis_scrollbar = ttk.Scrollbar(
            self._analysis_canvas_frame, orient=VERTICAL, command=self._analysis_canvas.yview
        )
        self._analysis_text = ttk.Frame(self._analysis_canvas)

        self._analysis_text.bind(
            "<Configure>",
            lambda e: self._analysis_canvas.configure(
                scrollregion=self._analysis_canvas.bbox("all")
            ),
        )

        self._analysis_win = self._analysis_canvas.create_window(
            (0, 0), window=self._analysis_text, anchor="nw"
        )

        self._analysis_canvas.configure(yscrollcommand=self._analysis_scrollbar.set)

        self._analysis_canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        self._analysis_scrollbar.pack(side=RIGHT, fill=Y)

        def _resize_canvas(event: Any) -> None:
            self._analysis_canvas.itemconfig(self._analysis_win, width=event.width)

        self._analysis_canvas.bind("<Configure>", _resize_canvas)

        # Mouse-wheel scrolling (Windows)
        def _on_mwheel(event: Any) -> None:
            self._analysis_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._analysis_canvas.bind("<Enter>", lambda e: self._analysis_canvas.bind_all(
            "<MouseWheel>", _on_mwheel
        ))
        self._analysis_canvas.bind("<Leave>", lambda e: self._analysis_canvas.unbind_all(
            "<MouseWheel>"
        ))

    # ===================================================================
    # Tab 4: Monitoring (Overvåkning)
    # ===================================================================

    def _build_monitoring_tab(self) -> None:
        tab = ttk.Frame(self._notebook, padding=10)
        self._notebook.add(tab, text="🔔 Overvåkning")

        ctrl = ttk.Frame(tab)
        ctrl.pack(fill=X, pady=(0, 10))

        self._mon_start_btn = ttk.Button(
            ctrl, text="🔔 Overvåk alle 5",
            command=self._start_monitoring_top5,
        )
        self._mon_start_btn.pack(side=LEFT, padx=(0, 10))

        self._mon_stop_btn = ttk.Button(
            ctrl, text="⏹️ Stopp",
            command=self._stop_monitoring,
            state="disabled",
        )
        self._mon_stop_btn.pack(side=LEFT, padx=(0, 10))

        self._peak_curve_btn = ttk.Button(
            ctrl, text="📈 Peak Kurve",
            command=self._open_peak_curve_window,
        )
        self._peak_curve_btn.pack(side=LEFT, padx=(0, 10))

        self._mon_status_label = ttk.Label(ctrl, text="Inaktiv — kjør Bulk Analyse først")
        self._mon_status_label.pack(side=LEFT, padx=(10, 0))

        # Monitoring results area (scrollable)
        self._mon_canvas_frame = ttk.Frame(tab)
        self._mon_canvas_frame.pack(fill=BOTH, expand=YES)

        self._mon_canvas = tk.Canvas(
            self._mon_canvas_frame, highlightthickness=0, bg="#f5f5f5"
        )
        self._mon_scrollbar = ttk.Scrollbar(
            self._mon_canvas_frame, orient=VERTICAL, command=self._mon_canvas.yview
        )
        self._mon_text = ttk.Frame(self._mon_canvas)

        self._mon_text.bind(
            "<Configure>",
            lambda e: self._mon_canvas.configure(
                scrollregion=self._mon_canvas.bbox("all")
            ),
        )

        self._mon_win = self._mon_canvas.create_window(
            (0, 0), window=self._mon_text, anchor="nw"
        )

        self._mon_canvas.configure(yscrollcommand=self._mon_scrollbar.set)
        self._mon_canvas.pack(side=LEFT, fill=BOTH, expand=YES)
        self._mon_scrollbar.pack(side=RIGHT, fill=Y)

        def _resize_mon(event: Any) -> None:
            self._mon_canvas.itemconfig(self._mon_win, width=event.width)

        self._mon_canvas.bind("<Configure>", _resize_mon)

        def _on_mwheel_mon(event: Any) -> None:
            self._mon_canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        self._mon_canvas.bind("<Enter>", lambda e: self._mon_canvas.bind_all(
            "<MouseWheel>", _on_mwheel_mon
        ))
        self._mon_canvas.bind("<Leave>", lambda e: self._mon_canvas.unbind_all(
            "<MouseWheel>"
        ))

    # -------------------------------------------------------------------
    # Monitoring logic
    # -------------------------------------------------------------------

    def _start_monitoring_top5(self) -> None:
        """Start background monitoring of top 5 cities."""
        if not self._monitored_cities:
            messagebox.showwarning(
                "Ingen data",
                "Kjør 'Bulk Analyse' først for å finne topp 5 byer.",
            )
            return

        if self._monitoring_active:
            return

        self._monitoring_active = True
        self._mon_start_btn.configure(state="disabled")
        self._mon_stop_btn.configure(state="normal")
        self._mon_status_label.configure(text="⏳ Overvåker...")

        # Reset all history
        self._confidence_history = {}
        self._peak_max = {}
        self._peak_info: dict[str, dict[str, Any]] = {}  # city -> {temp, conf_was, conf_now, time}
        self._momentant_over_alerted = {}
        self._last_alert_level = {}

        # Reset new peak detection tracking
        self._obs_history = {}
        self._today_max = {}
        self._peak_state = {}
        self._peak_confirmed = {}
        self._last_temp_fetch = {}
        self._last_bma_result = {}
        self._current_conditions = {}
        self._api_daily_max = {}

        # Clear monitoring canvas
        for widget in self._mon_text.winfo_children():
            widget.destroy()

        # SMS alert state (dedup: one per city per day)
        self._sms_sent_today: set[str] = set()

        # Start background thread
        self._monitoring_thread = threading.Thread(
            target=self._monitoring_loop, daemon=True
        )
        self._monitoring_thread.start()

    def _stop_monitoring(self) -> None:
        """Stop background monitoring."""
        self._monitoring_active = False
        self._mon_start_btn.configure(state="normal")
        self._mon_stop_btn.configure(state="disabled")
        self._mon_status_label.configure(text="Stoppet")

    def _monitoring_loop(self) -> None:
        """Background loop: interleaved current-temp (5min/3min) + BMA ensemble (15min)."""
        import asyncio as _asyncio

        BMA_INTERVAL = 900  # 15 minutes
        TEMP_NORMAL_INTERVAL = 300  # 5 minutes
        TEMP_PEAK_INTERVAL = 180  # 3 minutes during peak window
        API_MAX_INTERVAL = 1800  # 30 minutes for API daily max
        CHECK_INTERVAL = 30  # check every 30 seconds

        last_bma_refresh = 0.0
        last_api_max_fetch = 0.0
        last_temp_fetch: dict[str, float] = {}  # per-city last temp fetch

        while self._monitoring_active:
            now_ts = time.time()

            # Determine lead_days from analysis date selection
            lead_days = getattr(self, "_analysis_lead_days", 1)

            # --- BMA refresh every 15 minutes ---
            if now_ts - last_bma_refresh >= BMA_INTERVAL:
                try:
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)

                    results = []
                    for city_info in self._monitored_cities:
                        name = city_info["city"]
                        loc = self._find_location(name)
                        if loc is None:
                            continue

                        try:
                            analysis = loop.run_until_complete(
                                self._analyzer.analyze(loc, lead_days=lead_days)
                            )
                            if analysis.ensemble:
                                ens = analysis.ensemble
                                mean_c = (ens.mean_temp_f - 32.0) * 5.0 / 9.0
                                p5_c = (ens.p05_temp_f - 32.0) * 5.0 / 9.0
                                p95_c = (ens.p95_temp_f - 32.0) * 5.0 / 9.0
                                range_c = p95_c - p5_c

                                total_models = max(1, ens.model_count)
                                models_in = 0
                                if ens.individual_models:
                                    for mt in ens.individual_models.values():
                                        mc = (mt - 32.0) * 5.0 / 9.0
                                        if p5_c - 2 <= mc <= p95_c + 2:
                                            models_in += 1
                                agree_ratio = models_in / total_models if total_models > 0 else 0
                                narrow_bonus = 1.0 / (1.0 + max(0, range_c / 8.0))
                                conf = ens.confidence * (0.4 + 0.6 * agree_ratio) * min(1.0, 1.0 + narrow_bonus * 0.3)
                                conf = min(0.99, max(0.05, conf))

                                result = {
                                    "city": name,
                                    "mean_c": round(mean_c, 1),
                                    "p5_c": round(p5_c, 1),
                                    "p95_c": round(p95_c, 1),
                                    "range_c": round(range_c, 2),
                                    "confidence": round(conf, 3),
                                    "conf_pct": round(conf * 100, 0),
                                    "models_agree": models_in,
                                    "total_models": total_models,
                                }
                                results.append(result)
                                self._last_bma_result[name] = result

                                # Track confidence history
                                if name not in self._confidence_history:
                                    self._confidence_history[name] = []
                                history = self._confidence_history[name]
                                history.append(conf)
                                if len(history) > 10:
                                    history.pop(0)
                        except Exception:
                            pass

                    loop.close()
                    last_bma_refresh = now_ts

                    # Update UI on main thread
                    self.root.after(0, lambda r=results: self._refresh_monitoring_ui(r))

                except Exception:
                    pass

            # --- API daily max fetch (every 30 min, per-city) ---
            if now_ts - last_api_max_fetch >= API_MAX_INTERVAL:
                try:
                    loop = _asyncio.new_event_loop()
                    _asyncio.set_event_loop(loop)

                    for city_info in self._monitored_cities[:5]:
                        name = city_info["city"]
                        loc = self._find_location(name)
                        if loc is None:
                            continue
                        try:
                            api_data = loop.run_until_complete(
                                self._analyzer.get_today_max(loc.lat, loc.lon, loc.tz)
                            )
                            if api_data and api_data.get("max_c") is not None:
                                api_max_c = api_data["max_c"]
                                peak_time = api_data.get("peak_time", "")
                                self._api_daily_max[name] = {"max_c": api_max_c, "peak_time": peak_time}

                                # Update today_max with API value if higher than observed
                                observed_max = self._today_max.get(name)
                                if observed_max is None or api_max_c > observed_max[0]:
                                    # Parse peak_time into datetime if available
                                    peak_dt = datetime.now()
                                    if peak_time:
                                        try:
                                            peak_dt = datetime.fromisoformat(peak_time)
                                        except (ValueError, TypeError):
                                            pass
                                    self._today_max[name] = (api_max_c, peak_dt)
                        except Exception:
                            pass

                    loop.close()
                    last_api_max_fetch = now_ts
                except Exception:
                    pass

            # --- Current temp fetch (per-city, at appropriate interval) ---
            for city_info in self._monitored_cities[:5]:  # top 5 only
                name = city_info["city"]
                lat = city_info["lat"]
                lon = city_info["lon"]

                # Determine fetch interval for this city
                peak_state = self._peak_state.get(name)
                in_peak = peak_state is not None and peak_state.state in ("peak_window", "possible_peak")
                temp_interval = TEMP_PEAK_INTERVAL if in_peak else TEMP_NORMAL_INTERVAL

                last_fetch = last_temp_fetch.get(name, 0.0)
                if now_ts - last_fetch >= temp_interval:
                    last_temp_fetch[name] = now_ts
                    try:
                        loop = _asyncio.new_event_loop()
                        _asyncio.set_event_loop(loop)

                        loc = self._find_location(name)
                        tz = loc.tz if loc else "UTC"
                        temp_data = loop.run_until_complete(
                            self._analyzer.get_current_temp(lat, lon, tz)
                        )
                        loop.close()

                        if temp_data and temp_data.get("temp_c") is not None:
                            temp_c = temp_data["temp_c"]
                            ts = temp_data.get("time_local") or datetime.now()

                            # Store current conditions (humidity, wind, cloud cover)
                            self._current_conditions[name] = {
                                "humidity": temp_data.get("humidity"),
                                "wind_speed": temp_data.get("wind_speed"),
                                "wind_direction": temp_data.get("wind_direction"),
                                "wind_dir_compass": temp_data.get("wind_dir_compass"),
                                "cloud_cover": temp_data.get("cloud_cover"),
                            }

                            # Update observation history
                            if name not in self._obs_history:
                                self._obs_history[name] = []
                            self._obs_history[name].append((ts, temp_c))
                            # Keep last 60 minutes (12 readings at 5-min interval)
                            cutoff = ts - timedelta(minutes=65)
                            self._obs_history[name] = [
                                (t, v) for t, v in self._obs_history[name] if t > cutoff
                            ]

                            # Update today_max
                            prev_max = self._today_max.get(name)
                            if prev_max is None or temp_c > prev_max[0]:
                                self._today_max[name] = (temp_c, ts)

                            # Run peak detection (date-aware)
                            loc = self._find_location(name)
                            peak_start = getattr(loc, "peak_hour_start", 14) if loc else 14
                            peak_end = getattr(loc, "peak_hour_end", 16) if loc else 16
                            try:
                                local_now = datetime.now(ZoneInfo(tz))
                            except Exception:
                                local_now = datetime.now()

                            target_date = date.today() + timedelta(days=lead_days)
                            today_local = local_now.date()

                            peak_confirmed = self._peak_confirmed.get(name)
                            suggested_temp = self._suggested_temps.get(name)
                            peak_state = detect_peak_state(
                                obs_history=self._obs_history.get(name, []),
                                today_max=self._today_max.get(name),
                                peak_hour_start=peak_start,
                                peak_hour_end=peak_end,
                                local_now=local_now,
                                target_date=target_date,
                                peak_confirmed=peak_confirmed,
                                suggested_temp=suggested_temp,
                            )
                            self._peak_state[name] = peak_state

                            # --- Alert triggers (deduplicated) ---
                            last_alert = self._last_alert_level.get(name, "")

                            # 🎯 "Momentant over" — temp exceeds suggested bet
                            if (peak_state.alert_level == "info" and last_alert != "info"
                                    and name not in self._momentant_over_alerted):
                                self._momentant_over_alerted[name] = True
                                self._last_alert_level[name] = "info"
                                self.root.after(0, lambda n=name, ct=temp_c, st=suggested_temp:
                                    messagebox.showinfo(
                                        "🟢 INFO — Temp over foreslått spill",
                                        f"🌡️ {n}: {ct:.1f}°C — over anbefalt spill {st:.0f}°C! Vurder å selge."
                                    )
                                )

                            # 🎯 "Momentant over" — temp spikes ABOVE suggested (bet wins!)
                            if (temp_c is not None and suggested_temp is not None
                                    and temp_c > suggested_temp
                                    and name not in self._momentant_over_alerted):
                                self._momentant_over_alerted[name] = True
                                self._last_alert_level[name] = "info"
                                self.root.after(0, lambda n=name, ct=temp_c, st=suggested_temp:
                                    messagebox.showinfo(
                                        "🎯 MOMENTANT OVER!",
                                        f"🎯 {n}: {ct:.1f}°C — OVER {st:.0f}°C! Bet vinner! Vurder å selge posisjon for gevinst."
                                    )
                                )

                            # 🟡 ADVARSEL — Peak sannsynlig
                            if peak_state.alert_level == "advarsel" and last_alert not in ("advarsel", "kritisk", "bekreftet"):
                                self._last_alert_level[name] = "advarsel"
                                self.root.after(0, lambda n=name, ps=peak_state:
                                    messagebox.showwarning(
                                        "🟡 ADVARSEL — Peak sannsynlig",
                                        f"⚠️ {n}: Peak sannsynlig nådd! Live confidence: {ps.live_confidence:.0f}%. "
                                        f"Vurder å snu posisjon."
                                    )
                                )

                            # 🟠 KRITISK — Snu posisjon NÅ
                            if peak_state.alert_level == "kritisk" and last_alert != "kritisk":
                                self._last_alert_level[name] = "kritisk"
                                tmax = self._today_max.get(name)
                                tmax_val = tmax[0] if tmax else (temp_c if temp_c else 0)
                                self.root.after(0, lambda n=name, ps=peak_state, tv=tmax_val, ct=temp_c:
                                    messagebox.showwarning(
                                        "🟠 KRITISK — SNU POSISJON NÅ!",
                                        f"🔥 SNU POSISJON: {n} peak {tv:.1f}°C bekreftet! "
                                        f"{ct:.1f}°C synkende. Confidence: {ps.live_confidence:.0f}%. "
                                        f"SNU FØR MARKEDET REAGERER!"
                                    )
                                )

                            # 🔴 BEKREFTET — Peak låst
                            if peak_state.alert_level == "bekreftet" and last_alert != "bekreftet":
                                self._last_alert_level[name] = "bekreftet"
                                tmax = self._today_max.get(name)
                                tmax_val = tmax[0] if tmax else (temp_c if temp_c else 0)
                                self.root.after(0, lambda n=name, tv=tmax_val:
                                    messagebox.showinfo(
                                        "🔴 BEKREFTET — Peak frosset",
                                        f"✅ {n}: Peak {tv:.1f}°C låst. Markedet vil justeres snart."
                                    )
                                )

                            # Legacy: If peak just confirmed via detect_peak_state, trigger alert + freeze it
                            if peak_state.state == "confirmed" and name not in self._peak_confirmed:
                                if peak_state.confirmed_temp is not None:
                                    self._peak_confirmed[name] = (
                                        peak_state.confirmed_temp,
                                        peak_state.confirmed_time or local_now,
                                    )
                                    if last_alert != "bekreftet":
                                        self.root.after(0, lambda n=name, ps=peak_state:
                                            messagebox.showwarning(
                                                "🔴 PEAK BEKREFTET!",
                                                f"{n}: {ps.message}"
                                            )
                                        )
                    except Exception:
                        pass

            # ---- SMS Alert Check (integrated into monitoring loop) ----
            try:
                from _sms_alert import send_sms, can_send_sms_for_city, mark_sms_sent
                for city_info in self._monitored_cities[:5]:
                    city_name = city_info["city"]
                    peak_state = self._peak_state.get(city_name)
                    if peak_state is None:
                        continue
                    live_conf = peak_state.live_confidence
                    trend = peak_state.trend
                    suggested_temp = self._suggested_temps.get(city_name)
                    current_temp = None
                    obs_list = self._obs_history.get(city_name, [])
                    if obs_list:
                        current_temp = obs_list[-1][1]

                    # SMS trigger conditions: peak_confidence > 70%, live_conf > 60%, declining, strategy at risk
                    bma_conf = 0.0
                    bma_result = self._last_bma_result.get(city_name, {})
                    if bma_result:
                        bma_conf = bma_result.get("confidence", 0.0)

                    if (bma_conf > 0.70 and live_conf > 60 and trend == "↓"
                            and can_send_sms_for_city(city_name)):
                        if current_temp is not None and suggested_temp is not None:
                            if current_temp < suggested_temp - 0.5:
                                conf_pct = int(bma_conf * 100)
                                msg = (
                                    f"VARSMONITOR: {city_name} peak sannsynlig ({conf_pct}%). "
                                    f"KJOP {int(suggested_temp)}C star i fare. "
                                    f"Na {current_temp:.1f}C synkende. Vurder SELG."
                                )
                                loop_sms = _asyncio.new_event_loop()
                                _asyncio.set_event_loop(loop_sms)
                                try:
                                    loop_sms.run_until_complete(send_sms(msg))
                                except Exception:
                                    pass
                                finally:
                                    loop_sms.close()
                                mark_sms_sent(city_name)
            except Exception:
                pass

            # Sleep until next check
            for _ in range(CHECK_INTERVAL):
                if not self._monitoring_active:
                    break
                import time as _time
                _time.sleep(1)

    # -------------------------------------------------------------------
    # Helper: find SavedLocation by city name
    # -------------------------------------------------------------------

    def _find_location(self, city_name: str) -> Any:
        """Find a SavedLocation by city name."""
        for sl in self._loc_mgr.locations:
            if sl.name == city_name:
                return sl
        return None

    # -------------------------------------------------------------------
    # Refresh Monitoring UI — Live Confidence + Alerts + Flip Opportunity
    # -------------------------------------------------------------------

    def _refresh_monitoring_ui(self, results: list[dict[str, Any]]) -> None:
        """Update the Overvåkning tab with live confidence, alerts, flip panels."""
        if not self._monitoring_active:
            return

        for widget in self._mon_text.winfo_children():
            widget.destroy()

        now = datetime.now().strftime("%H:%M:%S")
        self._mon_status_label.configure(text=f"🔄 Sist oppdatert: {now}")

        # ---- Statistical Edge Info Box (shown once at top) ----
        edge_frame = tk.Frame(
            self._mon_text,
            bg="#1a237e",
            padx=12,
            pady=8,
            highlightbackground="#0d47a1",
            highlightthickness=1,
        )
        edge_frame.pack(fill=X, pady=(0, 8))

        tk.Label(
            edge_frame,
            text="📊 STATISTISK EDGE — EARLY PEAK DETECTION",
            font=("Segoe UI", 11, "bold"),
            bg="#1a237e",
            fg="#ffffff",
        ).pack(anchor=W)

        tk.Label(
            edge_frame,
            text="Sannsynlighet for ny rekord etter 15+ min nedgang: <5%  |  "
                 "Typisk markedsreaksjonstid: 10-30 min  |  "
                 "Anbefalt handlingsvindu: umiddelbart — 15 min",
            font=("Consolas", 9),
            bg="#1a237e",
            fg="#bbdefb",
        ).pack(anchor=W, pady=(2, 0))

        tk.Label(
            edge_frame,
            text="Kilde: WMO 1-10min rapportering, Polymarket CLI settlement 8AM ET neste dag, "
                 "Open-Meteo oppdateres hvert 5. min",
            font=("Segoe UI", 7, "italic"),
            bg="#1a237e",
            fg="#90caf9",
        ).pack(anchor=W)

        # ---- Correlation warnings ----
        mon_names = [c["city"] for c in results]
        corr_warnings = self._check_correlation_warnings(mon_names)
        if corr_warnings:
            corr_frame_m = tk.Frame(self._mon_text, bg="#fff3e0",
                                     highlightbackground="#E65100", highlightthickness=1,
                                     padx=10, pady=6)
            corr_frame_m.pack(fill=X, pady=(0, 6))
            for w in corr_warnings:
                tk.Label(corr_frame_m, text=w, font=("Segoe UI", 8, "bold"),
                         bg="#fff3e0", fg="#E65100").pack(anchor=W)

        # Build location lookup for UHI/station info
        loc_lookup_m: dict[str, Any] = {}
        for sl in self._loc_mgr.locations:
            loc_lookup_m[sl.name] = sl

        # ---- Per-City Cards ----
        for rank, c in enumerate(results):
            city_name = c["city"]
            conf_pct = c["conf_pct"]
            models_str = f"{c['models_agree']}/{c['total_models']}"
            peak_state = self._peak_state.get(city_name)
            suggested_temp = self._suggested_temps.get(city_name)
            obs_list = self._obs_history.get(city_name, [])
            conditions = self._current_conditions.get(city_name, {})

            # Get UHI + station info
            loc_m = loc_lookup_m.get(city_name)
            uhi_m = getattr(loc_m, "uhi_adjustment", 0.0) if loc_m else 0.0
            station_m = getattr(loc_m, "station", "") if loc_m else ""
            elev_m = getattr(loc_m, "station_elevation_m", 0.0) if loc_m else 0.0

            # --- Determine border color from peak state ---
            if peak_state is not None:
                border_color = peak_state.color_hex
                city_emoji = peak_state.emoji
                peak_msg = peak_state.message
                live_conf = peak_state.live_confidence
                mins_decline = getattr(peak_state, "minutes_of_decline", 0)
                mins_since_max = getattr(peak_state, "minutes_since_last_max", 0)
                alert_level = getattr(peak_state, "alert_level", "none")
                peak_state_str = peak_state.state
            else:
                if conf_pct >= 85:
                    border_color = "#2e7d32"
                elif conf_pct >= 70:
                    border_color = "#f57f17"
                else:
                    border_color = "#c62828"
                city_emoji = ""
                peak_msg = ""
                live_conf = 0.0
                mins_decline = 0
                mins_since_max = 0
                alert_level = "none"
                peak_state_str = "unknown"

            # HIGH CONTRAST: white background with colored left border
            card_bg = "#ffffff"

            conf_color = border_color
            acc = self._get_accent_text_color(border_color)

            # --- Compute confidence adjustments from conditions ---
            humidity = conditions.get("humidity")
            cloud_cover = conditions.get("cloud_cover")
            wind_speed = conditions.get("wind_speed")
            wind_dir = conditions.get("wind_dir_compass") or "—"
            conf_adjustments: list[tuple[str, float]] = []
            if humidity is not None:
                if humidity > 80:
                    conf_adjustments.append(("Høy fuktighet", -8))
                elif humidity < 40:
                    conf_adjustments.append(("Tørr luft", +3))
            if cloud_cover is not None:
                if cloud_cover > 70:
                    conf_adjustments.append(("Mye skyer", -5))
                elif cloud_cover < 20:
                    conf_adjustments.append(("Klart", +3))
            total_adj = sum(adj for _, adj in conf_adjustments)
            adjusted_conf_pct = max(5, min(99, conf_pct + total_adj))

            # --- Timezone + peak info ---
            tz_str = "UTC"
            peak_start = 14
            peak_end = 16
            for sl in self._loc_mgr.locations:
                if sl.name == city_name:
                    tz_str = sl.tz
                    peak_start = getattr(sl, "peak_hour_start", 14)
                    peak_end = getattr(sl, "peak_hour_end", 16)
                    break

            local_time_str = ""
            local_date_str = ""
            tz_short = ""
            local_now = None
            in_peak = False
            try:
                from zoneinfo import ZoneInfo as _ZIMon
            except ImportError:
                from backports.zoneinfo import ZoneInfo as _ZIMon  # type: ignore[no-redef]
            try:
                local_now = datetime.now(_ZIMon(tz_str))
                tz_short = local_now.strftime("%Z")
                local_date_str = local_now.strftime("%Y-%m-%d")
                local_time_str = f"🕐 {local_now.strftime('%H:%M %Z')}"
                in_peak = peak_start <= local_now.hour < peak_end
            except Exception:
                pass

            # --- Data extraction ---
            cur_temp = obs_list[-1][1] if obs_list else None
            trend = peak_state.trend if peak_state else "→"
            tmax = self._today_max.get(city_name)
            tmax_val = tmax[0] if tmax else (cur_temp or 0)
            tmax_time_str = ""
            if tmax and hasattr(tmax[1], "strftime"):
                tmax_time_str = tmax[1].strftime("%H:%M")

            # ══════ CARD ══════
            # White card with colored left border
            card = tk.Frame(
                self._mon_text,
                bg=card_bg,
                highlightbackground=border_color,
                highlightthickness=1,
                padx=0,
                pady=0,
            )
            card.pack(fill=X, pady=(0, 6))

            # LEFT COLORED BORDER (4px)
            left_bar_m = tk.Frame(card, bg=border_color, width=4)
            left_bar_m.pack(side=LEFT, fill=Y)
            left_bar_m.pack_propagate(False)

            # Content area inside card
            inner = tk.Frame(card, bg=card_bg, padx=10, pady=6)
            inner.pack(side=LEFT, fill=BOTH, expand=YES)

            # --- Header: City name + date ---
            hdr = tk.Frame(inner, bg=card_bg)
            hdr.pack(fill=X)
            tk.Label(
                hdr,
                text=f"{city_emoji} {city_name} — {local_date_str}",
                font=("Segoe UI", 10, "bold"),
                bg=card_bg, fg="#1a1a1a",
            ).pack(side=LEFT)
            tk.Label(
                hdr,
                text=f"BMA Konfidens: {conf_pct:.0f}%",
                font=("Segoe UI", 9, "bold"), bg=card_bg, fg=conf_color,
            ).pack(side=RIGHT)

            # --- Time + Peak row ---
            peak_status_text = ""
            peak_status_color = "#888"
            if peak_state is not None and peak_state.state in ("future_date", "past_date"):
                peak_status_text = peak_msg
                peak_status_color = peak_state.color_hex
            elif in_peak:
                peak_status_text = f"🟡 Peak: {peak_start:02d}:00-{peak_end:02d}:00 (AKTIV)"
                peak_status_color = "#E65100"
            elif local_now and local_now.hour < peak_start:
                peak_status_text = f"⏳ Peak: {peak_start:02d}:00-{peak_end:02d}:00 (venter)"
                peak_status_color = "#888"
            elif peak_state_str in ("confirmed", "completed"):
                peak_status_text = f"🔴 Peak: {peak_start:02d}:00-{peak_end:02d}:00 (PASSERT)"
                peak_status_color = "#c62828"
            else:
                peak_status_text = f"✅ Peak: {peak_start:02d}:00-{peak_end:02d}:00 (passert)"
                peak_status_color = "#2e7d32"

            time_row = tk.Frame(inner, bg=card_bg)
            time_row.pack(fill=X)
            tk.Label(
                time_row,
                text=f"{local_time_str} {tz_short} | {peak_status_text}",
                font=("Consolas", 8),
                bg=card_bg, fg=peak_status_color,
            ).pack(anchor=W)

            # Station info row (copyable)
            if station_m:
                st_row_m = tk.Frame(inner, bg=card_bg)
                st_row_m.pack(fill=X)
                st_txt_m = f"📡 Stasjon: {station_m}"
                if elev_m:
                    st_txt_m += f" ({elev_m:.0f}m moh.)"
                st_text_w = tk.Text(st_row_m, height=1, width=60, font=("Consolas", 8),
                                    bg=card_bg, fg="#555", relief="flat",
                                    borderwidth=0, wrap=tk.WORD, exportselection=True)
                st_text_w.insert("1.0", st_txt_m)
                st_text_w.configure(state=tk.DISABLED)
                st_text_w.pack(anchor=W)

            # ══════ SEPARATOR ══════
            sep1 = tk.Frame(inner, bg=border_color, height=1)
            sep1.pack(fill=X, pady=(4, 4))

            # --- Temperature row (copyable) ---
            temp_row = tk.Frame(inner, bg=card_bg)
            temp_row.pack(fill=X)
            temp_parts = []
            if cur_temp is not None:
                temp_parts.append(f"🌡️ Nå: {cur_temp:.1f}°C {trend}")
            if tmax:
                temp_parts.append(f"Max i dag: {tmax_val:.1f}°C")
                if tmax_time_str:
                    temp_parts[-1] += f" ({tmax_time_str})"

            # --- API daily max vs observed ---
            api_max_data = self._api_daily_max.get(city_name)
            if api_max_data is not None:
                api_max_c = api_max_data.get("max_c")
                peak_time = api_max_data.get("peak_time", "")
                # Compute observed max from history for comparison
                obs_max_val = None
                for (t, v) in obs_list:
                    if obs_max_val is None or v > obs_max_val:
                        obs_max_val = v
                api_parts = [f"📡 Faktisk dagsmaks (API): {api_max_c:.1f}°C"]
                if peak_time:
                    # Extract just the hour from ISO time e.g. "2026-08-08T16:00" -> "16:00"
                    try:
                        peak_dt = datetime.fromisoformat(peak_time)
                        api_parts[-1] += f" kl {peak_dt.strftime('%H:%M')}"
                    except (ValueError, TypeError):
                        pass
                if obs_max_val is not None and abs(obs_max_val - api_max_c) > 0.1:
                    api_parts.append(f"📈 Vår observerte maks: {obs_max_val:.1f}°C (siden oppstart)")
                temp_display = " | ".join(temp_parts + api_parts) if temp_parts else "🌡️ Ingen data"
            else:
                temp_display = " | ".join(temp_parts) if temp_parts else "🌡️ Ingen data"

            temp_text_w = tk.Text(temp_row, height=2 if api_max_data else 1, width=60,
                                  font=("Consolas", 9, "bold"),
                                  bg=card_bg, fg="#1a1a1a", relief="flat",
                                  borderwidth=0, wrap=tk.WORD, exportselection=True)
            temp_text_w.insert("1.0", temp_display)
            temp_text_w.configure(state=tk.DISABLED)
            temp_text_w.pack(side=LEFT)

            # --- BMA + suggested bet row with UHI (copyable) ---
            bma_row = tk.Frame(inner, bg=card_bg)
            bma_row.pack(fill=X)
            bma_text = f"📊 BMA: {c['mean_c']}°C"
            if uhi_m > 0:
                adj_m = c['mean_c'] + uhi_m
                bma_text += f" (+{uhi_m:.1f}°C UHI = {adj_m:.1f}°C justert)"
            if suggested_temp is not None:
                bma_text += f" | Anbefalt spill: {suggested_temp:.0f}°C"
            bma_text += f" | Range: {c['range_c']}°C | {models_str} modeller"
            bma_text_w = tk.Text(bma_row, height=1, width=80, font=("Consolas", 8),
                                 bg=card_bg, fg="#1a1a1a", relief="flat",
                                 borderwidth=0, wrap=tk.WORD, exportselection=True)
            bma_text_w.insert("1.0", bma_text)
            bma_text_w.configure(state=tk.DISABLED)
            bma_text_w.pack(anchor=W)

            # Ensemble spread signal (copyable)
            spd_row_m = tk.Frame(inner, bg=card_bg)
            spd_row_m.pack(fill=X)
            range_c = c.get("range_c", 0)
            spread_signal = "medium"
            if range_c <= 2.0:
                spread_signal = "narrow"
            elif range_c > 5.0:
                spread_signal = "wide"

            spread_txt = f"📊 Modell-spredning: {range_c:.1f}°C"
            if spread_signal == "narrow":
                spread_txt += " (smal = høy konfidens)"
            elif spread_signal == "wide":
                spread_txt += " ⚠️ Høy spredning — mulig edge"
            spd_text_w = tk.Text(spd_row_m, height=1, width=60, font=("Consolas", 8),
                                 bg=card_bg, fg="#555", relief="flat",
                                 borderwidth=0, wrap=tk.WORD, exportselection=True)
            spd_text_w.insert("1.0", spread_txt)
            spd_text_w.configure(state=tk.DISABLED)
            spd_text_w.pack(anchor=W)

            # Position sizing recommendation based on spread (PRI 3)
            pos_row_m = tk.Frame(inner, bg=card_bg)
            pos_row_m.pack(fill=X)
            conf_pct_val = conf_pct
            if spread_signal == "narrow" and conf_pct_val >= 70:
                pos_rec = "💰 Posisjon: Stor (3-5%) — smal spredning + høy edge"
                pos_color = "#2e7d32"
            elif spread_signal == "wide":
                pos_rec = "⚠️ Posisjon: Unngå — modeller uenige (høy spredning)"
                pos_color = "#c62828"
            elif spread_signal == "narrow":
                pos_rec = "💰 Posisjon: Moderat (1-3%) — smal spredning"
                pos_color = "#f57f17"
            else:
                pos_rec = "💰 Posisjon: Moderat (1-3%) — standard spredning"
                pos_color = "#555"
            pos_text_w = tk.Text(pos_row_m, height=1, width=60, font=("Consolas", 8, "bold"),
                                 bg=card_bg, fg=pos_color, relief="flat",
                                 borderwidth=0, wrap=tk.WORD, exportselection=True)
            pos_text_w.insert("1.0", pos_rec)
            pos_text_w.configure(state=tk.DISABLED)
            pos_text_w.pack(anchor=W)

            # ══════ LIVE CONFIDENCE BAR ══════
            conf_row = tk.Frame(inner, bg=card_bg)
            conf_row.pack(fill=X, pady=(3, 0))

            if live_conf >= 80:
                bar_color = "#c62828"; conf_emoji = "🔥"
            elif live_conf >= 60:
                bar_color = "#f57f17"; conf_emoji = "⚠️"
            elif live_conf >= 30:
                bar_color = "#f9a825"; conf_emoji = "👀"
            else:
                bar_color = "#9e9e9e"; conf_emoji = "⏳"

            tk.Label(
                conf_row, text="⚡ Live confidence:",
                font=("Segoe UI", 8, "bold"), bg=card_bg, fg="#1a1a1a",
            ).pack(side=LEFT)

            bar_width = 25
            filled = int(bar_width * live_conf / 100)
            bar_text = "█" * filled + "░" * (bar_width - filled)
            tk.Label(
                conf_row, text=f" {bar_text}",
                font=("Consolas", 8, "bold"), bg=card_bg, fg=bar_color,
            ).pack(side=LEFT)

            # --- Confidence display with adjustments ---
            if conf_adjustments:
                adj_text = f" {conf_emoji} {adjusted_conf_pct:.0f}%"
                adj_detail = f"(justert fra {conf_pct:.0f}%: "
                adj_detail += ", ".join(f"{adj:+d}% {reason}" for reason, adj in conf_adjustments)
                adj_detail += ")"
            else:
                adj_text = f" {conf_emoji} {conf_pct:.0f}%"
                adj_detail = ""

            tk.Label(
                conf_row, text=adj_text,
                font=("Segoe UI", 8, "bold"), bg=card_bg, fg=bar_color,
            ).pack(side=LEFT)

            if adj_detail:
                adj_detail_row = tk.Frame(inner, bg=card_bg)
                adj_detail_row.pack(fill=X)
                tk.Label(
                    adj_detail_row, text=f"   Konfidens: {adj_detail}",
                    font=("Consolas", 7), bg=card_bg, fg="#555",
                ).pack(anchor=W)

            # --- Alert level indicator ---
            if alert_level == "bekreftet":
                alert_icon = "🔴 BEKREFTET"; alert_color = "#c62828"
                alert_detail = "Peak låst — markedet justeres snart"
            elif alert_level == "kritisk":
                alert_icon = "🟠 KRITISK"; alert_color = "#E65100"
                alert_detail = "SNU POSISJON — <5% sjanse for ny rekord!"
            elif alert_level == "advarsel":
                alert_icon = "🟡 ADVARSEL"; alert_color = "#f57f17"
                alert_detail = "Peak sannsynlig nådd — vurder å snu"
            elif alert_level == "info":
                alert_icon = "🟢 INFO"; alert_color = "#2e7d32"
                alert_detail = "Temp over anbefalt spill"
            else:
                alert_icon = ""; alert_color = "#888"; alert_detail = ""

            if alert_icon:
                alert_row = tk.Frame(inner, bg=card_bg)
                alert_row.pack(fill=X)
                tk.Label(
                    alert_row, text=f"  {alert_icon}",
                    font=("Segoe UI", 7, "bold"), bg=card_bg, fg=alert_color,
                ).pack(side=LEFT)
                if alert_detail:
                    tk.Label(
                        alert_row, text=f" — {alert_detail}",
                        font=("Segoe UI", 7), bg=card_bg, fg=alert_color,
                    ).pack(side=LEFT)

            # --- Detailed confidence breakdown ---
            if live_conf > 0 or mins_decline > 0:
                detail_row = tk.Frame(inner, bg=card_bg)
                detail_row.pack(fill=X)
                parts = []
                if mins_decline > 0:
                    parts.append(f"Temp synkende i {mins_decline} min")
                if mins_since_max > 0:
                    parts.append(f"Tid siden siste rekord: {mins_since_max} min")
                if suggested_temp is not None and cur_temp is not None:
                    diff = cur_temp - suggested_temp
                    if diff < 0:
                        parts.append(f"Temp under anbefalt spill ({suggested_temp:.0f}°C): {diff:.1f}°C")
                if parts:
                    tk.Label(
                        detail_row,
                        text="  ⏱️ " + " | ".join(parts),
                        font=("Consolas", 7), bg=card_bg, fg="#777",
                    ).pack(anchor=W)

            # ═══════════════════════════════════════════════════════
            # 💹 BET UTFALL Panel — Bug 2: Show when confirmed/completed
            # ═══════════════════════════════════════════════════════
            is_final = peak_state_str in ("confirmed", "completed")
            if is_final and suggested_temp is not None:
                sep_bet = tk.Frame(inner, bg=border_color, height=1)
                sep_bet.pack(fill=X, pady=(6, 4))

                bet_frame = tk.Frame(inner, bg="#fff8e1", highlightbackground="#ff8f00",
                                     highlightthickness=1, padx=8, pady=6)
                bet_frame.pack(fill=X, pady=(0, 2))

                actual_peak = tmax_val
                if peak_state is not None and peak_state.confirmed_temp is not None:
                    actual_peak = peak_state.confirmed_temp
                peak_dist = actual_peak - suggested_temp

                if peak_dist >= 0:
                    outcome_icon = "✅"
                    outcome_text = "VINNER"
                    outcome_color = "#2e7d32"
                    outcome_detail = f"KJØP {suggested_temp:.0f}°C YES VINNER"
                    action = "Hold eller selg for gevinst"
                else:
                    outcome_icon = "❌"
                    outcome_text = "TAPER"
                    outcome_color = "#c62828"
                    outcome_detail = f"KJØP {suggested_temp:.0f}°C YES TAPER — peak for lav"
                    action = "SELG posisjonen umiddelbart"

                tk.Label(
                    bet_frame,
                    text=f"💹 BET UTFALL: {outcome_icon} {outcome_text}",
                    font=("Segoe UI", 10, "bold"),
                    bg="#fff8e1", fg=outcome_color,
                ).pack(anchor=W)

                tk.Label(
                    bet_frame,
                    text=f"   Anbefalt spill: {suggested_temp:.0f}°C",
                    font=("Consolas", 8), bg="#fff8e1", fg="#333",
                ).pack(anchor=W)

                dist_str = f"{abs(peak_dist):.1f}°C {'over' if peak_dist >= 0 else 'under'}"
                tk.Label(
                    bet_frame,
                    text=f"   Faktisk peak: {actual_peak:.1f}°C ({dist_str})",
                    font=("Consolas", 8), bg="#fff8e1", fg="#333",
                ).pack(anchor=W)

                tk.Label(
                    bet_frame,
                    text=f"   Utfall: {outcome_icon} {outcome_detail}",
                    font=("Consolas", 8, "bold"), bg="#fff8e1", fg=outcome_color,
                ).pack(anchor=W)

                tk.Label(
                    bet_frame,
                    text=f"   ⚡ Handling: {action}",
                    font=("Consolas", 8, "bold"), bg="#fff8e1", fg="#E65100",
                ).pack(anchor=W)

            # ═══════════════════════════════════════════════════════
            # ✅ ANBEFALING Panel — Bug 1: Recommendation for
            #    confirmed/completed when temp differs from suggested
            # ═══════════════════════════════════════════════════════
            if is_final and suggested_temp is not None and cur_temp is not None:
                peak_dist = cur_temp - suggested_temp
                if abs(peak_dist) > 1.0:
                    rec_frame = tk.Frame(inner, bg="#e8eaf6",
                                         highlightbackground="#3949ab",
                                         highlightthickness=2, padx=10, pady=8)
                    rec_frame.pack(fill=X, pady=(4, 0))

                    if peak_dist < 0:
                        rec_text = (
                            f"✅ ANBEFALING: Peak bekreftet på {tmax_val:.1f}°C — "
                            f"{abs(peak_dist):.1f}°C UNDER anbefalt spill "
                            f"({suggested_temp:.0f}°C). SELG {suggested_temp:.0f}°C YES — beten taper."
                        )
                        rec_color = "#c62828"
                    else:
                        rec_text = (
                            f"✅ ANBEFALING: Peak bekreftet på {tmax_val:.1f}°C — "
                            f"{peak_dist:.1f}°C OVER anbefalt spill "
                            f"({suggested_temp:.0f}°C). KJØP {suggested_temp:.0f}°C YES — beten vinner!"
                        )
                        rec_color = "#2e7d32"

                    tk.Label(
                        rec_frame,
                        text=rec_text,
                        font=("Segoe UI", 10, "bold"),
                        bg="#e8eaf6", fg=rec_color,
                        wraplength=750, justify="left",
                    ).pack(anchor=W)

            # ═══════════════════════════════════════════════════════
            # 💹 FLIP MULIGHET Panel (only when NOT confirmed/completed
            #    AND live_confidence > 60%)
            # ═══════════════════════════════════════════════════════
            if not is_final and live_conf >= 60 and suggested_temp is not None and cur_temp is not None:
                flip_card = tk.Frame(
                    inner,
                    bg="#fff3e0",
                    highlightbackground="#E65100",
                    highlightthickness=1,
                    padx=8,
                    pady=4,
                )
                flip_card.pack(fill=X, pady=(4, 0))

                tk.Label(
                    flip_card,
                    text="💹 FLIP MULIGHET:",
                    font=("Segoe UI", 8, "bold"),
                    bg="#fff3e0", fg="#E65100",
                ).pack(anchor=W)

                tk.Label(
                    flip_card,
                    text=f"   Opprinnelig spill: KJØP {suggested_temp:.0f}°C YES",
                    font=("Consolas", 8), bg="#fff3e0", fg="#333",
                ).pack(anchor=W)

                tk.Label(
                    flip_card,
                    text=f"   Nåværende status: {tmax_val:.1f}°C nådd → bet vinner" if tmax_val >= suggested_temp - 0.5
                         else f"   Nåværende status: {tmax_val:.1f}°C — nærmer seg {suggested_temp:.0f}°C",
                    font=("Consolas", 8), bg="#fff3e0", fg="#2e7d32",
                ).pack(anchor=W)

                tk.Label(
                    flip_card,
                    text=f"   Live confidence peak: {live_conf:.0f}%",
                    font=("Consolas", 8), bg="#fff3e0", fg="#c62828",
                ).pack(anchor=W)

                if cur_temp < suggested_temp - 0.5:
                    rec_action = (
                        f"   Anbefaling: SELG {suggested_temp:.0f}°C YES "
                        f"(sikre gevinst før markedet justerer)"
                    )
                else:
                    rec_action = (
                        f"   Anbefaling: Vurder å SELG {suggested_temp:.0f}°C YES "
                        f"(peak sannsynlig nådd)"
                    )
                tk.Label(
                    flip_card,
                    text=rec_action,
                    font=("Consolas", 8, "bold"), bg="#fff3e0", fg="#E65100",
                ).pack(anchor=W)

                tk.Label(
                    flip_card,
                    text="   ⏱️ Estimert tid før markedet justerer: 10-30 min",
                    font=("Consolas", 7, "italic"), bg="#fff3e0", fg="#888",
                ).pack(anchor=W)

            # ═══════════════════════════════════════════════════════
            # 🌬️ Conditions Row — Improvement 3 (copyable)
            # ═══════════════════════════════════════════════════════
            cond_parts = []
            if wind_speed is not None:
                cond_parts.append(f"🌬️ Vind: {wind_speed:.0f} km/h {wind_dir}")
            if humidity is not None:
                cond_parts.append(f"💧 Fuktighet: {humidity:.0f}%")
            if cloud_cover is not None:
                cond_parts.append(f"☁️ Skydekke: {cloud_cover:.0f}%")
            if cond_parts:
                cond_row = tk.Frame(inner, bg=card_bg)
                cond_row.pack(fill=X, pady=(3, 0))
                cond_display = " | ".join(cond_parts)
                cond_text_w = tk.Text(cond_row, height=1, width=60, font=("Consolas", 8),
                                      bg=card_bg, fg="#666", relief="flat",
                                      borderwidth=0, wrap=tk.WORD, exportselection=True)
                cond_text_w.insert("1.0", cond_display)
                cond_text_w.configure(state=tk.DISABLED)
                cond_text_w.pack(anchor=W)

            # --- Observation sparkline ---
            if len(obs_list) >= 2:
                temps = [t for _, t in obs_list[-6:]]
                spark = " → ".join(f"{t:.1f}°C" for t in temps)
                spark_row = tk.Frame(inner, bg=card_bg)
                spark_row.pack(fill=X)
                tk.Label(
                    spark_row,
                    text=f"📈 Obs: {spark}",
                    font=("Consolas", 7), bg=card_bg, fg="#999",
                ).pack(anchor=W)

            # --- Confidence history sparkline ---
            history = self._confidence_history.get(city_name, [])
            if len(history) >= 2:
                hist_str = " → ".join(f"{h:.0%}" for h in history[-5:])
                hist_row = tk.Frame(inner, bg=card_bg)
                hist_row.pack(fill=X)
                tk.Label(
                    hist_row,
                    text=f"📊 BMA konfidens-historikk: {hist_str}",
                    font=("Consolas", 7), bg=card_bg, fg="#999",
                ).pack(anchor=W)

    # -------------------------------------------------------------------
    # Helper: convert hex color to a tkinter-friendly pastel version
    # -------------------------------------------------------------------

    # -------------------------------------------------------------------
    # Card color helpers (high contrast: white bg + colored left border)
    # -------------------------------------------------------------------

    @staticmethod
    def _hex_to_tk_bg(hex_color: str) -> str:
        """Convert a hex color to a lighter pastel background for cards."""
        mapping = {
            "#D32F2F": "#ffcdd2",  # red → light red
            "#FF9800": "#ffe0b2",  # orange → light orange
            "#FFC107": "#fff9c4",  # yellow → light yellow
            "#2196F3": "#bbdefb",  # blue → light blue
            "#4CAF50": "#c8e6c9",  # green → light green
            "#9E9E9E": "#e0e0e0",  # grey → light grey
        }
        return mapping.get(hex_color, "#f5f5f5")

    @staticmethod
    def _get_card_bg(border_color: str) -> str:
        """Return white background for high-contrast card design."""
        return "#ffffff"  # Always white for readability

    @staticmethod
    def _get_accent_text_color(border_color: str) -> str:
        """Get appropriate dark text color for card labels based on border."""
        mapping = {
            "#D32F2F": "#b71c1c",  # dark red
            "#FF9800": "#e65100",  # dark orange
            "#FFC107": "#f57f17",  # dark yellow/amber
            "#2196F3": "#0d47a1",  # dark blue
            "#4CAF50": "#1b5e20",  # dark green
            "#9E9E9E": "#424242",  # dark grey
            "#2e7d32": "#1b5e20",  # dark green
            "#c62828": "#b71c1c",  # dark red
            "#f57f17": "#e65100",  # dark orange
            "#E65100": "#bf360c",  # deep orange
            "#3949ab": "#1a237e",  # dark indigo
            "#ff8f00": "#e65100",  # dark amber
        }
        return mapping.get(border_color, "#1a1a1a")

    @staticmethod
    def _compute_kelly(win_prob: float, odds: float = 1.39) -> tuple[float, str]:
        """Compute Kelly Criterion and return (pct, formatted string)."""
        if odds <= 1.0 or win_prob <= 0.5:
            return (0.0, "")
        b = odds - 1.0
        q = 1.0 - win_prob
        kelly = max(0.0, (b * win_prob - q) / b)
        kelly_pct = kelly * 100.0
        edge_pct = (win_prob * odds - 1.0) * 100
        txt = (
            f"💰 Kelly: {kelly_pct:.1f}% av bankroll\n"
            f"(p={win_prob:.2f}, odds={odds:.2f}, edge={edge_pct:+.0f}%)"
        )
        return (kelly_pct, txt)

    def _check_correlation_warnings(self, city_names: list[str]) -> list[str]:
        """Check for correlated cities and return warning strings."""
        warnings: list[str] = []
        try:
            defaults_path = Path(_SCRIPT_DIR) / "weather_monitor_defaults.json"
            if defaults_path.exists():
                data = json.loads(defaults_path.read_text(encoding="utf-8"))
                correlations = data.get("city_correlations", [])
                city_set = set(city_names)
                for corr in correlations:
                    c1 = corr.get("city1", "")
                    c2 = corr.get("city2", "")
                    r = corr.get("r", 0.0)
                    c1_match = any(c1 in c or c in c1 for c in city_set)
                    c2_match = any(c2 in c or c in c2 for c in city_set)
                    if c1_match and c2_match and r >= 0.55:
                        warnings.append(
                            f"⚠️ {c1} og {c2} er korrelerte (r={r:.2f}). Reduser samlet eksponering."
                        )
        except Exception:
            pass
        return warnings


    # ===================================================================
    # Status Bar
    # ===================================================================

    def _update_status_bar(self) -> None:
        """Refresh the status bar labels."""
        self._status_openmeteo_label.configure(text=f"Open-Meteo: {self._status_openmeteo}")
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self._status_time_label.configure(text=f"Sist oppdatert: {now}")
        self._status_label.configure(text="✅ Klar" if self._initialized else "⏳ Initialiserer...")

    # ===================================================================
    # Location Management (Tab 1)
    # ===================================================================

    def _refresh_location_list(self) -> None:
        """Reload the location listbox from the backend."""
        self._loc_listbox.delete(0, END)
        locations = self._loc_mgr.locations
        for i, loc in enumerate(locations):
            source_tag = " [Standard]" if loc.source == "default" else ""
            self._loc_listbox.insert(END, f"[{i}] {loc.name}{source_tag}  ({loc.lat:.4f}, {loc.lon:.4f})")
        max_loc = LocationManager.MAX_LOCATIONS
        self._loc_count_label.configure(text=f"{len(locations)}/{max_loc} lokasjoner")

        # Also refresh combos on other tabs
        loc_names = [f"[{i}] {loc.name}" for i, loc in enumerate(locations)]
        self._analysis_loc_combo["values"] = loc_names

        # Refresh cities tab
        self._refresh_cities_tab()

    def _add_by_city(self) -> None:
        """Geocode a city name and add it to the location list."""
        city = self._city_entry.get().strip()
        if not city:
            messagebox.showwarning("Mangler input", "Skriv inn et bynavn.")
            return

        if self._loc_mgr.count >= LocationManager.MAX_LOCATIONS:
            messagebox.showwarning(
                "Fullt", f"Maksimalt {LocationManager.MAX_LOCATIONS} lokasjoner. Fjern en først."
            )
            return

        self._set_ui_state(False)
        self._status_label.configure(text="⏳ Geokoder...")

        async def _do() -> Any:
            return await self._geocode_city(city)

        self._async.submit(_do(), self._on_add_city_done)

    def _on_add_city_done(self, result: Any) -> None:
        self._set_ui_state(True)
        if isinstance(result, Exception):
            messagebox.showerror("Geokoding feilet", str(result))
            self._update_status_bar()
            return

        if result is None:
            messagebox.showwarning(
                "Ikke funnet",
                f"Fant ingen resultater for '{self._city_entry.get().strip()}'.\n"
                "Prøv med landkode: 'Oslo, NO', 'New York, US'",
            )
            self._update_status_bar()
            return

        display_name, lat, lon = result
        try:
            self._loc_mgr.add(display_name, lat, lon)
            self._city_entry.delete(0, END)
            self._refresh_location_list()
            self._update_status_bar()
        except ValueError as exc:
            messagebox.showwarning("Feil", str(exc))
            self._update_status_bar()

    def _add_by_coords(self) -> None:
        """Add a location by manual lat/lon entry."""
        name = self._coord_name_entry.get().strip()
        lat_str = self._lat_entry.get().strip()
        lon_str = self._lon_entry.get().strip()

        if not name:
            messagebox.showwarning("Mangler input", "Skriv inn et navn for lokasjonen.")
            return
        if not lat_str or not lon_str:
            messagebox.showwarning("Mangler input", "Skriv inn breddegrad og lengdegrad.")
            return

        try:
            lat = float(lat_str)
            lon = float(lon_str)
        except ValueError:
            messagebox.showwarning("Ugyldig", "Breddegrad og lengdegrad må være tall.")
            return

        if self._loc_mgr.count >= LocationManager.MAX_LOCATIONS:
            messagebox.showwarning(
                "Fullt", f"Maksimalt {LocationManager.MAX_LOCATIONS} lokasjoner. Fjern en først."
            )
            return

        try:
            self._loc_mgr.add(name, lat, lon)
            self._coord_name_entry.delete(0, END)
            self._lat_entry.delete(0, END)
            self._lon_entry.delete(0, END)
            self._refresh_location_list()
            self._update_status_bar()
        except ValueError as exc:
            messagebox.showwarning("Feil", str(exc))

    def _remove_location(self) -> None:
        """Remove the selected location."""
        sel = self._loc_listbox.curselection()
        if not sel:
            messagebox.showwarning("Ingen valgt", "Velg en lokasjon å fjerne.")
            return

        idx = sel[0]
        try:
            loc = self._loc_mgr.remove(idx)
            self._refresh_location_list()
            self._status_label.configure(text=f"🗑️ Fjernet: {loc.name}")
            self._update_status_bar()
        except (ValueError, IndexError) as exc:
            messagebox.showwarning("Feil", str(exc))

    def _clear_locations(self) -> None:
        """Clear all saved locations."""
        if not self._loc_mgr.locations:
            return
        if not messagebox.askyesno("Bekreft", "Tømme alle lagrede lokasjoner?"):
            return
        count = self._loc_mgr.clear()
        self._refresh_location_list()
        self._status_label.configure(text=f"❌ Tømt {count} lokasjon(er)")
        self._update_status_bar()

    def _reset_to_defaults(self) -> None:
        """Reset all locations to the default city database."""
        if not messagebox.askyesno(
            "Bekreft",
            "Tilbakestille alle lokasjoner til standardbyer?\n\n"
            "Dette vil erstatte alle nåværende lokasjoner med de 51 "
            "forhåndsdefinerte byene.",
        ):
            return
        count = self._loc_mgr.reset_to_defaults()
        self._last_analyses = {}
        self._refresh_location_list()
        self._status_label.configure(text=f"🔄 Lastet {count} standardbyer")
        self._update_status_bar()

    # ===================================================================
    # Cities Tab helpers
    # ===================================================================

    def _refresh_cities_tab(self) -> None:
        """Refresh the cities overview tab."""
        for row in self._cities_tree.get_children():
            self._cities_tree.delete(row)

        locations = self._loc_mgr.locations
        for loc in locations:
            self._cities_tree.insert(
                "",
                END,
                values=(loc.name, loc.tz, f"{loc.lat:.4f}, {loc.lon:.4f}"),
            )

        self._cities_summary.configure(
            text=f"{len(locations)} byer tilgjengelig for analyse."
        )

    # ===================================================================
    # Analysis (Tab 3)
    # ===================================================================

    def _on_analysis_loc_selected(self, event: Any) -> None:
        """When a location is selected in the dropdown."""
        pass  # No action needed; user clicks "Kjør analyse"

    def _run_analysis(self) -> None:
        """Run BMA ensemble analysis for the selected location."""
        sel = self._analysis_loc_var.get()
        if not sel:
            messagebox.showwarning("Ingen valgt", "Velg en lokasjon fra nedtrekksmenyen.")
            return

        # Parse index from "[i] name"
        try:
            idx = int(sel.split("]")[0].replace("[", ""))
        except (ValueError, IndexError):
            messagebox.showwarning("Feil", "Ugyldig lokasjonsvalg.")
            return

        try:
            loc = self._loc_mgr.get(idx)
        except IndexError:
            messagebox.showwarning("Feil", "Lokasjon ikke funnet.")
            return

        # Read selected date and compute lead_days
        date_sel = self._analysis_date_var.get().strip()
        lead_days = self._date_lead_map.get(date_sel)
        if lead_days is None:
            lead_days = 1
        self._analysis_lead_days = lead_days

        self._analysis_btn.configure(state="disabled")
        self._analysis_progress.start(10)
        self._status_label.configure(text=f"⏳ Analyserer {loc.name}...")

        async def _do() -> Any:
            return await self._analyzer.analyze(loc, lead_days=lead_days)

        self._async.submit(_do(), self._on_analysis_done)

    def _on_analysis_done(self, result: Any) -> None:
        self._analysis_progress.stop()
        self._analysis_btn.configure(state="normal")

        if isinstance(result, Exception):
            messagebox.showerror("Analyse feilet", str(result))
            self._update_status_bar()
            return

        analysis_result = result  # AnalysisResult

        # Store for later use
        sel = self._analysis_loc_var.get()
        try:
            idx = int(sel.split("]")[0].replace("[", ""))
        except (ValueError, IndexError):
            idx = -1
        if idx >= 0:
            self._last_analyses[idx] = analysis_result

        # Clear previous results
        for widget in self._analysis_text.winfo_children():
            widget.destroy()

        if analysis_result.error:
            ttk.Label(
                self._analysis_text,
                text=f"❌ Feil: {analysis_result.error}",
                foreground="#c62828",
                wraplength=800,
            ).pack(anchor=W, pady=5)
            self._update_status_bar()
            return

        ens = analysis_result.ensemble
        if ens is None:
            ttk.Label(
                self._analysis_text,
                text="❌ Ingen ensemble-data returnert.",
                foreground="#c62828",
            ).pack(anchor=W, pady=5)
            self._update_status_bar()
            return

        loc = analysis_result.location

        # --- Forecast Summary ---
        summary_frame = ttk.LabelFrame(self._analysis_text, text="🌡️ Temperaturprognose", padding=10)
        summary_frame.pack(fill=X, pady=(0, 10))

        def _c(val_f: float) -> float:
            """F to C conversion (inline to avoid import issues)."""
            return (val_f - 32.0) * 5.0 / 9.0

        # --- Local time for this location ---
        local_time_str = ""
        try:
            local_now = datetime.now(ZoneInfo(loc.tz))
            local_time_str = f"🕐 {local_now.strftime('%H:%M %Z (%Y-%m-%d)')}"
        except Exception:
            pass

        rows = [
            ("Lokasjon:", ens.location or loc.name),
            ("Dato:", ens.target_date or "i dag"),
            ("Lead Days:", str(ens.lead_days)),
            ("Modeller:", str(ens.model_count)),
            ("", ""),
            ("BMA Gj.snitt:", f"{ens.mean_temp_f:.1f}°F / {_c(ens.mean_temp_f):.1f}°C"),
            ("Std Avvik:", f"{ens.std_temp_f:.2f}°F"),
            ("Median:", f"{ens.median_temp_f:.1f}°F / {_c(ens.median_temp_f):.1f}°C"),
            ("P5–P95:", f"{ens.p05_temp_f:.1f}°F – {ens.p95_temp_f:.1f}°F"),
            ("P10–P90:", f"{ens.p10_temp_f:.1f}°F – {ens.p90_temp_f:.1f}°F"),
            ("Konfidens:", f"{ens.confidence:.3f}"),
        ]
        for label_text, value_text in rows:
            row_frame = ttk.Frame(summary_frame)
            row_frame.pack(fill=X, pady=1)
            ttk.Label(row_frame, text=label_text, width=18, anchor=E).pack(side=LEFT, padx=(0, 5))
            ttk.Label(row_frame, text=value_text, font=("Consolas", 10)).pack(side=LEFT)

        # Local time row
        if local_time_str:
            lt_frame = ttk.Frame(summary_frame)
            lt_frame.pack(fill=X, pady=1)
            ttk.Label(lt_frame, text="Lokal tid:", width=18, anchor=E).pack(side=LEFT, padx=(0, 5))
            ttk.Label(lt_frame, text=local_time_str, font=("Consolas", 10)).pack(side=LEFT)

        # Peak temperature time row
        try:
            peak_h_start = getattr(loc, "peak_hour_start", 14)
            peak_h_end = getattr(loc, "peak_hour_end", 16)
            tz_short = "UTC"
            try:
                from zoneinfo import ZoneInfo
            except ImportError:
                from backports.zoneinfo import ZoneInfo  # type: ignore[no-redef]
            tz_short = datetime.now(ZoneInfo(loc.tz)).strftime("%Z")
        except Exception:
            tz_short = loc.tz
        peak_row = ttk.Frame(summary_frame)
        peak_row.pack(fill=X, pady=1)
        ttk.Label(peak_row, text="Forventet peak:", width=18, anchor=E).pack(side=LEFT, padx=(0, 5))
        peak_str = f"🌡️ {peak_h_start:02d}:00–{peak_h_end:02d}:00 {tz_short}"
        ttk.Label(peak_row, text=peak_str, font=("Consolas", 10)).pack(side=LEFT)

        # --- Model Weights ---
        if ens.weights_snapshot:
            wf = ttk.LabelFrame(self._analysis_text, text="📊 Modellvekter", padding=10)
            wf.pack(fill=X, pady=(0, 10))
            for model, w in sorted(ens.weights_snapshot.items(), key=lambda x: -x[1]):
                bar_len = int(w * 40)
                bar = "█" * bar_len + "░" * (40 - bar_len)
                row_frame = ttk.Frame(wf)
                row_frame.pack(fill=X, pady=1)
                ttk.Label(row_frame, text=f"{model:<20s}", font=("Consolas", 9), width=20).pack(
                    side=LEFT
                )
                ttk.Label(
                    row_frame,
                    text=f"{w:.3f}  {bar}",
                    font=("Consolas", 9),
                ).pack(side=LEFT)

        # --- Individual Models ---
        if ens.individual_models:
            imf = ttk.LabelFrame(self._analysis_text, text="🔬 Individuelle modeller", padding=10)
            imf.pack(fill=X, pady=(0, 10))
            for model, temp_f in ens.individual_models.items():
                diff = temp_f - ens.mean_temp_f
                sign = "+" if diff >= 0 else ""
                row_frame = ttk.Frame(imf)
                row_frame.pack(fill=X, pady=1)
                ttk.Label(row_frame, text=f"{model:<20s}", font=("Consolas", 9), width=20).pack(
                    side=LEFT
                )
                ttk.Label(
                    row_frame,
                    text=f"{temp_f:.1f}°F  ({sign}{diff:.1f}°F vs BMA)",
                    font=("Consolas", 9),
                ).pack(side=LEFT)

        self._status_label.configure(text=f"✅ Analyse fullført: {loc.name}")
        self._update_status_bar()

        # --- 🔴 LIVE PEAK STATUS — Fetch current temp + run peak detection ---
        self._peak_status_loc = loc
        self._peak_status_ens = ens
        self._peak_status_lead_days_val = self._analysis_lead_days

        async def _fetch_peak() -> Any:
            temp_data = await self._analyzer.get_current_temp(loc.lat, loc.lon, loc.tz)
            return temp_data

        self._async.submit(_fetch_peak(), self._on_peak_status_done)

    # -------------------------------------------------------------------
    # Peak Status Card (appended after analysis in single-city mode)
    # -------------------------------------------------------------------

    def _on_peak_status_done(self, result: Any) -> None:
        """Render the 🔴 LIVE PEAK STATUS card after fetching current temp."""
        loc = getattr(self, "_peak_status_loc", None)
        ens = getattr(self, "_peak_status_ens", None)
        lead_days = getattr(self, "_peak_status_lead_days_val", 1)
        if loc is None or ens is None:
            return

        if isinstance(result, Exception):
            # Silently skip peak card on error
            return

        temp_data = result
        if temp_data is None or temp_data.get("temp_c") is None:
            # No current data — skip
            return

        cur_temp = temp_data["temp_c"]
        cur_time = temp_data.get("time_local") or datetime.now()

        # Compute suggested temp (same logic as BMA analysis)
        mean_c = (ens.mean_temp_f - 32.0) * 5.0 / 9.0
        uhi = getattr(loc, "uhi_adjustment", 0.0)
        adj_mean = mean_c + uhi
        suggested_temp = float(int(round(adj_mean if uhi > 0 else mean_c)))

        # Build observation history for this city
        city_name = loc.name
        if city_name not in self._obs_history:
            self._obs_history[city_name] = []
        self._obs_history[city_name].append((cur_time, cur_temp))
        # Keep last 60 minutes
        cutoff = cur_time - timedelta(minutes=65)
        self._obs_history[city_name] = [
            (t, v) for t, v in self._obs_history[city_name] if t > cutoff
        ]

        # Update today_max from observed data
        prev_max = self._today_max.get(city_name)
        if prev_max is None or cur_temp > prev_max[0]:
            self._today_max[city_name] = (cur_temp, cur_time)

        # --- Fetch actual daily max from API ---
        api_max_c = None
        try:
            import asyncio as _asyncio_peak
            loop = _asyncio_peak.new_event_loop()
            _asyncio_peak.set_event_loop(loop)
            try:
                api_data = loop.run_until_complete(
                    self._analyzer.get_today_max(loc.lat, loc.lon, loc.tz)
                )
            finally:
                loop.close()
            if api_data and api_data.get("api_max_c") is not None:
                api_max_c = api_data["api_max_c"]
                self._api_daily_max[city_name] = api_max_c

                # Update today_max with API value if it's higher than what we've observed
                observed_max = self._today_max.get(city_name)
                if observed_max is None or api_max_c > observed_max[0]:
                    # Use API value as today_max — it's the authoritative source
                    self._today_max[city_name] = (api_max_c, cur_time)
        except Exception:
            pass

        # Local time
        try:
            local_now = datetime.now(ZoneInfo(loc.tz))
        except Exception:
            local_now = datetime.now()

        # Peak detection
        peak_start = getattr(loc, "peak_hour_start", 14)
        peak_end = getattr(loc, "peak_hour_end", 16)
        target_date = date.today() + timedelta(days=lead_days)

        peak_state = detect_peak_state(
            obs_history=self._obs_history.get(city_name, []),
            today_max=self._today_max.get(city_name),
            peak_hour_start=peak_start,
            peak_hour_end=peak_end,
            local_now=local_now,
            target_date=target_date,
            peak_confirmed=None,
            suggested_temp=suggested_temp,
        )

        # Compute live confidence
        live_conf, mins_since_max, mins_decline, alert_level, alert_msg = compute_live_confidence(
            obs_history=self._obs_history.get(city_name, []),
            today_max=self._today_max.get(city_name),
            peak_hour_start=peak_start,
            peak_hour_end=peak_end,
            local_now=local_now,
            suggested_temp=suggested_temp,
        )

        # --- Render peak card ---
        trend = peak_state.trend

        # Determine card border color from peak state
        border_color = peak_state.color_hex
        emoji = peak_state.emoji
        state_label = peak_state.state_label
        peak_msg = peak_state.message

        # Peak window info
        in_peak = peak_start <= local_now.hour < peak_end
        peak_window_str = f"{peak_start:02d}:00-{peak_end:02d}:00"
        now_str = local_now.strftime("%H:%M")

        # Today's max info — use authoritative max (higher of API or observed)
        tmax = self._today_max.get(city_name)
        tmax_val = tmax[0] if tmax else cur_temp
        tmax_time_str = tmax[1].strftime("%H:%M") if tmax and hasattr(tmax[1], "strftime") else "—"

        # Observed-only max for comparison display
        obs_max_val = None
        obs_max_time_str = "—"
        for (t, v) in self._obs_history.get(city_name, []):
            if obs_max_val is None or v > obs_max_val:
                obs_max_val = v
                if hasattr(t, "strftime"):
                    obs_max_time_str = t.strftime("%H:%M")

        # --- Build peak card in analysis canvas ---
        # Separator
        sep = tk.Frame(self._analysis_text, bg=border_color, height=2)
        sep.pack(fill=X, pady=(10, 4))

        # Card header
        card = tk.Frame(
            self._analysis_text,
            bg="#ffffff",
            highlightbackground=border_color,
            highlightthickness=1,
            padx=0,
            pady=0,
        )
        card.pack(fill=X, pady=(0, 6))

        # Left colored bar
        left_bar = tk.Frame(card, bg=border_color, width=4)
        left_bar.pack(side=LEFT, fill=Y)
        left_bar.pack_propagate(False)

        # Content
        inner = tk.Frame(card, bg="#ffffff", padx=10, pady=8)
        inner.pack(side=LEFT, fill=BOTH, expand=YES)

        # Header
        tk.Label(
            inner,
            text=f"🔴 LIVE PEAK STATUS — {city_name}",
            font=("Segoe UI", 11, "bold"),
            bg="#ffffff", fg="#1a1a1a",
        ).pack(anchor=W)

        tk.Label(
            inner,
            text="━" * 42,
            font=("Consolas", 8),
            bg="#ffffff", fg=border_color,
        ).pack(anchor=W, pady=(1, 3))

        # Temperature row
        temp_parts = [f"🌡️ Nåværende temp: {cur_temp:.1f}°C {trend}"]
        if trend == "↑":
            temp_parts[-1] += " (stigende)"
        elif trend == "↓":
            temp_parts[-1] += " (synkende)"
        else:
            temp_parts[-1] += " (stabil)"
        tk.Label(
            inner, text=temp_parts[0],
            font=("Consolas", 9, "bold"), bg="#ffffff", fg="#1a1a1a",
        ).pack(anchor=W)

        # Dagens maks — show both API and observed
        if api_max_c is not None:
            tk.Label(
                inner,
                text=f"📡 Faktisk dagsmaks (API): {api_max_c:.1f}°C",
                font=("Consolas", 9, "bold"), bg="#ffffff", fg="#0d47a1",
            ).pack(anchor=W)
        if obs_max_val is not None and (api_max_c is None or abs(obs_max_val - api_max_c) > 0.1):
            tk.Label(
                inner,
                text=f"📈 Vår observerte maks: {obs_max_val:.1f}°C (siden oppstart, kl {obs_max_time_str})",
                font=("Consolas", 9), bg="#ffffff", fg="#333",
            ).pack(anchor=W)
        else:
            tk.Label(
                inner,
                text=f"📈 Dagens maks: {tmax_val:.1f}°C (kl {tmax_time_str})",
                font=("Consolas", 9), bg="#ffffff", fg="#333",
            ).pack(anchor=W)

        # Time metrics
        if mins_since_max > 0:
            tk.Label(
                inner,
                text=f"⏱️ Siden siste rekord: {mins_since_max} min",
                font=("Consolas", 9), bg="#ffffff", fg="#555",
            ).pack(anchor=W)
        if mins_decline > 0:
            tk.Label(
                inner,
                text=f"📉 Synkende i: {mins_decline} min",
                font=("Consolas", 9), bg="#ffffff", fg="#555",
            ).pack(anchor=W)

        tk.Label(inner, text="", bg="#ffffff").pack()  # spacer

        # Live confidence bar
        conf_row = tk.Frame(inner, bg="#ffffff")
        conf_row.pack(fill=X)

        if live_conf >= 80:
            bar_color = "#c62828"; conf_emoji = "🔥"
        elif live_conf >= 60:
            bar_color = "#f57f17"; conf_emoji = "⚠️"
        elif live_conf >= 30:
            bar_color = "#f9a825"; conf_emoji = "👀"
        else:
            bar_color = "#9e9e9e"; conf_emoji = "⏳"

        bar_width = 25
        filled = int(bar_width * live_conf / 100)
        bar_text = "█" * filled + "░" * (bar_width - filled)

        tk.Label(
            conf_row, text="⚡ PEAK CONFIDENCE: ",
            font=("Segoe UI", 8, "bold"), bg="#ffffff", fg="#1a1a1a",
        ).pack(side=LEFT)
        tk.Label(
            conf_row, text=f"{bar_text} {conf_emoji} {live_conf:.0f}%",
            font=("Consolas", 8, "bold"), bg="#ffffff", fg=bar_color,
        ).pack(side=LEFT)

        # Status message
        tk.Label(
            inner,
            text=f"   Status: {peak_state.emoji} {state_label} — {peak_msg}",
            font=("Segoe UI", 8, "bold"), bg="#ffffff", fg=border_color,
            wraplength=700,
        ).pack(anchor=W, pady=(2, 0))

        # Dagspeak summary
        if peak_state.state in ("possible_peak", "confirmed", "peak_window"):
            if peak_state.state == "possible_peak":
                dagspeak_text = f"🎯 DAGSPEAK: Sannsynlig nådd ({live_conf:.0f}% konfidens)"
            elif peak_state.state == "confirmed":
                dagspeak_text = f"🎯 DAGSPEAK: Bekreftet — {tmax_val:.1f}°C"
            else:
                dagspeak_text = f"🎯 DAGSPEAK: I peak-vindu — kan fortsatt stige"
            dagspeak_text += f"   Forventet peak-vindu: {peak_window_str} | Nå: {now_str}"
            if in_peak:
                dagspeak_text += " (inne i vindu)"
            else:
                dagspeak_text += " (utenfor vindu)"
            tk.Label(
                inner,
                text=dagspeak_text,
                font=("Consolas", 8), bg="#ffffff", fg="#555",
                wraplength=700,
            ).pack(anchor=W, pady=(2, 0))

        # Clean up temp refs
        self._peak_status_loc = None
        self._peak_status_ens = None

    # ===================================================================
    # Bulk Analysis (Tab 3)
    # ===================================================================

    def _run_bulk_analysis(self) -> None:
        """Run BMA analysis on ALL locations, rank by confidence, show Top N."""
        locations = self._loc_mgr.locations
        if not locations:
            messagebox.showwarning(
                "Ingen lokasjoner",
                "Ingen lokasjoner å analysere. Legg til byer eller bruk 'Standard' knappen.",
            )
            return

        # Read city count from spinbox (default 5, range 1-51)
        try:
            count = int(self._bulk_count_var.get())
            count = max(1, min(51, count))
        except (ValueError, TypeError):
            count = 5

        # Read selected date
        date_sel = self._analysis_date_var.get().strip()
        lead_days = self._date_lead_map.get(date_sel)
        if lead_days is None:
            lead_days = 1

        self._bulk_btn.configure(state="disabled")
        self._bulk_count_spinbox.configure(state="disabled")
        self._bulk_progress["maximum"] = len(locations)
        self._bulk_progress["value"] = 0
        self._bulk_status.configure(text="Analyserer byer...")
        self._status_label.configure(text=f"⏳ Bulk analyse ({count} byer) kjører...")

        async def _do() -> Any:
            t_start = time.perf_counter()
            total = len(locations)
            city_results: list[dict[str, Any]] = []

            # Run all analyses in parallel batches
            sem = asyncio.Semaphore(6)  # max 6 concurrent

            async def _analyze_one(i: int, loc: Any) -> dict[str, Any] | None:
                async with sem:
                    analysis = await self._analyzer.analyze(loc, lead_days=lead_days)
                    self._last_analyses[i] = analysis

                    def _update_progress() -> None:
                        done = sum(1 for c in city_results if c is not None) + 1
                        self._bulk_progress["value"] = min(done, total)
                        self._bulk_status.configure(text=f"Analyserer [{done}/{total}] {loc.name}...")

                    self.root.after(0, _update_progress)

                    if analysis.error or analysis.ensemble is None:
                        return None

                    ens = analysis.ensemble

                    # Compute BMA confidence: model agreement + narrowness of P5-P95
                    total_models = max(1, ens.model_count)
                    mean_c = (ens.mean_temp_f - 32.0) * 5.0 / 9.0
                    p5_c = (ens.p05_temp_f - 32.0) * 5.0 / 9.0
                    p95_c = (ens.p95_temp_f - 32.0) * 5.0 / 9.0
                    range_c = p95_c - p5_c

                    # Find which bucket mean falls in and count agreeing models
                    models_in_best = 0
                    best_bucket_label = ""
                    if ens.individual_models:
                        for model_temp_f in ens.individual_models.values():
                            model_c = (model_temp_f - 32.0) * 5.0 / 9.0
                            if p5_c - 2 <= model_c <= p95_c + 2:
                                models_in_best += 1

                    # Confidence = ensemble.confidence blended with model agreement
                    agree_ratio = models_in_best / total_models if total_models > 0 else 0
                    narrow_bonus = 1.0 / (1.0 + max(0, range_c / 8.0))
                    conf = ens.confidence * (0.4 + 0.6 * agree_ratio) * min(1.0, 1.0 + narrow_bonus * 0.3)
                    conf = min(0.99, max(0.05, conf))

                    return {
                        "city": loc.name,
                        "lat": loc.lat,
                        "lon": loc.lon,
                        "mean_c": round(mean_c, 1),
                        "p5_c": round(p5_c, 1),
                        "p95_c": round(p95_c, 1),
                        "range_c": round(range_c, 2),
                        "confidence": round(conf, 3),
                        "conf_pct": round(conf * 100, 0),
                        "models_agree": models_in_best,
                        "total_models": total_models,
                        "model_count": ens.model_count,
                    }

            tasks = [_analyze_one(i, loc) for i, loc in enumerate(locations)]
            raw_results = await asyncio.gather(*tasks)
            city_results = [r for r in raw_results if r is not None]

            # Rank by confidence descending
            city_results.sort(key=lambda x: x["confidence"], reverse=True)
            elapsed = time.perf_counter() - t_start
            return {"city_results": city_results, "total": total, "elapsed": elapsed, "count": count}

        self._async.submit(_do(), self._on_bulk_done)

    def _on_bulk_done(self, result: Any) -> None:
        """Render bulk analysis Top N results (confidence-based). Compact 3-line cards."""
        self._bulk_btn.configure(state="normal")
        self._bulk_count_spinbox.configure(state="normal")
        self._bulk_progress["value"] = 0
        self._bulk_status.configure(text="")

        if isinstance(result, Exception):
            messagebox.showerror("Bulk analyse feilet", str(result))
            self._update_status_bar()
            return

        if isinstance(result, dict) and "error" in result:
            messagebox.showerror("Bulk analyse feilet", result["error"])
            self._update_status_bar()
            return

        city_results = result.get("city_results", []) if isinstance(result, dict) else []
        total = result.get("total", 0) if isinstance(result, dict) else 0
        elapsed = result.get("elapsed", 0) if isinstance(result, dict) else 0
        count = result.get("count", 5) if isinstance(result, dict) else 5

        # Clear previous results in analysis canvas
        for widget in self._analysis_text.winfo_children():
            widget.destroy()

        if not city_results:
            ttk.Label(
                self._analysis_text,
                text="ℹ️ Ingen byer kunne analyseres.\nSjekk at Open-Meteo er tilgjengelig.",
                wraplength=700,
            ).pack(anchor=W, pady=10)
            self._status_label.configure(text="✅ Bulk analyse fullført — ingen treff")
            self._update_status_bar()
            return

        topn = city_results[:count]
        medals = ["🥇", "🥈", "🥉"] + ["⭐"] * max(0, count - 3)

        # Store top N for monitoring + suggested temps
        self._monitored_cities = topn

        # Build location lookup for UHI/station info
        loc_lookup: dict[str, Any] = {}
        for sl in self._loc_mgr.locations:
            loc_lookup[sl.name] = sl

        for c in topn:
            loc = loc_lookup.get(c["city"])
            uhi = getattr(loc, "uhi_adjustment", 0.0) if loc else 0.0
            adj_mean = c["mean_c"] + uhi
            suggested_temp = int(round(adj_mean if uhi > 0 else c["mean_c"]))
            self._suggested_temps[c["city"]] = float(suggested_temp)

        # Header
        header_frame = tk.Frame(
            self._analysis_text,
            bg="#1a237e",
            padx=12,
            pady=10,
        )
        header_frame.pack(fill=X, pady=(0, 8))

        header_text = f"🏆 TOP {count} — HØYEST KONFIDENS"
        tk.Label(
            header_frame,
            text=header_text,
            font=("Segoe UI", 14, "bold"),
            bg="#1a237e",
            fg="#ffffff",
        ).pack(anchor=W)

        tk.Label(
            header_frame,
            text=f"Analyserte {total} byer på {elapsed:.1f}s — viser de {count} med høyest konfidens",
            font=("Segoe UI", 9),
            bg="#1a237e",
            fg="#bbdefb",
        ).pack(anchor=W)

        # -- "Overvåk alle N" button --
        mon_btn_frame = tk.Frame(header_frame, bg="#1a237e")
        mon_btn_frame.pack(anchor=E, pady=(5, 0))
        mon_btn_text = f"🔔 Overvåk alle {min(count, len(topn))}"
        tk.Button(
            mon_btn_frame,
            text=mon_btn_text,
            font=("Segoe UI", 9, "bold"),
            bg="#ff9800",
            fg="#fff",
            command=self._start_monitoring_top5,
        ).pack(side=RIGHT)

        # ---- Correlation warnings ----
        top5_names = [c["city"] for c in topn]
        corr_warnings = self._check_correlation_warnings(top5_names)
        if corr_warnings:
            corr_frame = tk.Frame(self._analysis_text, bg="#fff3e0",
                                  highlightbackground="#E65100", highlightthickness=1,
                                  padx=10, pady=6)
            corr_frame.pack(fill=X, pady=(0, 8))
            for w in corr_warnings:
                tk.Label(corr_frame, text=w, font=("Segoe UI", 8, "bold"),
                         bg="#fff3e0", fg="#E65100", justify=LEFT).pack(anchor=W)

        # -- Compute canvas wrap width --
        try:
            _wrap_w = max(350, self._analysis_canvas.winfo_width() - 50)
        except Exception:
            _wrap_w = 700

        # Track daily max labels for async update
        daily_max_labels: dict[str, tk.Label] = {}

        # ---- Compact 3-line cards ----
        for rank, c in enumerate(topn):
            medal = medals[rank]
            conf_pct = c["conf_pct"]
            mean_c = c["mean_c"]
            p5_c = c["p5_c"]
            p95_c = c["p95_c"]
            range_c = c["range_c"]
            models_str = f"{c['models_agree']}/{c['total_models']}"
            city_name = c["city"]

            # Get UHI + station info
            loc = loc_lookup.get(city_name)
            uhi = getattr(loc, "uhi_adjustment", 0.0) if loc else 0.0
            station = getattr(loc, "station", "") if loc else ""
            elev = getattr(loc, "station_elevation_m", 0.0) if loc else 0.0
            adj_mean = mean_c + uhi
            suggested_temp = int(round(adj_mean if uhi > 0 else mean_c))

            # Border color based on confidence
            if conf_pct >= 85:
                border_color = "#2e7d32"
                signal_icon = "✅"
                signal_label = "SIKKER"
            elif conf_pct >= 70:
                border_color = "#f57f17"
                signal_icon = "🟡"
                signal_label = "MODERAT"
            else:
                border_color = "#c62828"
                signal_icon = "🔴"
                signal_label = "USIKKER"

            acc = self._get_accent_text_color(border_color)
            card_bg = "#ffffff"

            card = tk.Frame(
                self._analysis_text,
                bg=card_bg,
                highlightbackground=border_color,
                highlightthickness=1,
                padx=10,
                pady=5,
            )
            card.pack(fill=X, pady=(0, 4))

            # LEFT COLORED BORDER (4px indicator)
            left_bar = tk.Frame(card, bg=border_color, width=4)
            left_bar.pack(side=LEFT, fill=Y)
            left_bar.pack_propagate(False)

            # Right content area
            content = tk.Frame(card, bg=card_bg)
            content.pack(side=LEFT, fill=BOTH, expand=YES)

            # ── ROW 1: Rank + City + Confidence ──
            row1 = tk.Frame(content, bg=card_bg)
            row1.pack(fill=X)

            tk.Label(
                row1,
                text=f"#{rank+1} {medal} {city_name}",
                font=("Segoe UI", 10, "bold"),
                bg=card_bg,
                fg="#1a1a1a",
            ).pack(side=LEFT)

            tk.Label(
                row1,
                text=f"Konfidens: {conf_pct:.0f}%",
                font=("Segoe UI", 10, "bold"),
                bg=card_bg,
                fg=acc,
            ).pack(side=RIGHT)

            # ── ROW 2: BMA stats + suggested temp (compact 1 line) ──
            row2 = tk.Frame(content, bg=card_bg)
            row2.pack(fill=X, pady=(2, 0))

            bma_line = f"🌡️ BMA: {mean_c:.1f}°C  |  P5-P95: {p5_c:.1f}-{p95_c:.1f}°C  |  Range: {range_c:.1f}°C"
            if uhi > 0:
                bma_line += f"  |  +{uhi:.1f}°C UHI = {adj_mean:.1f}°C justert"

            tk.Label(
                row2,
                text=bma_line,
                font=("Consolas", 8),
                bg=card_bg,
                fg="#1a1a1a",
                anchor=W,
                wraplength=_wrap_w,
                justify=LEFT,
            ).pack(fill=X)

            # 🎯 Spill suggestion (compact, same row2 area)
            spill_sub = tk.Frame(content, bg=card_bg)
            spill_sub.pack(fill=X)
            spill_line = f"🎯 Spill: {suggested_temp}°C"
            if uhi > 0:
                spill_line += f" (UHI-justert fra {mean_c:.1f}°C)"
            else:
                spill_line += f" (BMA snitt → nærmest {suggested_temp}°C)"
            tk.Label(
                spill_sub,
                text=spill_line,
                font=("Consolas", 8),
                bg=card_bg,
                fg="#e65100",
                anchor=W,
                wraplength=_wrap_w,
                justify=LEFT,
            ).pack(fill=X)

            # ── ROW 3: Signal + Kelly + station + local time (compact 1 line) ──
            row3 = tk.Frame(content, bg=card_bg)
            row3.pack(fill=X, pady=(1, 0))

            signal_line = f"{signal_icon} {signal_label} — {models_str} modeller"

            # Kelly
            win_prob = conf_pct / 100.0
            kelly_pct, kelly_txt = self._compute_kelly(win_prob)
            if kelly_pct > 0:
                kelly_short = kelly_txt.replace("\n", " | ")
            else:
                if conf_pct >= 85:
                    kelly_short = "Kelly: 2-5% bankroll"
                elif conf_pct >= 70:
                    kelly_short = "Kelly: 1-3% bankroll"
                else:
                    kelly_short = "Kelly: ingen handel"

            # Local time
            tz_str = "UTC"
            for sl in self._loc_mgr.locations:
                if sl.name == city_name:
                    tz_str = sl.tz
                    break
            try:
                local_now = datetime.now(ZoneInfo(tz_str))
                local_str = f"🕐 {local_now.strftime('%H:%M %Z')}"
            except Exception:
                local_str = "🕐 UTC"

            # Station
            station_str = ""
            if station:
                station_str = f"📡 {station}"
                if elev:
                    station_str += f" ({elev:.0f}m)"

            meta_parts = [signal_line, kelly_short]
            if station_str:
                meta_parts.append(station_str)
            meta_parts.append(local_str)
            meta_line = "  |  ".join(meta_parts)

            tk.Label(
                row3,
                text=meta_line,
                font=("Consolas", 7),
                bg=card_bg,
                fg="#555",
                anchor=W,
                wraplength=_wrap_w,
                justify=LEFT,
            ).pack(fill=X)

            # ── ROW 4: Daily max placeholder (filled async) ──
            row4 = tk.Frame(content, bg=card_bg)
            row4.pack(fill=X, pady=(1, 0))
            daily_lbl = tk.Label(
                row4,
                text="📡 Dagsmaks: Laster...",
                font=("Consolas", 8, "bold"),
                bg=card_bg,
                fg="#1565c0",
                anchor=W,
            )
            daily_lbl.pack(fill=X)
            daily_max_labels[city_name] = daily_lbl

        # Footer disclaimer
        tk.Label(
            self._analysis_text,
            text=f"⚠️ Analyse fullført på {elapsed:.1f}s | Basert på BMA ensemble-konfidens — sjekk motpart før handel!",
            font=("Segoe UI", 8, "italic"),
            fg="#555",
        ).pack(anchor=W, pady=(8, 0))

        self._status_label.configure(text=f"✅ Bulk analyse fullført — {len(city_results)} byer, topp konfidens: {topn[0]['conf_pct']:.0f}%")
        self._update_status_bar()

        # -- Async fetch daily max for top N --
        self._fetch_daily_maxes(topn, daily_max_labels, loc_lookup)

    def _fetch_daily_maxes(
        self,
        top5: list[dict[str, Any]],
        daily_max_labels: dict[str, tk.Label],
        loc_lookup: dict[str, Any],
    ) -> None:
        """Async fetch today's observed max temp for top 5 cities, update labels."""

        async def _do() -> dict[str, Any]:
            results: dict[str, Any] = {}
            for c in top5:
                city_name = c["city"]
                loc = loc_lookup.get(city_name)
                if loc is None:
                    results[city_name] = None
                    continue
                tz = getattr(loc, "tz", "UTC")
                try:
                    daily = await self._analyzer.get_today_max(loc.lat, loc.lon, tz)
                except Exception:
                    daily = None
                results[city_name] = daily
            return results

        def _on_done(result: Any) -> None:
            if isinstance(result, Exception):
                return
            for city_name, daily in result.items():
                lbl = daily_max_labels.get(city_name)
                if lbl is None or not lbl.winfo_exists():
                    continue
                if daily and daily.get("max_c") is not None:
                    max_c = daily["max_c"]
                    peak_time = daily.get("peak_time", "")
                    time_str = ""
                    if peak_time:
                        try:
                            dt = datetime.fromisoformat(str(peak_time))
                            time_str = f" kl {dt.strftime('%H:%M')}"
                        except Exception:
                            pass
                    lbl.configure(
                        text=f"📡 Dagsmaks: {max_c:.1f}°C{time_str}",
                        fg="#0d47a1",
                    )
                else:
                    lbl.configure(text="📡 Dagsmaks: —")

        self._async.submit(_do(), _on_done)

    # ===================================================================
    # Helpers
    # ===================================================================

    def _set_ui_state(self, enabled: bool) -> None:
        """Enable or disable all interactive widgets during async operations."""
        state = "normal" if enabled else "disabled"
        # We don't need to disable everything — just key buttons are managed per-operation
        pass

    # -------------------------------------------------------------------
    # Date Selection Helpers
    # -------------------------------------------------------------------

    def _populate_date_options(self) -> None:
        """Generate the next 7 days for date dropdowns."""
        norwegian_days = [
            "mandag", "tirsdag", "onsdag", "torsdag",
            "fredag", "lørdag", "søndag",
        ]
        today = date.today()
        options: list[str] = []
        lead_map: dict[str, int] = {}
        for i in range(7):
            d = today + timedelta(days=i)
            day_name = norwegian_days[d.weekday()]
            label = f"{d.isoformat()} ({day_name})"
            options.append(label)
            lead_map[label] = i  # 0=today, 1=tomorrow, ...
        self._date_options = options
        self._date_lead_map = lead_map

    def _get_date_var_and_combo(
        self, parent: ttk.Frame, default_lead_days: int
    ) -> tuple[StringVar, ttk.Combobox]:
        """Create a date dropdown frame and return (var, combo)."""
        date_var = StringVar()
        combo = ttk.Combobox(
            parent,
            textvariable=date_var,
            state="readonly",
            width=28,
            values=self._date_options,
        )
        # Set default: tomorrow (index 1) or the closest match
        default_idx = min(default_lead_days, len(self._date_options) - 1)
        if self._date_options:
            combo.current(default_idx)
        return date_var, combo

    def _on_close(self) -> None:
        """Clean shutdown."""
        try:
            self._async.shutdown()
        except Exception:
            pass
        self.root.destroy()

    # ===================================================================
    # Peak Curve Window (PRI 4: 📈 Peak Kurve)
    # ===================================================================

    def _open_peak_curve_window(self) -> None:
        """Open the Peak Curve Toplevel window for live temperature graphing.

        Reuses data from the monitoring loop (``_obs_history``, ``_peak_state``,
        ``_suggested_temps``, ``_last_bma_result``) — no extra API calls.
        """
        if not self._monitored_cities:
            messagebox.showwarning(
                "Ingen data",
                "Kjor Bulk Analyse og start overvakning forst.",
            )
            return

        win = Toplevel(self.root)
        win.title("📈 Peak Kurve — Live Temperatur")
        win.geometry("900x650")
        win.minsize(700, 500)

        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - 900) // 2
        y = (sh - 650) // 2
        win.geometry(f"+{x}+{y}")

        # City selection frame
        sel_frame = ttk.LabelFrame(win, text="Velg byer (1-5)", padding=10)
        sel_frame.pack(fill=X, padx=10, pady=(10, 5))

        city_names = [c["city"] for c in self._monitored_cities[:5]]
        city_vars: dict[str, BooleanVar] = {}

        for i, name in enumerate(city_names):
            var = BooleanVar(value=(i == 0))
            city_vars[name] = var
            cb = ttk.Checkbutton(sel_frame, text=name, variable=var)
            cb.pack(side=LEFT, padx=8)

        # Graph canvas
        graph_frame = tk.Frame(win, bg="#1a1a2e")
        graph_frame.pack(fill=BOTH, expand=YES, padx=10, pady=5)

        canvas = tk.Canvas(graph_frame, bg="#1a1a2e", highlightthickness=0)
        canvas.pack(fill=BOTH, expand=YES)

        # Status bar
        status_frame = tk.Frame(win, bg="#0d0d1a", height=40)
        status_frame.pack(fill=X, side=BOTTOM)
        status_label = tk.Label(
            status_frame,
            text="🔄 Oppdaterer hvert 60. sekund...",
            font=("Consolas", 9),
            bg="#0d0d1a",
            fg="#888",
        )
        status_label.pack(side=LEFT, padx=10, pady=6)

        def _draw_graph() -> None:
            """Redraw the peak curve graph for selected cities."""
            canvas.delete("all")
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 50 or h < 50:
                return

            selected = [n for n, v in city_vars.items() if v.get()]
            if not selected:
                canvas.create_text(
                    w // 2, h // 2,
                    text="Velg minst en by for a se grafen",
                    fill="#666",
                    font=("Segoe UI", 12),
                )
                return

            n_cities = len(selected)
            section_h = h // n_cities
            colors = ["#4fc3f7", "#ff8a65", "#81c784", "#fff176", "#ce93d8"]

            for idx, city_name in enumerate(selected):
                y0 = idx * section_h
                y1 = y0 + section_h

                canvas.create_rectangle(0, y0, w, y1, fill="#16213e", outline="#0f3460", width=1)

                obs_list = self._obs_history.get(city_name, [])
                peak_state = self._peak_state.get(city_name)
                suggested_temp = self._suggested_temps.get(city_name)
                bma_result = self._last_bma_result.get(city_name, {})

                if not obs_list:
                    canvas.create_text(
                        w // 2, y0 + section_h // 2,
                        text=f"{city_name}: Ingen data enna...",
                        fill="#666",
                        font=("Segoe UI", 10),
                    )
                    continue

                temps = [t[1] for t in obs_list]
                if len(temps) < 2:
                    continue

                min_t = min(temps) - 2
                max_t = max(temps) + 2
                if suggested_temp is not None:
                    max_t = max(max_t, suggested_temp + 1)
                    min_t = min(min_t, suggested_temp - 1)

                t_range = max(max_t - min_t, 1.0)

                chart_x0 = 60
                chart_x1 = w - 30
                chart_y0 = y0 + 25
                chart_y1 = y1 - 10
                chart_w = chart_x1 - chart_x0
                chart_h = chart_y1 - chart_y0

                # Grid lines
                for i in range(5):
                    y_pos = chart_y0 + chart_h * i / 4
                    t_val = max_t - t_range * i / 4
                    canvas.create_line(chart_x0, y_pos, chart_x1, y_pos, fill="#2a2a4a", dash=(2, 4))
                    canvas.create_text(
                        chart_x0 - 5, y_pos,
                        text=f"{t_val:.1f}",
                        anchor="e",
                        fill="#888",
                        font=("Consolas", 7),
                    )

                # Temperature line
                n_pts = len(temps)
                points: list[float] = []
                for i in range(n_pts):
                    x = chart_x0 + chart_w * i / max(n_pts - 1, 1)
                    y = chart_y0 + chart_h * (1 - (temps[i] - min_t) / t_range)
                    points.extend([x, y])

                color = colors[idx % len(colors)]
                if len(points) >= 4:
                    canvas.create_line(*points, fill=color, width=2, smooth=True)
                    cx = chart_x0 + chart_w * (n_pts - 1) / max(n_pts - 1, 1)
                    cy = chart_y0 + chart_h * (1 - (temps[-1] - min_t) / t_range)
                    canvas.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=color, outline="white", width=2)

                # BMA predicted peak line
                if suggested_temp is not None:
                    sy = chart_y0 + chart_h * (1 - (suggested_temp - min_t) / t_range)
                    if chart_y0 <= sy <= chart_y1:
                        canvas.create_line(chart_x0, sy, chart_x1, sy, fill="#ffeb3b", dash=(8, 4), width=1)
                        canvas.create_text(
                            chart_x1 + 5, sy,
                            text=f"{suggested_temp:.0f}C",
                            anchor="w",
                            fill="#ffeb3b",
                            font=("Consolas", 7, "bold"),
                        )

                # City label + indicator
                if peak_state is not None:
                    live_conf = peak_state.live_confidence
                    trend = peak_state.trend
                    state_label = peak_state.state_label
                    peak_emoji = peak_state.emoji

                    if peak_state.state in ("confirmed", "completed"):
                        indicator = f"PEAK NADD ({state_label})"
                        ind_color = "#ff5252"
                    elif live_conf >= 60:
                        indicator = f"PEAK NADD? ({state_label} {live_conf:.0f}%)"
                        ind_color = "#ffab40"
                    elif trend == "↑":
                        indicator = "STIGER"
                        ind_color = "#69f0ae"
                    else:
                        indicator = "VENT"
                        ind_color = "#888"

                    city_label = f"{peak_emoji} {city_name}: {indicator}"
                else:
                    city_label = f"  {city_name}: VENTER"
                    ind_color = "#aaa"

                canvas.create_text(
                    chart_x0 + 5, y0 + 10,
                    text=city_label,
                    anchor="w",
                    fill=ind_color,
                    font=("Segoe UI", 9, "bold"),
                )

                cur_temp = temps[-1] if temps else 0
                bma_conf = bma_result.get("confidence", 0)
                info_text = f"{cur_temp:.1f}C | BMA: {bma_conf:.0%}"
                canvas.create_text(
                    w - 35, y0 + 10,
                    text=info_text,
                    anchor="e",
                    fill="#888",
                    font=("Consolas", 7),
                )

        def _refresh_loop() -> None:
            try:
                _draw_graph()
            except Exception:
                pass
            if win.winfo_exists():
                win.after(60000, _refresh_loop)

        _draw_graph()
        canvas.bind("<Configure>", lambda e: _draw_graph())
        win.after(60000, _refresh_loop)

        close_frame = tk.Frame(win, bg="#f5f5f5")
        close_frame.pack(fill=X, padx=10, pady=(0, 10))
        ttk.Button(close_frame, text="Lukk", command=win.destroy).pack(side=RIGHT)


# =============================================================================
# Entry Point
# =============================================================================


def main() -> None:
    root = Tk()
    _app = WeatherMonitorGUI(root)  # noqa: F841
    root.mainloop()


if __name__ == "__main__":
    main()
