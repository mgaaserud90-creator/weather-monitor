#!/usr/bin/env python3
"""
PnL / Edge Ledger — measurable edge for the Mean(round) strategy
=================================================================

Maintains a persistent ``_pnl_log.json`` ledger with one record per resolved
city/date bet (the Mean(round)-vs-Polymarket strategy). The ledger is built
idempotently from ``_model_quality_log.json``: every re-run upserts by
``(date, city)`` so no duplicate entries are ever created.

Per-bet record (mirrors PLAN_EDGE_AUTOPILOT.md P6 schema):

    date           — market date (YYYY-MM-DD)
    city           — full city key (e.g. "Taipei, TW")
    bucket         — native market bucket label (e.g. "36°C", "86-87°F")
    question_type  — "highest" / "lowest" / "unknown"
    unit           — "C" / "F"
    market_type    — "point" / "threshold"
    mean_spill     — Mean(round) bet temperature (°C integer)
    bma_prob       — model win probability (0-1, strategies.mean.win_prob)
    market_price   — implied probability of the bet bucket from _market_prices.json
                     (None when no matching live market is available)
    edge           — bma_prob − market_price (None when market_price is None)
    kelly          — quarter-Kelly recommended stake in USD (None when not computable)
    eligible       — True when edge >= EDGE_THRESHOLD and quarter-Kelly > 0
    stake          — flat paper stake in USD (PNL_STAKE_USD, default 100)
    result         — "WIN" / "LOSS" (from strategies.mean.pm_result)
    payout         — 1.0 on WIN else 0.0 (per-$1 profit multiple)
    pnl            — +stake on WIN, −stake on LOSS (even-money paper settlement)

Aggregate metrics computed from the ledger:
    total PnL, ROI (pnl / staked), average edge, ECE (10pp reliability bins),
    and Brier score.

USAGE:
    python _pnl_tracker.py            # rebuild ledger + print summary
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass

SCRIPT_DIR = Path(__file__).resolve().parent
PNL_LOG_FILE = SCRIPT_DIR / "_pnl_log.json"
QUALITY_LOG_FILE = SCRIPT_DIR / "_model_quality_log.json"

# ── Tunables (env-overridable, matching the plan's configurable threshold) ──
DEFAULT_STAKE_USD = float(os.environ.get("PNL_STAKE_USD", "100.0"))
EDGE_THRESHOLD = float(os.environ.get("EDGE_THRESHOLD", "0.05"))
KELLY_FRACTION = 0.25  # quarter-Kelly, conservative


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------

def load_ledger() -> dict:
    """Load the PnL ledger, returning an empty structure when absent/corrupt."""
    if PNL_LOG_FILE.exists():
        try:
            data = json.loads(PNL_LOG_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("records"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"records": [], "updated_at": None}


def save_ledger(data: dict) -> None:
    """Persist the ledger atomically-ish (write-then-nothing: plain write is fine here)."""
    data["updated_at"] = _now_iso()
    PNL_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    PNL_LOG_FILE.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def upsert_bet(record: dict, ledger: dict | None = None) -> bool:
    """Insert or update a bet record keyed by (date, city). Never duplicates.

    Returns True when the record was newly inserted, False when updated.
    """
    if ledger is None:
        ledger = load_ledger()
    records = ledger.setdefault("records", [])
    key = (str(record.get("date", "")), str(record.get("city", "")))
    for i, rec in enumerate(records):
        if (str(rec.get("date", "")), str(rec.get("city", ""))) == key:
            records[i] = record
            return False
    records.append(record)
    return True


# ---------------------------------------------------------------------------
# Data sources (soft imports keep this module importable standalone)
# ---------------------------------------------------------------------------

def _load_market_resolved_details() -> dict:
    """Unit-aware resolved market details: {(city, date): market_info}."""
    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        return _load_market_resolved_details()
    except Exception:
        return {}


def _load_market_opportunities() -> list[dict]:
    """Parsed live market opportunities from _market_prices.json."""
    try:
        from _compute_market_edge import load_market_prices  # type: ignore
        opps, _ = load_market_prices()
        return opps
    except Exception:
        return []


def _compute_bma_prob(mean_c: float, std_c: float, temp: int, qtype: str = "exact") -> float:
    """Fallback bucket win-probability (0-1) when strategies.mean.win_prob is absent."""
    try:
        from _compute_market_edge import compute_bma_prob  # type: ignore
        return compute_bma_prob(mean_c, std_c, temp, qtype) / 100.0
    except Exception:
        if std_c <= 0:
            std_c = 1.0
        hi = 0.5 * (1.0 + math.erf((temp + 0.5 - mean_c) / (std_c * 1.4142135623730951)))
        lo = 0.5 * (1.0 + math.erf((temp - 0.5 - mean_c) / (std_c * 1.4142135623730951)))
        return max(0.0, min(1.0, hi - lo))


def _compute_kelly(bma_prob: float, market_price: float) -> float | None:
    """Quarter-Kelly recommended stake in USD, or None when not computable.

    Uses the standard Kelly formula f* = (p·b − q)/b with b = (1−price)/price.
    Returns None when the bet has no positive expectation (p ≤ price).
    """
    try:
        price = max(0.01, min(0.99, market_price))
        p = max(0.0, min(1.0, bma_prob))
        q = 1.0 - p
        b = (1.0 - price) / price
        if b <= 0:
            return None
        full = (p * b - q) / b
        if full <= 0:
            return None
        return round(full * KELLY_FRACTION * DEFAULT_STAKE_USD, 2)
    except Exception:
        return None


def _build_market_price_index() -> dict[tuple[str, str, int], tuple[float, str]]:
    """Index live market prices: {(city_base, date, temp): (yes_price, question_type)}.

    Prices are YES prices (0-1) of the exact-bucket market. Used only to attach
    a market_price to a resolved bet when a matching live market exists.
    """
    index: dict[tuple[str, str, int], tuple[float, str]] = {}
    for opp in _load_market_opportunities():
        city = str(opp.get("city", "")).strip()
        if not city or city.lower() == "unknown":
            continue
        base = city.lower().split(",")[0].strip()
        date_str = str(opp.get("date", ""))[:10]
        temp = opp.get("temp")
        if temp is None:
            continue
        try:
            temp_i = int(temp)
        except (TypeError, ValueError):
            continue
        price = opp.get("market_prob", 0)  # percentage points (0-100)
        qtype = str(opp.get("question_type", "unknown"))
        key = (base, date_str, temp_i)
        index.setdefault(key, (round(price / 100.0, 4), qtype))
        # Also index the no-parenthesis city form ("Seoul (Incheon)" -> "Seoul")
        import re
        base_no_paren = re.sub(r"\s*\(.*?\)\s*", "", base).strip()
        if base_no_paren and base_no_paren != base:
            index.setdefault((base_no_paren, date_str, temp_i), (round(price / 100.0, 4), qtype))
    return index


def _match_market_price(
    city: str, date_str: str, mean_spill: int, price_index: dict,
) -> tuple[float | None, str | None]:
    """Look up the implied market price for the bet bucket, if available."""
    base = city.lower().split(",")[0].strip()
    for candidate in (base, base.split("(")[0].strip()):
        hit = price_index.get((candidate, date_str, mean_spill))
        if hit is not None:
            return hit[0], hit[1]
    return None, None


def _build_record(
    city: str,
    pdata: dict,
    date_str: str,
    market_details: dict,
    price_index: dict,
) -> dict | None:
    """Build one ledger record for a resolved Mean(round) bet, or None."""
    mean = (pdata.get("strategies", {}) or {}).get("mean", {}) or {}
    result = mean.get("pm_result")
    if result not in ("WIN", "LOSS"):
        return None

    mean_spill = mean.get("spill")
    if mean_spill is None:
        return None
    try:
        mean_spill_i = int(mean_spill)
    except (TypeError, ValueError):
        return None

    bma_prob = mean.get("win_prob")
    if not isinstance(bma_prob, (int, float)):
        bma_prob = _compute_bma_prob(
            float(pdata.get("bma_mean", mean_spill_i)),
            float(pdata.get("bma_std", 1.0)),
            mean_spill_i,
        )
    bma_prob = max(0.0, min(1.0, float(bma_prob)))

    city_base = city.split(",")[0].strip()
    market_info = market_details.get((city, date_str)) or market_details.get((city_base, date_str)) or {}

    bucket = market_info.get("bucket") or f"{mean_spill_i}°C"
    unit = (market_info.get("unit") or "C").upper()
    market_type = market_info.get("type") or "point"

    market_price, qtype = _match_market_price(city, date_str, mean_spill_i, price_index)
    question_type = qtype or str(market_info.get("question_type") or "unknown")

    edge = round(bma_prob - market_price, 4) if market_price is not None else None

    kelly = _compute_kelly(bma_prob, market_price) if market_price is not None else None
    eligible = bool(
        edge is not None
        and edge >= EDGE_THRESHOLD
        and kelly is not None
        and kelly > 0
    )

    stake = DEFAULT_STAKE_USD
    payout = 1.0 if result == "WIN" else 0.0
    pnl = stake if result == "WIN" else -stake

    return {
        "date": date_str,
        "city": city,
        "bucket": bucket,
        "question_type": question_type,
        "unit": unit,
        "market_type": market_type,
        "mean_spill": mean_spill_i,
        "bma_prob": round(bma_prob, 4),
        "market_price": market_price,
        "edge": edge,
        "kelly": kelly,
        "eligible": eligible,
        "stake": round(stake, 2),
        "result": result,
        "payout": payout,
        "pnl": round(pnl, 2),
    }


def build_ledger_from_quality_log() -> list[dict]:
    """Rebuild (upsert) the ledger from every resolved Mean(round) bet in the
    quality log. Idempotent — re-running never duplicates records."""
    if not QUALITY_LOG_FILE.exists():
        return []
    try:
        log_data = json.loads(QUALITY_LOG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    market_details = _load_market_resolved_details()
    price_index = _build_market_price_index()

    ledger = load_ledger()
    inserted = 0
    updated = 0

    for run in log_data.get("runs", []):
        run_date = str(run.get("run_date") or "")
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            date_str = str(pdata.get("_target_date") or run_date or "")
            record = _build_record(city, pdata, date_str, market_details, price_index)
            if record is None:
                continue
            if upsert_bet(record, ledger):
                inserted += 1
            else:
                updated += 1

    save_ledger(ledger)
    print(f"[PnL] ledger upserted: {inserted} inserted, {updated} updated, "
          f"{len(ledger.get('records', []))} total records")
    return ledger.get("records", [])


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def compute_metrics(records: list[dict]) -> dict:
    """Compute cumulative PnL / ROI / average edge / ECE / Brier from the ledger."""
    n = len(records)
    wins = sum(1 for r in records if r.get("result") == "WIN")
    losses = n - wins

    total_stake = sum(float(r.get("stake") or 0) for r in records)
    total_pnl = sum(float(r.get("pnl") or 0) for r in records)
    roi = round(total_pnl / total_stake, 4) if total_stake else 0.0

    edges = [float(r["edge"]) for r in records if r.get("edge") is not None]
    avg_edge = round(sum(edges) / len(edges), 4) if edges else None

    # Brier: mean squared error of probability vs binary outcome.
    brier_pairs = [
        (float(r["bma_prob"]), 1.0 if r.get("result") == "WIN" else 0.0)
        for r in records
        if isinstance(r.get("bma_prob"), (int, float))
    ]
    brier = (
        round(sum((p - y) ** 2 for p, y in brier_pairs) / len(brier_pairs), 4)
        if brier_pairs else None
    )

    # ECE: 10 percentage-point reliability bins.
    bins = [{"sum_p": 0.0, "sum_y": 0.0, "n": 0} for _ in range(10)]
    for p, y in brier_pairs:
        idx = min(9, int(p * 10))
        bins[idx]["sum_p"] += p
        bins[idx]["sum_y"] += y
        bins[idx]["n"] += 1
    ece = 0.0
    for b in bins:
        if b["n"] > 0:
            ece += (b["n"] / len(brier_pairs)) * abs(
                b["sum_p"] / b["n"] - b["sum_y"] / b["n"]
            )
    ece = round(ece, 4) if brier_pairs else None

    return {
        "n_bets": n,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "total_stake": round(total_stake, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": roi,
        "avg_edge": avg_edge,
        "ece": ece,
        "brier": brier,
    }


def per_city_pnl(records: list[dict]) -> list[dict]:
    """Aggregate the ledger into a short per-city PnL table."""
    tally: dict[str, dict] = {}
    for r in records:
        city = r.get("city", "?")
        entry = tally.setdefault(city, {"bets": 0, "wins": 0, "losses": 0, "stake": 0.0, "pnl": 0.0})
        entry["bets"] += 1
        if r.get("result") == "WIN":
            entry["wins"] += 1
        else:
            entry["losses"] += 1
        entry["stake"] += float(r.get("stake") or 0)
        entry["pnl"] += float(r.get("pnl") or 0)
    rows = []
    for city, e in tally.items():
        roi = round(e["pnl"] / e["stake"], 4) if e["stake"] else 0.0
        rows.append({
            "city": city,
            "bets": e["bets"],
            "wins": e["wins"],
            "losses": e["losses"],
            "pnl": round(e["pnl"], 2),
            "roi": roi,
        })
    rows.sort(key=lambda d: (-d["pnl"], -d["wins"], d["city"]))
    return rows


def main() -> int:
    records = build_ledger_from_quality_log()
    metrics = compute_metrics(records)

    print("\n" + "═" * 60)
    print("   PnL / EDGE LEDGER — Mean(round) vs Polymarket")
    print("═" * 60)
    print(f"   Bets:      {metrics['n_bets']}  (W:{metrics['wins']} / L:{metrics['losses']})")
    print(f"   Win rate:  {metrics['win_rate'] * 100:.1f}%")
    print(f"   Staked:    ${metrics['total_stake']:.2f}")
    print(f"   Total PnL: ${metrics['total_pnl']:+.2f}")
    print(f"   ROI:       {metrics['roi'] * 100:+.2f}%")
    avg_edge = metrics["avg_edge"]
    print(f"   Avg edge:  {avg_edge * 100:+.2f}pp" if avg_edge is not None else "   Avg edge:  n/a")
    print(f"   ECE:       {metrics['ece'] if metrics['ece'] is not None else 'n/a'}")
    print(f"   Brier:     {metrics['brier'] if metrics['brier'] is not None else 'n/a'}")
    print(f"   Ledger:    {PNL_LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
