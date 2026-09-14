#!/usr/bin/env python3
"""
Modifisert confidence layer + forward bet ledger.

Why this exists
---------------
The Modifisert strategy's raw hit rate is ~42% overall, but the local research
shows two things clearly (see ``_modified_research.py``):

  * the BMA-implied probability of the chosen bucket is well CALIBRATED
    (mean model probability ≈ realised hit rate across probability buckets);
  * hit rate rises monotonically with model confidence (low ``bma_std`` /
    high ``confidence``) and differs strongly per city
    (e.g. Jinan/Singapore/Ankara/New York >> Miami/Denver/San Francisco).

So instead of trusting a single raw number we combine
  1. the calibrated model probability ``p_model``,
  2. the city's own resolved track record (Beta-Binomial shrinkage), and
  3. explicit confidence gates (sample, Wilson lower bound, ``bma_std``, edge),
and we record every qualified pick in a forward ledger with the *entry price*
so real ROI can be measured as markets resolve. Historical market prices were
never stored, so historical ROI cannot be reconstructed — this ledger fixes
that going forward.

All thresholds are env-overridable.
"""

from __future__ import annotations

import json
import math
import os
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

MODIFIED_LOG_FILE = SCRIPT_DIR / "_modified_strategy_log.json"
LEDGER_FILE = SCRIPT_DIR / "_modified_bets_log.json"

# ── Gates / tunables (env-overridable) ──────────────────────────────────────
MIN_CITY_SAMPLE = int(os.environ.get("MOD_MIN_CITY_SAMPLE", "5"))
MIN_CITY_WILSON_LB = float(os.environ.get("MOD_MIN_CITY_WILSON_LB", "0.40"))
MIN_P_FINAL = float(os.environ.get("MOD_MIN_P", "0.40"))
MAX_BMA_STD = float(os.environ.get("MOD_MAX_BMA_STD", "1.0"))
MIN_EDGE = float(os.environ.get("MOD_MIN_EDGE", "0.05"))
SHRINK_K = float(os.environ.get("MOD_SHRINK_K", "5.0"))
THRESHOLD_EDGE_MULT = float(os.environ.get("MOD_THRESHOLD_EDGE_MULT", "2.0"))
MIN_VOLUME = float(os.environ.get("MOD_MIN_VOLUME", "5000"))
MIN_PRICE = float(os.environ.get("MOD_MIN_PRICE", "0.02"))
MAX_PRICE = float(os.environ.get("MOD_MAX_PRICE", "0.95"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def wilson_lb(wins: int, n: int, z: float = 1.96) -> float:
    """Lower bound of the 95% Wilson score interval for a binomial rate."""
    if n <= 0:
        return 0.0
    phat = wins / n
    denom = 1.0 + z * z / n
    center = phat + z * z / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


def _match_stats(stats: dict[str, dict], city: str) -> dict | None:
    if city in stats:
        return stats[city]
    base = city.split(",")[0].strip().lower()
    if not base:
        return None
    for key, value in stats.items():
        if key.split(",")[0].strip().lower() == base:
            return value
    for key, value in stats.items():
        kbase = key.split(",")[0].strip().lower()
        if kbase and (base in kbase or kbase in base):
            return value
    return None


def load_city_stats(path: Path = MODIFIED_LOG_FILE) -> dict[str, dict]:
    """Per-city resolved Modifisert record from ``_modified_strategy_log.json``."""
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return out
    for city, info in (data.get("cities", {}) or {}).items():
        try:
            wins = int(info.get("wins", 0) or 0)
            losses = int(info.get("losses", 0) or 0)
        except (TypeError, ValueError):
            wins, losses = 0, 0
        n = wins + losses
        out[city] = {
            "wins": wins,
            "losses": losses,
            "n": n,
            "wr": (wins / n) if n else 0.0,
            "wilson_lb": wilson_lb(wins, n),
        }
    return out


def shrunk_prob(p_model: float, wins: int, n: int, k: float = SHRINK_K) -> float:
    """Beta-Binomial posterior mean of the win probability.

    Prior Beta(k·p_model, k·(1−p_model)) pulls small samples toward the
    calibrated model probability and lets large samples dominate.
    """
    p_model = max(0.0, min(1.0, float(p_model)))
    if n <= 0:
        return p_model
    return (wins + k * p_model) / (n + k)


def evaluate(
    city_stats: dict[str, dict],
    city: str,
    p_model: float,
    bma_std: float | None,
    market_type: str,
    price: float,
    wins: int | None = None,
    n: int | None = None,
    volume: float | None = None,
) -> dict:
    """Return the confidence-adjusted probability, edge and gate decision.

    ``wins``/``n`` override the city lookup so each strategy is judged on its
    own resolved record (e.g. Modifisert vs Sigma for the same city).
    """
    if wins is None or n is None:
        stats = _match_stats(city_stats, city) or {"wins": 0, "n": 0, "wr": 0.0, "wilson_lb": 0.0}
        wins = int(stats.get("wins", 0))
        n = int(stats.get("n", 0))
    else:
        wins = int(wins)
        n = int(n)
    lb = wilson_lb(wins, n)
    wr = (wins / n) if n else 0.0
    p_final = shrunk_prob(p_model, wins, n)
    edge = p_final - float(price)

    reasons: list[str] = []
    if n < MIN_CITY_SAMPLE:
        reasons.append(f"city sample n={n} < {MIN_CITY_SAMPLE}")
    if lb < MIN_CITY_WILSON_LB:
        reasons.append(f"city Wilson LB {lb:.2f} < {MIN_CITY_WILSON_LB:.2f}")
    if p_final < MIN_P_FINAL:
        reasons.append(f"p_final {p_final:.3f} < {MIN_P_FINAL:.2f}")
    if bma_std is None or float(bma_std) > MAX_BMA_STD:
        reasons.append(f"bma_std {bma_std} > {MAX_BMA_STD:.2f}")
    if volume is not None and float(volume) < MIN_VOLUME:
        reasons.append(f"volume {float(volume):.0f} < {MIN_VOLUME:.0f}")
    if float(price) < MIN_PRICE:
        reasons.append(f"price {float(price):.3f} < {MIN_PRICE:.2f}")
    if float(price) > MAX_PRICE:
        reasons.append(f"price {float(price):.3f} > {MAX_PRICE:.2f}")

    min_edge = MIN_EDGE * (THRESHOLD_EDGE_MULT if str(market_type) == "threshold" else 1.0)
    if edge < min_edge:
        reasons.append(f"edge {edge:.3f} < {min_edge:.3f}")

    return {
        "p_final": round(p_final, 4),
        "edge_final": round(edge, 4),
        "city_n": n,
        "city_wins": wins,
        "city_wr": round(wr, 4),
        "city_wilson_lb": round(lb, 4),
        "min_edge_required": round(min_edge, 4),
        "qualified": not reasons,
        "reasons": reasons,
    }


# ---------------------------------------------------------------------------
# Forward bet ledger (real entry prices -> real, measurable ROI over time)
# ---------------------------------------------------------------------------

def load_ledger(path: Path = LEDGER_FILE) -> dict:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("bets"), list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"meta": {}, "bets": []}


def _resolve_entry(entry: dict, resolved: dict) -> bool:
    """Resolve an open ledger entry; returns True when it changed."""
    if entry.get("result") in ("WIN", "LOSS"):
        return False
    city = str(entry.get("city_key") or entry.get("city", ""))
    date_str = str(entry.get("date", ""))
    base = city.split(",")[0].strip()
    market = resolved.get((city, date_str)) or resolved.get((base, date_str))
    if not market:
        return False
    spill = entry.get("spill")
    if spill is None:
        return False
    try:
        from _model_quality_tracker import (  # type: ignore
            _spill_vs_polymarket_result,
            _spill_vs_threshold_result,
        )
        if market.get("type") == "threshold":
            res = _spill_vs_threshold_result(spill, market)
        else:
            res = _spill_vs_polymarket_result(spill, market)
    except Exception:
        return False
    if res not in ("WIN", "LOSS"):
        return False
    entry["result"] = res
    entry["resolved_at"] = _now_iso()
    price = float(entry.get("price") or 0.0)
    stake = float(entry.get("stake") or 0.0)
    payout = 1.0 if res == "WIN" else 0.0
    entry["pnl"] = round(stake * (payout / price - 1.0), 2) if price > 0 else 0.0
    return True


def update_ledger(open_entries: list[dict], path: Path = LEDGER_FILE) -> dict:
    """Upsert today's qualified picks, resolve past ones, return summary metrics."""
    ledger = load_ledger(path)
    bets: list[dict] = ledger.setdefault("bets", [])
    index = {(str(b.get("date")), str(b.get("city_key") or b.get("city"))): b for b in bets}

    for entry in open_entries:
        key = (str(entry.get("date")), str(entry.get("city_key") or entry.get("city")))
        if key in index:
            existing = index[key]
            # Keep an already-resolved result/pnl; refresh price/model fields.
            for k, v in entry.items():
                if k in ("result", "pnl", "resolved_at") and existing.get(k) is not None:
                    continue
                existing[k] = v
        else:
            bets.append(entry)
            index[key] = entry

    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        resolved = _load_market_resolved_details()
    except Exception:
        resolved = {}
    for b in bets:
        _resolve_entry(b, resolved)

    resolved_bets = [b for b in bets if b.get("result") in ("WIN", "LOSS")]
    wins = sum(1 for b in resolved_bets if b["result"] == "WIN")
    losses = len(resolved_bets) - wins
    staked = sum(float(b.get("stake") or 0.0) for b in resolved_bets)
    pnl = sum(float(b.get("pnl") or 0.0) for b in resolved_bets)
    brier = None
    if resolved_bets:
        brier = round(
            sum((float(b.get("p_final") or 0.0) - (1.0 if b["result"] == "WIN" else 0.0)) ** 2
                for b in resolved_bets) / len(resolved_bets),
            4,
        )
    summary = {
        "n_open": len(bets) - len(resolved_bets),
        "n_resolved": len(resolved_bets),
        "wins": wins,
        "losses": losses,
        "hit_rate": round(wins / len(resolved_bets), 4) if resolved_bets else None,
        "total_staked_usd": round(staked, 2),
        "total_pnl_usd": round(pnl, 2),
        "roi": round(pnl / staked, 4) if staked > 0 else None,
        "brier": brier,
        "note": (
            "Forward paper ledger with real entry prices. Historical market "
            "prices were never stored, so pre-ledger ROI cannot be reconstructed."
        ),
    }
    ledger["meta"] = {
        "updated": _now_iso(),
        "gates": {
            "min_city_sample": MIN_CITY_SAMPLE,
            "min_city_wilson_lb": MIN_CITY_WILSON_LB,
            "min_p_final": MIN_P_FINAL,
            "max_bma_std": MAX_BMA_STD,
            "min_edge": MIN_EDGE,
            "shrink_k": SHRINK_K,
            "threshold_edge_mult": THRESHOLD_EDGE_MULT,
            "min_volume": MIN_VOLUME,
            "min_price": MIN_PRICE,
            "max_price": MAX_PRICE,
        },
        "summary": summary,
    }
    path.write_text(json.dumps(ledger, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    stats = load_city_stats()
    print(f"[modified confidence] {len(stats)} cities with resolved Modifisert records")
    for city in list(stats)[:5]:
        print(f"  {city}: {stats[city]}")
    summary = update_ledger([])
    print(json.dumps(summary, indent=2, ensure_ascii=False))
