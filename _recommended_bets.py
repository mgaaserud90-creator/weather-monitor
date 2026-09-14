#!/usr/bin/env python3
"""
Recommended Bets — "Anbefalt spill" engine (v1 Polymarket Weather).
==================================================================

Produces ``_recommended_bets.json``: a refreshable table of the highest-edge
temperature bets for today, one row per qualifying city.

Qualification rule
------------------
For each city we compute the historical win rate of all four strategies
(Sigma, P5, Mean, Modifisert):

  * Sigma / P5 / Mean  → ``_daily_city_log.json`` (per (city, date, strategy)
    WIN/LOSS rows, resolved against Polymarket).
  * Modifisert         → ``_modified_strategy_log.json`` (per-city aggregate
    wins / losses / bets).

The BEST strategy per city is the one with the highest historical win rate
among strategies with at least ``REC_BETS_MIN_SAMPLE`` (default 8) resolved
bets. A city is included while its best-strategy historical win rate is
>= 60% (it drops out again once the rate falls back below 60%).

Confidence layer (Modifisert-focused)
-------------------------------------
The raw Modifisert hit rate is only ~42% overall, but the local research
(``_modified_research.py``) shows the BMA bucket probability is well
calibrated and that hit rate rises with model confidence and differs strongly
by city. So every candidate bet now passes through ``_modified_confidence.py``:

  * ``p_final`` = Beta-Binomial shrinkage of the calibrated BMA bucket
    probability toward the chosen strategy's own resolved city record;
  * gates: city sample, Wilson lower bound, ``p_final``, BMA std, liquidity
    (24h volume), price sanity, minimum edge, and the market bucket must
    actually contain the spill (threshold markets need double edge);
  * only fully qualified, open markets are tradeable and are written to the
    forward ledger ``_modified_bets_log.json`` with the real entry price, so
    ROI becomes measurable as markets resolve (historical prices were never
    stored, so pre-ledger ROI cannot be reconstructed).

For each qualifying city we then determine TODAY's bet:

  * bucket   — the chosen strategy's spill for today
               (Sigma/P5/Mean from ``_daily_city_log.json``'s latest date;
                Modifisert from the latest ``_modified_strategy.py`` record).
  * P(bucket)— the strategy's stored win probability from
               ``_model_quality_log.json`` (Sigma/P5/Mean), or the BMA
               probability of the bucket (Modifisert / fallback).
  * price    — the Polymarket YES price for that bucket from
               ``_market_prices.json`` (matched via the existing
               ``_compute_market_edge`` market lookup / parsers).
  * EDGE     — the project's existing edge convention: P(bucket) − price
               (stored as both a fraction and percentage points).
  * MAX STAKE— order-book liquidity is NOT present in ``_market_prices.json``
               (only 24h ``volume``), so we fall back to fractional-Kelly
               (quarter-Kelly) capped by bankroll, and document the method.

Output
------
``_recommended_bets.json`` with ``generated_at`` and the full table, sorted
by edge descending. ``_anbefalt_spill.html`` renders it client-side and has a
🔄 Refresh button.

Usage
-----
    python _recommended_bets.py
    python _recommended_bets.py --json   # print JSON to stdout as well
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
DAILY_LOG_FILE = SCRIPT_DIR / "_daily_city_log.json"
MODIFIED_LOG_FILE = SCRIPT_DIR / "_modified_strategy_log.json"
QUALITY_LOG_FILE = SCRIPT_DIR / "_model_quality_log.json"
OUTPUT_FILE = SCRIPT_DIR / "_recommended_bets.json"

# ---------------------------------------------------------------------------
# Configuration (env-overridable)
# ---------------------------------------------------------------------------
STRATEGIES = ("sigma", "p5", "mean", "modifisert")
MIN_SAMPLE = int(os.environ.get("REC_BETS_MIN_SAMPLE", "8"))
QUALIFY_WIN_RATE = float(os.environ.get("REC_BETS_QUALIFY_WIN_RATE", "60.0"))

BANKROLL = float(os.environ.get("REC_BETS_BANKROLL", "1000"))
KELLY_FRACTION = float(os.environ.get("REC_BETS_KELLY_FRACTION", "0.25"))
MAX_STAKE_CAP = float(os.environ.get("REC_BETS_MAX_STAKE_CAP", "250"))

STAKE_METHOD = (
    "fractional_kelly_quarter_capped_by_bankroll "
    "(no order-book liquidity field in _market_prices.json; 24h volume shown for context)"
)

# ---------------------------------------------------------------------------
# Reuse the project's existing edge / market machinery.
# ---------------------------------------------------------------------------
from _compute_market_edge import (  # noqa: E402  (import after sys.path setup)
    load_market_prices,
    compute_bma_prob,
    compute_kelly_fraction,
    _f_bucket_bounds,
    _bucket_label,
    _normalize_base,
    is_us_city,
    c_to_f,
)

from _modified_confidence import (  # noqa: E402
    load_city_stats,
    evaluate as evaluate_confidence,
    update_ledger as update_bets_ledger,
    LEDGER_FILE as MODIFIED_BETS_LEDGER,
    MIN_CITY_SAMPLE as CONF_MIN_CITY_SAMPLE,
    MIN_CITY_WILSON_LB as CONF_MIN_CITY_WILSON_LB,
    MIN_P_FINAL as CONF_MIN_P_FINAL,
    MAX_BMA_STD as CONF_MAX_BMA_STD,
    MIN_EDGE as CONF_MIN_EDGE,
    MIN_VOLUME as CONF_MIN_VOLUME,
    MIN_PRICE as CONF_MIN_PRICE,
    MAX_PRICE as CONF_MAX_PRICE,
)


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_daily_city_log() -> list[dict]:
    data = _load_json(DAILY_LOG_FILE) or {}
    return data.get("rows", []) or []


def load_modified_cities() -> dict[str, dict]:
    data = _load_json(MODIFIED_LOG_FILE) or {}
    return data.get("cities", {}) or {}


def load_latest_run_predictions() -> dict[str, dict]:
    data = _load_json(QUALITY_LOG_FILE) or {}
    runs = data.get("runs", []) or []
    if not runs:
        return {}
    return runs[-1].get("predictions", {}) or {}


# ---------------------------------------------------------------------------
# Historical win-rate computation
# ---------------------------------------------------------------------------

def compute_historical_win_rates(
    daily_rows: list[dict],
    modified_cities: dict[str, dict],
) -> dict[str, dict[str, dict]]:
    """Return {city: {strategy: {"wins","losses","bets","win_rate"}}}.

    Sigma / P5 / Mean come from the daily city log; Modifisert comes from the
    modified strategy log's per-city aggregates.
    """
    rates: dict[str, dict[str, dict]] = {}

    for row in daily_rows:
        city = str(row.get("city", "")).strip()
        strat = str(row.get("strategy", "")).strip().lower()
        wl = str(row.get("win_loss", "")).strip().upper()
        if not city or strat not in ("sigma", "p5", "mean"):
            continue
        if wl not in ("WIN", "LOSS"):
            continue
        rec = rates.setdefault(city, {})
        stat = rec.setdefault(strat, {"wins": 0, "losses": 0})
        if wl == "WIN":
            stat["wins"] += 1
        else:
            stat["losses"] += 1

    for city, info in (modified_cities or {}).items():
        try:
            wins = int(info.get("wins", 0) or 0)
            losses = int(info.get("losses", 0) or 0)
        except (TypeError, ValueError):
            wins, losses = 0, 0
        rec = rates.setdefault(city, {})
        rec["modifisert"] = {"wins": wins, "losses": losses}

    for rec in rates.values():
        for stat in rec.values():
            bets = stat["wins"] + stat["losses"]
            stat["bets"] = bets
            stat["win_rate"] = round(stat["wins"] / bets * 100.0, 1) if bets else None

    return rates


def pick_best_strategy(rec: dict[str, dict]) -> tuple[str | None, float | None, dict | None]:
    """Pick the strategy with the highest win rate, requiring MIN_SAMPLE bets.

    Ties are broken by the canonical strategy order (sigma, p5, mean, modifisert).
    """
    best: str | None = None
    best_rate: float | None = None
    best_stats: dict | None = None
    for sn in STRATEGIES:
        stat = rec.get(sn)
        if not stat:
            continue
        bets = stat.get("bets", 0) or 0
        rate = stat.get("win_rate")
        if bets < MIN_SAMPLE or rate is None:
            continue
        if best_rate is None or rate > best_rate:
            best_rate = float(rate)
            best = sn
            best_stats = stat
    return best, best_rate, best_stats


# ---------------------------------------------------------------------------
# Today's spill lookup
# ---------------------------------------------------------------------------

def latest_daily_spills(daily_rows: list[dict]) -> tuple[dict[str, dict], str]:
    """Return ({city: {"sigma": spill, "p5": spill, "mean": spill, "date": d}}, latest_date)."""
    dates = sorted({str(r.get("date", "")) for r in daily_rows if r.get("date")})
    latest = dates[-1] if dates else ""
    spills: dict[str, dict] = {}
    for row in daily_rows:
        if str(row.get("date", "")) != latest:
            continue
        city = str(row.get("city", "")).strip()
        strat = str(row.get("strategy", "")).strip().lower()
        if strat not in ("sigma", "p5", "mean") or not city:
            continue
        entry = spills.setdefault(city, {"date": latest})
        entry[strat] = row.get("predicted_spill_c")
    return spills, latest


def modified_spills_by_date() -> dict[str, dict[str, int]]:
    """Return {city: {date: spill}} from the modified strategy records.

    ``_modified_strategy.build_log()`` computes the full daily series (per
    provider where available, BMA-mean fallback otherwise) and persists it, so
    the modified bucket can be aligned with any target date — in particular the
    daily-log ``spill_date`` the other strategies use.
    """
    try:
        import _modified_strategy as mod  # type: ignore
        log = mod.build_log()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not compute modified-strategy spills: {exc}", file=sys.stderr)
        return {}
    out: dict[str, dict[str, int]] = {}
    for rec in (log.get("records", []) or []):
        city = str(rec.get("city", "")).strip()
        date_str = str(rec.get("date", "")).strip()
        spill = rec.get("spill")
        if not city or not date_str or spill is None:
            continue
        out.setdefault(city, {})[date_str] = int(spill)
    return out


# ---------------------------------------------------------------------------
# Market matching (today's price per bucket)
# ---------------------------------------------------------------------------

def group_markets_by_city(market_opps: list[dict]) -> dict[str, list[dict]]:
    by_city: dict[str, list[dict]] = {}
    for opp in market_opps:
        base = _normalize_base(str(opp.get("city", "")))
        if base:
            by_city.setdefault(base, []).append(opp)
    return by_city


def _find_city_markets(city_base: str, by_city: dict[str, list[dict]]) -> list[dict]:
    opps = by_city.get(city_base)
    if opps:
        return opps
    for mbase, mlist in by_city.items():
        if city_base and (city_base in mbase or mbase in city_base):
            return mlist
    return []


def pick_market_for_spill(spill_c: int, city_base: str, by_city: dict[str, list[dict]]) -> dict | None:
    """Pick the Polymarket market bucket that corresponds to a strategy spill.

    Prefers the 'highest' temperature market type (the strategies forecast the
    daily max / peak). US cities trade native 1°F buckets; other cities trade
    exact °C point markets (with threshold markets as a fallback).
    """
    opps = _find_city_markets(city_base, by_city)
    if not opps:
        return None

    highest = [o for o in opps if o.get("question_type") == "highest"]
    pool = highest or list(opps)

    # US cities → native °F bucket containing the converted forecast.
    if is_us_city(city_base):
        f_val = c_to_f(float(spill_c))
        f_buckets = [o for o in pool if _f_bucket_bounds(o) is not None]
        if f_buckets:
            best = None
            best_dist = float("inf")
            for o in f_buckets:
                bounds = _f_bucket_bounds(o)
                if bounds is None:
                    continue
                lo, hi = bounds
                if lo <= f_val <= hi:
                    return o
                mid = (lo + hi) / 2.0
                dist = abs(mid - f_val)
                if dist < best_dist:
                    best_dist = dist
                    best = o
            if best is not None:
                return best

    # °C point markets → nearest integer bucket.
    exact = [o for o in pool if o.get("type") in ("exact", None)]
    if exact:
        target = int(round(spill_c))
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
        if best is not None:
            return best

    # Threshold markets (only when no exact bucket exists).
    threshold = [o for o in pool if o.get("type") in ("higher", "below")]
    if threshold:
        return threshold[0]

    # Last resort: nearest bucket in the full pool.
    target = int(round(spill_c))
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


# ---------------------------------------------------------------------------
# Probability helpers
# ---------------------------------------------------------------------------

def _market_contains_spill(market: dict, spill_c: int, city_base: str) -> bool:
    """True when the chosen market bucket actually contains the strategy spill."""
    bounds = _f_bucket_bounds(market)
    if bounds is not None:
        lo, hi = bounds
        return lo <= c_to_f(float(spill_c)) <= hi
    if market.get("type") in ("exact", None):
        temp = market.get("temp")
        if temp is None:
            return False
        try:
            return int(temp) == int(round(float(spill_c)))
        except (TypeError, ValueError):
            return False
    return False


def _bma_prob_for_market(bma_mean_c: float, bma_std_c: float, opp: dict) -> float:
    """BMA probability (0-1) for a market bucket, °F-bucket aware."""
    bounds = _f_bucket_bounds(opp)
    if bounds is not None:
        lo_f, hi_f = bounds
        f_mean = c_to_f(float(bma_mean_c))
        f_std = float(bma_std_c) * 9.0 / 5.0
        if f_std <= 0:
            f_std = 1.0
        import math
        def _cdf(x: float) -> float:
            return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))
        p = _cdf((hi_f + 0.5 - f_mean) / f_std) - _cdf((lo_f - 0.5 - f_mean) / f_std)
        return max(0.0, min(1.0, p))
    return compute_bma_prob(
        float(bma_mean_c),
        float(bma_std_c),
        int(opp.get("temp", 0) or 0),
        str(opp.get("type", "exact")),
    ) / 100.0


# ---------------------------------------------------------------------------
# Core: build the recommended-bets table
# ---------------------------------------------------------------------------

def compute_recommended_bets() -> dict:
    daily_rows = load_daily_city_log()
    modified_cities = load_modified_cities()
    quality_preds = load_latest_run_predictions()

    rates = compute_historical_win_rates(daily_rows, modified_cities)
    today_spills, spill_date = latest_daily_spills(daily_rows)
    modified_spills = modified_spills_by_date()
    city_confidence = load_city_stats()
    qualified_bets: list[dict] = []

    market_opps, fetched_at = load_market_prices()
    by_city = group_markets_by_city(market_opps)

    # Index quality-log predictions by base name for win-prob lookups.
    quality_by_base: dict[str, dict] = {}
    for city, pdata in (quality_preds or {}).items():
        base = _normalize_base(city)
        if base:
            quality_by_base.setdefault(base, pdata)

    rows: list[dict] = []
    missing_bucket: list[str] = []
    missing_price: list[str] = []

    for city in sorted(rates):
        rec = rates[city]
        best, best_rate, best_stats = pick_best_strategy(rec)
        if best is None or best_rate is None or best_rate < QUALIFY_WIN_RATE:
            continue

        city_base = _normalize_base(city)
        city_display = city.split(",")[0].strip()

        # --- Today's bucket for the chosen strategy -------------------------
        bucket: int | None = None
        bucket_date = spill_date
        bucket_note: str | None = None
        if best == "modifisert":
            city_map = modified_spills.get(city, {})
            if spill_date in city_map:
                bucket = int(city_map[spill_date])
                bucket_date = spill_date
            elif city_map:
                bucket_date = max(city_map)
                bucket = int(city_map[bucket_date])
                if bucket_date and bucket_date != spill_date:
                    bucket_note = (
                        f"Modifisert today spill unavailable — using latest available record ({bucket_date})"
                    )
        else:
            ts = today_spills.get(city)
            if ts:
                spill = ts.get(best)
                if isinstance(spill, (int, float)):
                    bucket = int(round(float(spill)))

        row: dict[str, Any] = {
            "city": city_display,
            "city_key": city,
            "strategy": best,
            "strategy_display": {
                "sigma": "Sigma (μ−kσ)",
                "p5": "P5",
                "mean": "Mean",
                "modifisert": "Modifisert",
            }.get(best, best),
            "historical": {
                "wins": (best_stats or {}).get("wins", 0),
                "losses": (best_stats or {}).get("losses", 0),
                "bets": (best_stats or {}).get("bets", 0),
                "win_rate_pct": best_rate,
            },
            "bucket": bucket,
            "bucket_date": bucket_date,
            "bucket_label": None,
            "p_bucket": None,
            "p_bucket_pct": None,
            "p_final": None,
            "p_final_pct": None,
            "bma_std": None,
            "city_n": None,
            "city_wr": None,
            "city_wilson_lb": None,
            "min_edge_required": None,
            "qualified": False,
            "exclusion_reasons": [],
            "edge_model_pp": None,
            "expected_value_per_dollar": None,
            "prob_source": None,
            "price": None,
            "price_pct": None,
            "edge": None,
            "edge_frac": None,
            "max_stake_usd": None,
            "volume": None,
            "volume_display": None,
            "question": None,
            "market_date": None,
            "market_status": None,
            "is_tradeable": False,
            "bucket_note": bucket_note,
            "note": None,
        }

        if bucket is None:
            missing_bucket.append(city_display)
            row["note"] = "No today bucket available for chosen strategy"
            rows.append(row)
            continue

        # --- Market / price ------------------------------------------------
        market = pick_market_for_spill(bucket, city_base, by_city)
        if market is None:
            missing_price.append(city_display)
            row["bucket_label"] = f"{bucket}°C"
            row["note"] = "No Polymarket market/price found for this bucket"
            rows.append(row)
            continue

        price = float(market.get("market_prob", 0)) / 100.0
        row["bucket_label"] = _bucket_label(market)
        row["question"] = market.get("question")
        row["market_date"] = market.get("date")
        row["volume"] = market.get("volume")
        row["volume_display"] = market.get("volume_display")

        # --- P(bucket) -----------------------------------------------------
        qp = quality_by_base.get(city_base, {})
        bma_mean = qp.get("bma_mean", 0) or 0
        bma_std = qp.get("bma_std", 1.0) or 1.0
        strategies = qp.get("strategies", {}) or {}

        p_bucket: float | None = None
        prob_source: str | None = None
        if best != "modifisert":
            stored_wp = (strategies.get(best, {}) or {}).get("win_prob")
            if isinstance(stored_wp, (int, float)):
                p_bucket = max(0.0, min(1.0, float(stored_wp)))
                prob_source = f"{best}.win_prob (stored)"
        if p_bucket is None:
            p_bucket = _bma_prob_for_market(bma_mean, bma_std, market)
            prob_source = "bma_prob (computed)"

        # --- Confidence-adjusted edge + max stake ---------------------------
        is_resolved = bool(market.get("is_resolved", False))
        market_type = str(market.get("type") or "")
        gate = evaluate_confidence(
            city_confidence, city, p_bucket, bma_std, market_type, price,
            wins=int((best_stats or {}).get("wins", 0) or 0),
            n=int((best_stats or {}).get("bets", 0) or 0),
            volume=float(market.get("volume") or 0.0),
        )
        if market_type != "threshold" and not _market_contains_spill(market, bucket, city_base):
            gate["qualified"] = False
            gate.setdefault("reasons", []).append("market bucket does not contain the spill")
        p_final = gate["p_final"]
        edge_frac = gate["edge_final"]

        row["p_bucket"] = round(p_bucket, 4)
        row["p_bucket_pct"] = round(p_bucket * 100, 1)
        row["p_final"] = p_final
        row["p_final_pct"] = round(p_final * 100, 1)
        row["price"] = round(price, 4)
        row["price_pct"] = round(price * 100, 1)
        row["prob_source"] = prob_source
        row["market_status"] = "resolved" if is_resolved else "open"
        row["bma_std"] = bma_std
        row["city_n"] = gate["city_n"]
        row["city_wr"] = gate["city_wr"]
        row["city_wilson_lb"] = gate["city_wilson_lb"]
        row["min_edge_required"] = gate["min_edge_required"]
        row["qualified"] = gate["qualified"]
        row["exclusion_reasons"] = gate["reasons"]
        row["edge_frac"] = round(edge_frac, 4)
        row["edge"] = round(edge_frac * 100, 1)  # percentage points (project convention)
        row["edge_model_pp"] = round((p_bucket - price) * 100, 1)
        row["expected_value_per_dollar"] = round(p_final / price - 1.0, 4) if price > 0 else None

        tradeable = (not is_resolved) and gate["qualified"]
        row["is_tradeable"] = tradeable

        if tradeable:
            kelly_full = compute_kelly_fraction(p_final, price)
            stake = min(kelly_full * KELLY_FRACTION * BANKROLL, MAX_STAKE_CAP)
            row["max_stake_usd"] = round(stake, 2)
            qualified_bets.append({
                "date": market.get("date") or spill_date,
                "city_key": city,
                "city": city_display,
                "strategy": best,
                "spill": bucket,
                "bucket_label": row.get("bucket_label"),
                "market_type": market_type,
                "price": round(price, 4),
                "p_final": p_final,
                "edge": round(edge_frac, 4),
                "stake": round(stake, 2),
                "logged_at": datetime.now(timezone.utc).isoformat(),
            })
        else:
            row["max_stake_usd"] = 0.0

        if is_resolved:
            row["note"] = "Market resolved (price at extreme) — max stake 0 (not tradeable)"
        elif gate["reasons"]:
            row["note"] = "Gated out: " + "; ".join(gate["reasons"])
        elif edge_frac <= 0:
            row["note"] = "No positive edge — max stake 0"

        rows.append(row)

    # Sort by edge (percentage points) descending; missing prices last.
    def _sort_key(r: dict) -> tuple:
        edge = r.get("edge")
        if edge is None:
            return (0, 0)
        return (1, float(edge))

    rows.sort(key=_sort_key, reverse=True)

    ledger_summary = update_bets_ledger(qualified_bets)

    notes: list[str] = [STAKE_METHOD]
    notes.append(
        "Confidence gates: city n>=" f"{CONF_MIN_CITY_SAMPLE}, Wilson LB>={CONF_MIN_CITY_WILSON_LB}, "
        f"p_final>={CONF_MIN_P_FINAL}, bma_std<={CONF_MAX_BMA_STD}, edge>={CONF_MIN_EDGE}, "
        f"volume>={CONF_MIN_VOLUME:.0f}, {CONF_MIN_PRICE:.2f}<=price<={CONF_MAX_PRICE:.2f} "
        "(threshold markets require double edge)."
    )
    notes.append(
        f"Forward bet ledger: {ledger_summary['n_resolved']} resolved "
        f"(hit_rate={ledger_summary['hit_rate']}, ROI={ledger_summary['roi']}); "
        "entry prices were never stored before the ledger started, so historical ROI is not reconstructable."
    )
    if fetched_at:
        notes.append(f"Market prices fetched at: {fetched_at}")
    else:
        notes.append("Market prices file missing or stale — edge/stake marked from latest available data.")
    if spill_date:
        notes.append(f"Strategy spill date (daily log latest): {spill_date}")
    if missing_bucket:
        notes.append(f"Cities missing a today bucket: {', '.join(missing_bucket)}")
    if missing_price:
        notes.append(f"Cities missing a Polymarket price: {', '.join(missing_price)}")

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qualification": {
            "min_sample": MIN_SAMPLE,
            "min_win_rate_pct": QUALIFY_WIN_RATE,
            "strategies": list(STRATEGIES),
            "rule": (
                "Include every city whose best-strategy historical win rate >= "
                f"{QUALIFY_WIN_RATE:.0f}% (min {MIN_SAMPLE} resolved bets); it stays "
                "in the list until the rate falls back below the threshold."
            ),
        },
        "stake_method": STAKE_METHOD,
        "spill_date": spill_date,
        "market_prices_fetched_at": fetched_at,
        "count": len(rows),
        "notes": notes,
        "confidence": {
            "gates": {
                "min_city_sample": CONF_MIN_CITY_SAMPLE,
                "min_city_wilson_lb": CONF_MIN_CITY_WILSON_LB,
                "min_p_final": CONF_MIN_P_FINAL,
                "max_bma_std": CONF_MAX_BMA_STD,
                "min_edge": CONF_MIN_EDGE,
            },
            "n_cities_with_confidence": len(city_confidence),
            "n_qualified_bets": len(qualified_bets),
            "ledger": ledger_summary,
            "method": (
                "p_final = Beta-Binomial shrinkage of the calibrated BMA bucket "
                "probability toward the city's own resolved Modifisert record; "
                "only bets passing all gates are tradeable and logged."
            ),
        },
        "modified_bets_ledger": MODIFIED_BETS_LEDGER.name,
        "recommended_bets": rows,
    }


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _fmt_pct(v: float | None) -> str:
    return "—" if v is None else f"{v:.1f}%"


def _fmt_usd(v: float | None) -> str:
    return "—" if v is None else f"${v:.2f}"


def format_table_text(rows: list[dict]) -> str:
    if not rows:
        return "No recommended bets (no city qualifies with >= 60% historical win rate)."
    lines = [
        "BY | STRATEGI | BØTTE | P(bøtte) | PRIS | EDGE | MAX STAKE",
        "---|---|---|---|---|---|---",
    ]
    for r in rows:
        edge = f"{r['edge']:+.1f}pp" if r.get("edge") is not None else "—"
        lines.append(
            f"{r['city']} | {r['strategy_display']} | {r.get('bucket_label') or '—'} | "
            f"{_fmt_pct(r.get('p_bucket_pct'))} | {_fmt_pct(r.get('price_pct'))} | "
            f"{edge} | {_fmt_usd(r.get('max_stake_usd'))}"
        )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Compute recommended bets (Anbefalt spill).")
    parser.add_argument("--json", action="store_true", help="Also print the JSON payload to stdout.")
    args = parser.parse_args()

    output = compute_recommended_bets()

    OUTPUT_FILE.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"[SAVED] {output['count']} recommended bets -> {OUTPUT_FILE}")

    print("\n" + format_table_text(output["recommended_bets"]))

    for note in output["notes"]:
        print(f"[NOTE] {note}")

    if args.json:
        print(json.dumps(output, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())
