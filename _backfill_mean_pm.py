#!/usr/bin/env python3
"""One-off backfill: persist mean(round)-vs-Polymarket results into the log.

Populates every run in _model_quality_log.json with:
  - pdata["strategies"]["mean"]["pm_result"] = "WIN" | "LOSS" | None
  - run["mean_pm_winners"] = [{date, city, mean_spill, pm_value, pm_unit, pm_bucket}]
and refreshes the cumulative mean_pm_wins / mean_pm_losses counters.

Idempotent: re-running deduplicates winners by (date, city) and recomputes the
cumulative counters from every run's predictions (not the run summaries).
"""
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from _model_quality_tracker import (  # noqa: E402
    _load_market_resolved_details,
    _mean_pm_result,
    _save_log,
)

LOG = SCRIPT_DIR / "_model_quality_log.json"


def main() -> None:
    log_data = json.loads(LOG.read_text(encoding="utf-8"))
    resolved_markets = _load_market_resolved_details()

    for run in log_data.get("runs", []):
        winners = run.setdefault("mean_pm_winners", [])
        known = {(w.get("date"), w.get("city")) for w in winners}
        for city, pdata in run.get("predictions", {}).items():
            mean = (pdata.get("strategies", {}) or {}).get("mean", {})
            mean_spill = mean.get("spill")
            if mean_spill is None:
                continue
            city_target = pdata.get("_target_date", run.get("run_date", ""))
            city_base = city.split(",")[0].strip()
            market_info = (
                resolved_markets.get((city, city_target))
                or resolved_markets.get((city_base, city_target))
            )
            pm_result = _mean_pm_result(market_info, mean_spill)
            mean["pm_result"] = pm_result
            if pm_result == "WIN" and market_info is not None:
                winner = {
                    "date": city_target,
                    "city": city,
                    "mean_spill": int(mean_spill),
                    "pm_value": market_info.get("value"),
                    "pm_unit": (market_info.get("unit") or "C").upper(),
                    "pm_bucket": market_info.get("bucket"),
                }
                key = (winner["date"], winner["city"])
                if key not in known:
                    winners.append(winner)
                    known.add(key)

        print(f"{run.get('run_date')}: {len(winners)} mean_pm winners")

    # Refresh cumulative mean_pm counters from predictions (idempotent).
    _c_meanpm_w = _c_meanpm_l = 0
    for run in log_data.get("runs", []):
        for pdata in run.get("predictions", {}).values():
            pmr = (pdata.get("strategies", {}) or {}).get("mean", {}).get("pm_result")
            if pmr == "WIN":
                _c_meanpm_w += 1
            elif pmr == "LOSS":
                _c_meanpm_l += 1
    cum = log_data.setdefault("cumulative", {})
    cum["mean_pm_wins"] = _c_meanpm_w
    cum["mean_pm_losses"] = _c_meanpm_l
    print(f"cumulative mean_pm: {_c_meanpm_w}W/{_c_meanpm_l}L")

    _save_log(log_data)
    print("backfill complete")


if __name__ == "__main__":
    main()
