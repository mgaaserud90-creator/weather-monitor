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


def modified_latest_spills() -> dict[str, dict]:
    """Return {city: {"spill": int, "date": str}} from _modified_strategy records.

    ``_modified_strategy_log.json`` only stores per-city aggregates, so we
    recompute the deterministic per-(city,date) records via
    ``_modified_strategy.build_log()`` and take each city's latest spill.
    """
    try:
        import _modified_strategy as mod  # type: ignore
        log = mod.build_log()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] Could not compute modified-strategy spills: {exc}", file=sys.stderr)
        return {}
    out: dict[str, dict] = {}
    for rec in (log.get("records", []) or []):
        city = str(rec.get("city", "")).strip()
        date_str = str(rec.get("date", "")).strip()
        spill = rec.get("spill")
        if not city or spill is None:
            continue
        cur = out.get(city)
        if cur is None or date_str > cur["date"]:
            out[city] = {"spill": int(spill), "date": date_str}
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
    modified_spills = modified_latest_spills()

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
            ms = modified_spills.get(city)
            if ms:
                bucket = int(ms["spill"])
                bucket_date = ms["date"]
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

        # --- Edge + max stake ----------------------------------------------
        is_resolved = bool(market.get("is_resolved", False))
        row["p_bucket"] = round(p_bucket, 4)
        row["p_bucket_pct"] = round(p_bucket * 100, 1)
        row["price"] = round(price, 4)
        row["price_pct"] = round(price * 100, 1)
        row["prob_source"] = prob_source
        row["market_status"] = "resolved" if is_resolved else "open"

        edge_frac = p_bucket - price
        row["edge_frac"] = round(edge_frac, 4)
        row["edge"] = round(edge_frac * 100, 1)  # percentage points (project convention)

        tradeable = (not is_resolved) and edge_frac > 0
        row["is_tradeable"] = tradeable

        if tradeable:
            kelly_full = compute_kelly_fraction(p_bucket, price)
            stake = min(kelly_full * KELLY_FRACTION * BANKROLL, MAX_STAKE_CAP)
            row["max_stake_usd"] = round(stake, 2)
        else:
            row["max_stake_usd"] = 0.0

        if is_resolved:
            row["note"] = "Market resolved (price at extreme) — max stake 0 (not tradeable)"
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

    notes: list[str] = [STAKE_METHOD]
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
