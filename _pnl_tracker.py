#!/usr/bin/env python3
"""
PnL / Edge Ledger — measurable edge for the Mean(round) strategy
=================================================================

Maintains a persistent ``_pnl_log.json`` ledger with one record per resolved
city/date bet (the Mean(round)-vs-Polymarket strategy). The ledger is built
idempotently from ``_model_quality_log.json``: every re-run upserts by
``(date, city)`` so no duplicate entries are ever created.

Per-bet record (P0.2 price-aware settlement):

    date           — market date (YYYY-MM-DD)
    city           — full city key (e.g. "Taipei, TW")
    bucket         — native market bucket label (e.g. "36°C", "86-87°F")
    question_type  — "highest" / "lowest" / "unknown"
    unit           — "C" / "F"
    market_type    — "point" / "threshold"
    mean_spill     — Mean(round) bet temperature (°C integer)
    bma_prob       — model win probability (0-1); recomputed on the market's
                     NATIVE bucket width for °F markets (P3)
    bma_prob_source— "stored" / "native_f_width" / "fallback_1c"
    market_price   — price actually paid (YES ask), FROZEN on first write (P0.3)
    price_ts       — when that price was first observed (frozen with it)
    price_source   — where the price came from ("yes_price_snapshot" / "none")
    edge           — bma_prob − market_price (None when market_price is None)
    kelly          — quarter-Kelly recommended stake in USD (None when not computable)
    eligible       — True when edge >= EDGE_THRESHOLD and quarter-Kelly > 0
    stake          — flat paper stake in USD (PNL_STAKE_USD, default 100)
    result         — "WIN" / "LOSS", derived from the resolution ONLY
    settle_rule    — "point_c" / "point_f" / "threshold_high" / "threshold_low"
    tradable       — False when the price is missing or degenerate
                     (p <= 0.001 or p >= 0.999); such a bet is NOT settled
    payout         — price-dependent per-$1 multiple: WIN → 1/p − 1, LOSS → −1
    pnl            — WIN → stake × (1/p − 1); LOSS → −stake

The old even-money arithmetic (`payout = 1.0 if WIN`, `pnl = ±stake`) is gone:
it made ROI degenerate to `2 × hit_rate − 1` regardless of price, which is why
the ledger used to print a meaningless −36.25 %.

Aggregate metrics computed from the ledger:
    total PnL, ROI (pnl / staked, TRADABLE records only), average edge, ECE
    (10pp reliability bins), and Brier score.

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

# P0.2 — a quote at or beyond these bounds is not a tradeable price. A 0.1 ¢
# YES is not "nearly a certain win" and a 99.9 ¢ YES is not "free money"; both
# are the market saying the bucket is decided, and booking them as even-money
# wins is exactly the defect this module used to have.
MIN_TRADEABLE_PRICE = 0.001
MAX_TRADEABLE_PRICE = 0.999

# P0.3 — price fields are frozen on first write. A rebuild may only fill a
# NULL; it may never overwrite a non-null price, because the price captured on
# the bet day is the only historical price that will ever exist.
FREEZE_PRICE_FIELDS: tuple[str, ...] = (
    "market_price", "price_ts", "price_source", "edge", "kelly", "eligible",
)


# F1: shared stat-keyed JSON cache (_json_io). The ledger rebuild and the
# quality-log parse below are the two heaviest reads in this script. Parsed
# object and on-disk schema are unchanged — I/O optimisation only.
try:
    from _json_io import load as _json_load
except ModuleNotFoundError:  # pragma: no cover - script copied without the helper

    def _json_load(path, **kwargs):  # type: ignore[misc]
        return json.loads(Path(path).read_text(encoding="utf-8"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Ledger persistence
# ---------------------------------------------------------------------------

def load_ledger() -> dict:
    """Load the PnL ledger, returning an empty structure when absent/corrupt."""
    if PNL_LOG_FILE.exists():
        try:
            data = _json_load(PNL_LOG_FILE)
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


def is_tradeable_price(price: Any) -> bool:
    """True when ``price`` is a usable YES price (strictly inside the book)."""
    if price is None or isinstance(price, bool):
        return False
    try:
        value = float(price)
    except (TypeError, ValueError):
        return False
    return MIN_TRADEABLE_PRICE < value < MAX_TRADEABLE_PRICE


def apply_settlement(record: dict) -> dict:
    """(Re)compute payout / pnl / tradable from the record's own price.

    The single implementation of the settlement rule, used both when a record
    is first built and when a frozen price is merged back over a rebuild:

        price p tradeable : WIN  -> payout = 1/p - 1, pnl = stake * (1/p - 1)
                            LOSS -> payout = -1,      pnl = -stake
        price missing/degenerate : tradable = False, payout = None, pnl = None

    ``result`` is never touched here: it comes from the resolution only.
    """
    record["tradable"] = is_tradeable_price(record.get("market_price"))
    if not record["tradable"]:
        record["payout"] = None
        record["pnl"] = None
        return record
    price = float(record["market_price"])
    stake = float(record.get("stake") or 0.0)
    if record.get("result") == "WIN":
        record["payout"] = round(1.0 / price - 1.0, 6)
        record["pnl"] = round(stake * (1.0 / price - 1.0), 6)
    else:
        record["payout"] = -1.0
        record["pnl"] = round(-stake, 6)
    return record


def _merge_frozen_prices(previous: dict, new: dict) -> dict:
    """Merge a rebuilt record over the stored one, freezing price fields (P0.3).

    A non-null stored value always wins; a rebuild may only fill a null. This is
    precisely what stops a rebuild from erasing the price captured on bet day.
    """
    merged = dict(new)
    for field in FREEZE_PRICE_FIELDS:
        if previous.get(field) is not None:
            merged[field] = previous[field]
    if merged.get("price_source") is None:
        merged["price_source"] = "none"
    return merged


def upsert_bet(record: dict, ledger: dict | None = None) -> bool:
    """Insert or update a bet record keyed by (date, city). Never duplicates.

    Updates are price-frozen: the stored price fields are kept and the
    settlement is then recomputed from the frozen price (P0.3 + P0.2).

    Returns True when the record was newly inserted, False when updated.
    """
    if ledger is None:
        ledger = load_ledger()
    records = ledger.setdefault("records", [])
    key = (str(record.get("date", "")), str(record.get("city", "")))
    for i, rec in enumerate(records):
        if (str(rec.get("date", "")), str(rec.get("city", ""))) == key:
            records[i] = apply_settlement(_merge_frozen_prices(rec, record))
            return False
    records.append(apply_settlement(record))
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


def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))


def _compute_bma_prob_native_f(
    mean_c: float, std_c: float, lo_f: float, hi_f: float
) -> float:
    """P(bucket) computed over the market's TRUE native °F width (P3).

    A °F bucket is 1 °F wide (0.556 °C), not 1 °C. Computing p on a 1 °C window
    overstates it on °F markets by roughly 1 °C / 0.556 °C ≈ 1.8×, which is the
    whole of the observed 13.3 pp °C/°F hit-rate gap.
    """
    sd_c = std_c if std_c and std_c > 0 else 1.0
    mu_f = mean_c * 9.0 / 5.0 + 32.0
    sd_f = max(1e-3, sd_c * 9.0 / 5.0)
    prob = _norm_cdf((float(hi_f) - mu_f) / sd_f) - _norm_cdf((float(lo_f) - mu_f) / sd_f)
    return max(1e-6, min(1.0 - 1e-6, prob))


def _settle_rule_for_market(market_info: dict) -> str:
    """Which settlement rule this market resolves under (stored per record)."""
    if market_info.get("type") == "threshold":
        direction = str(market_info.get("bucket") or "").lower()
        if any(kw in direction for kw in ("or below", "or lower", "≤")):
            return "threshold_low"
        return "threshold_high"
    if (market_info.get("unit") or "C").upper() == "F":
        return "point_f"
    return "point_c"


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
    """Build one ledger record for a resolved Mean(round) bet, or None.

    Settlement is price-aware (P0.2): the price is the one actually paid, the
    result comes from the resolution only, and `payout` is a price-dependent
    multiple rather than a 0/1 flag. See :func:`apply_settlement`.
    """
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

    bma_mean = float(pdata.get("bma_mean", mean_spill_i) or mean_spill_i)
    bma_std = float(pdata.get("bma_std", 1.0) or 1.0)

    city_base = city.split(",")[0].strip()
    market_info = market_details.get((city, date_str)) or market_details.get((city_base, date_str)) or {}

    bucket = market_info.get("bucket") or f"{mean_spill_i}°C"
    unit = (market_info.get("unit") or "C").upper()
    market_type = market_info.get("type") or "point"

    stored_prob = mean.get("win_prob")
    if isinstance(stored_prob, (int, float)):
        bma_prob = max(0.0, min(1.0, float(stored_prob)))
        prob_source = "stored"
    else:
        bma_prob = _compute_bma_prob(bma_mean, bma_std, mean_spill_i)
        prob_source = "fallback_1c"

    # P3 — °F buckets are 1 °F wide (0.556 °C). The stored win_prob was computed
    # on a 1 °C window, which overstates p on °F markets by ~1.8×. Recompute it
    # on the native width of the °F bucket that contains the bet.
    if unit == "F":
        spill_f = mean_spill_i * 9.0 / 5.0 + 32.0
        lo_f = float(math.floor(spill_f))
        hi_f = lo_f + 1.0
        bma_prob = _compute_bma_prob_native_f(bma_mean, bma_std, lo_f, hi_f)
        prob_source = "native_f_width"

    market_price_raw, qtype = _match_market_price(city, date_str, mean_spill_i, price_index)
    question_type = qtype or str(market_info.get("question_type") or "unknown")

    market_price = None
    price_source = "none"
    price_ts = None
    if market_price_raw is not None:
        market_price = round(float(market_price_raw), 4)
        price_source = "yes_price_snapshot"
        price_ts = _now_iso()

    edge = round(bma_prob - market_price, 4) if market_price is not None else None

    kelly = _compute_kelly(bma_prob, market_price) if market_price is not None else None
    eligible = bool(
        edge is not None
        and edge >= EDGE_THRESHOLD
        and kelly is not None
        and kelly > 0
    )

    record = {
        "date": date_str,
        "city": city,
        "bucket": bucket,
        "question_type": question_type,
        "unit": unit,
        "market_type": market_type,
        "mean_spill": mean_spill_i,
        "bma_prob": round(bma_prob, 4),
        "bma_prob_source": prob_source,
        "market_price": market_price,
        "price_ts": price_ts,
        "price_source": price_source,
        "edge": edge,
        "kelly": kelly,
        "eligible": eligible,
        "stake": round(DEFAULT_STAKE_USD, 2),
        "result": result,
        "settle_rule": _settle_rule_for_market(market_info),
        "tradable": False,
        "payout": None,
        "pnl": None,
    }
    return apply_settlement(record)


def build_ledger_from_quality_log() -> list[dict]:
    """Rebuild (upsert) the ledger from every resolved Mean(round) bet in the
    quality log. Idempotent — re-running never duplicates records."""
    if not QUALITY_LOG_FILE.exists():
        return []
    try:
        log_data = _json_load(QUALITY_LOG_FILE)
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

    # P0.2 — no record may keep the old even-money arithmetic. A handful of
    # legacy rows are no longer reproduced by the rebuild (their strategy lost
    # its pm_result), so they would otherwise sit in the file with pnl = ±stake
    # and no price, quietly poisoning ROI. Normalise them through the one
    # settlement implementation: no real price => untradeable, pnl None.
    normalised = 0
    for rec in ledger.get("records", []):
        if "tradable" not in rec or rec.get("price_source") is None:
            rec.setdefault("settle_rule", "unknown")
            rec.setdefault("price_source", "none")
            rec.setdefault("price_ts", None)
            rec.setdefault("tradable", False)
            rec.setdefault("payout", None)
            apply_settlement(rec)
            normalised += 1

    save_ledger(ledger)
    print(f"[PnL] ledger upserted: {inserted} inserted, {updated} updated, "
          f"{normalised} legacy records normalised, "
          f"{len(ledger.get('records', []))} total records")
    return ledger.get("records", [])


# ---------------------------------------------------------------------------
# Aggregate metrics
# ---------------------------------------------------------------------------

def compute_metrics(records: list[dict]) -> dict:
    """Compute cumulative PnL / ROI / average edge / ECE / Brier from the ledger.

    ROI and PnL are computed over TRADEABLE records only. Booking PnL for a bet
    whose price was never observed is exactly the even-money arithmetic this
    module used to do; a record without a real price is reported as untradeable
    and contributes to hit rate and Brier only.
    """
    n = len(records)
    wins = sum(1 for r in records if r.get("result") == "WIN")
    losses = n - wins

    tradeable = [r for r in records if r.get("pnl") is not None]

    total_stake = sum(float(r.get("stake") or 0) for r in tradeable)
    total_pnl = sum(float(r.get("pnl") or 0) for r in tradeable)
    roi = round(total_pnl / total_stake, 4) if total_stake else 0.0
    mean_return = (
        round(sum(float(r["pnl"]) / float(r["stake"]) for r in tradeable
                  if float(r.get("stake") or 0) > 0) / len(tradeable), 4)
        if tradeable else 0.0
    )

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
        "n_tradeable": len(tradeable),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / n, 4) if n else 0.0,
        "total_stake": round(total_stake, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": roi,
        "mean_return": mean_return,
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
    print(f"   Tradeable: {metrics['n_tradeable']} of {metrics['n_bets']} "
          f"(a price was actually observed and is inside 0.001 < p < 0.999)")
    print(f"   Staked:    ${metrics['total_stake']:.2f}  (tradeable only)")
    print(f"   Total PnL: ${metrics['total_pnl']:+.2f}  (tradeable only)")
    print(f"   ROI:       {metrics['roi'] * 100:+.2f}%  (price-aware, tradeable only)")
    print(f"   Mean ret:  {metrics['mean_return'] * 100:+.2f}%  (per-bet, tradeable only)")
    avg_edge = metrics["avg_edge"]
    print(f"   Avg edge:  {avg_edge * 100:+.2f}pp" if avg_edge is not None else "   Avg edge:  n/a")
    print(f"   ECE:       {metrics['ece'] if metrics['ece'] is not None else 'n/a'}")
    print(f"   Brier:     {metrics['brier'] if metrics['brier'] is not None else 'n/a'}")
    print(f"   Ledger:    {PNL_LOG_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
