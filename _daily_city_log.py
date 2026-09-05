#!/usr/bin/env python3
"""
Daily city log — one complete row per (city, date, strategy).

Builds ``_daily_city_log.csv`` and ``_daily_city_log.json`` from
``_model_quality_log.json`` + the unit-aware resolved-markets lookup so the
user can see exactly why a city missed on a given day:

    city, date, predicted_mean_c, predicted_spill_c, strategy,
    resolved_c, resolved_unit, deviation_c, win_loss, bucket_label

Rows are emitted for all three strategies (sigma / p5 / mean) so every bet is
accounted for. Unresolved rows keep win_loss empty and resolved_c null.

Safe to re-run daily — the files are rebuilt idempotently from the quality log.
"""

from __future__ import annotations

import csv
import json
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

BASE_DIR = Path(__file__).resolve().parent
MODEL_QUALITY_LOG = BASE_DIR / "_model_quality_log.json"
DAILY_CSV = BASE_DIR / "_daily_city_log.csv"
DAILY_JSON = BASE_DIR / "_daily_city_log.json"

CSV_COLUMNS = [
    "city", "date", "predicted_mean_c", "predicted_spill_c", "strategy",
    "resolved_c", "resolved_unit", "deviation_c", "win_loss", "bucket_label",
]

STRATEGIES = ("sigma", "p5", "mean")


def _load_quality_runs() -> list[dict]:
    if not MODEL_QUALITY_LOG.exists():
        return []
    try:
        data = json.loads(MODEL_QUALITY_LOG.read_text(encoding="utf-8"))
        return data.get("runs", []) or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_resolved_markets() -> dict:
    try:
        from _model_quality_tracker import _load_market_resolved_details  # type: ignore
        return _load_market_resolved_details()
    except Exception:
        return {}


def _resolved_value_c(market_info: dict | None) -> float | None:
    if not market_info or market_info.get("value") is None:
        return None
    try:
        value = float(market_info["value"])
    except (TypeError, ValueError):
        return None
    unit = (market_info.get("unit") or "C").upper()
    if unit == "F":
        return round((value - 32.0) * 5.0 / 9.0, 2)
    return round(value, 2)


def _bucket_label(market_info: dict | None) -> str:
    if not market_info:
        return ""
    return str(market_info.get("bucket") or "")


def _win_loss(pdata: dict, strategy: str, market_info: dict | None) -> str:
    strat = (pdata.get("strategies") or {}).get(strategy) or {}
    result = strat.get("result")
    if result in ("WIN", "LOSS"):
        return result
    # Fall back to recomputing from the resolved market when possible.
    spill = strat.get("spill")
    if spill is None or not market_info or market_info.get("value") is None:
        return ""
    try:
        from _model_quality_tracker import _spill_vs_polymarket_result  # type: ignore
        res = _spill_vs_polymarket_result(spill, market_info)
        return res if res in ("WIN", "LOSS") else ""
    except Exception:
        return ""


def build_rows() -> list[dict]:
    runs = _load_quality_runs()
    resolved = _load_resolved_markets()

    rows: list[dict] = []
    seen: set[tuple[str, str, str]] = set()

    for run in sorted(runs, key=lambda r: r.get("run_date", "")):
        run_date = run.get("run_date", "")
        for city, pdata in (run.get("predictions", {}) or {}).items():
            if not isinstance(pdata, dict):
                continue
            date_str = pdata.get("_target_date") or run_date
            if not date_str:
                continue

            mean_c = pdata.get("bma_mean")
            if mean_c is None:
                continue
            try:
                mean_c_f = float(mean_c)
            except (TypeError, ValueError):
                continue

            base = city.split(",")[0].strip()
            market_info = resolved.get((city, date_str)) or resolved.get((base, date_str))
            resolved_c = _resolved_value_c(market_info)
            bucket = _bucket_label(market_info)
            resolved_unit = (market_info.get("unit") or "C").upper() if market_info else ""

            for sn in STRATEGIES:
                key = (city, date_str, sn)
                if key in seen:
                    continue
                seen.add(key)
                strat = (pdata.get("strategies") or {}).get(sn) or {}
                spill = strat.get("spill")
                try:
                    spill_i = int(spill) if spill is not None else None
                except (TypeError, ValueError):
                    spill_i = None

                deviation_c = None
                if resolved_c is not None:
                    deviation_c = round(mean_c_f - resolved_c, 2)

                rows.append({
                    "city": city,
                    "date": date_str,
                    "predicted_mean_c": round(mean_c_f, 2),
                    "predicted_spill_c": spill_i,
                    "strategy": sn,
                    "resolved_c": resolved_c,
                    "resolved_unit": resolved_unit,
                    "deviation_c": deviation_c,
                    "win_loss": _win_loss(pdata, sn, market_info),
                    "bucket_label": bucket,
                })

    rows.sort(key=lambda r: (r["date"], r["city"], r["strategy"]))
    return rows


def write_outputs(rows: list[dict]) -> None:
    output = {
        "updated": datetime.now(timezone.utc).isoformat(),
        "columns": CSV_COLUMNS,
        "n_rows": len(rows),
        "rows": rows,
    }
    DAILY_JSON.write_text(json.dumps(output, indent=2, ensure_ascii=False), encoding="utf-8")

    with open(DAILY_CSV, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            row = {c: r.get(c) for c in CSV_COLUMNS}
            # Keep CSV values simple: None -> "".
            for c in CSV_COLUMNS:
                if row[c] is None:
                    row[c] = ""
            writer.writerow(row)


def main() -> int:
    rows = build_rows()
    write_outputs(rows)
    n_days = len({r["date"] for r in rows})
    n_cities = len({r["city"] for r in rows})
    n_win = sum(1 for r in rows if r["win_loss"] == "WIN")
    n_loss = sum(1 for r in rows if r["win_loss"] == "LOSS")
    print(f"[daily city log] {len(rows)} rows ({n_cities} cities, {n_days} days) "
          f"— WIN={n_win} LOSS={n_loss}")
    print(f"  {DAILY_CSV.name} and {DAILY_JSON.name} written")
    return 0


if __name__ == "__main__":
    sys.exit(main())
