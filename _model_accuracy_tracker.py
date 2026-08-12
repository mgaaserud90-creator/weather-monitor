#!/usr/bin/env python3
"""
Per-Model Accuracy Tracker
==========================
Tracks individual NWP model accuracy against actual outcomes.
Essential for optimizing model weights after 3+ weeks of data.

Reads _model_quality_log.json for predictions and appends per-model
accuracy data to _model_accuracy_log.json.

ZERO additional API calls — purely reorganizing existing data.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
QUALITY_LOG = _SCRIPT_DIR / "_model_quality_log.json"
ACCURACY_LOG = _SCRIPT_DIR / "_model_accuracy_log.json"


def load_json(path: Path) -> dict:
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {}


def compute_model_accuracy() -> dict:
    """Compute per-city per-model accuracy from quality log predictions."""
    log = load_json(QUALITY_LOG)
    runs = log.get("runs", [])

    # Load existing accuracy data
    acc = load_json(ACCURACY_LOG)
    tracked = acc.get("tracked_models", {})  # {model_name: {city: {error_sum, count, dates}}}

    for run in runs:
        run_date = run.get("run_date", "")
        predictions = run.get("predictions", {})

        for city, pdata in predictions.items():
            sigma = pdata.get("strategies", {}).get("sigma", {})
            actual = sigma.get("actual_peak")
            if actual is None:
                continue

            bma_mean = pdata.get("bma_mean")
            if bma_mean is None:
                continue

            # Per-model data would come from ensemble module
            # For now, track BMA aggregate accuracy
            error = round(bma_mean - actual, 1)

            # Track BMA aggregate
            if "bma_aggregate" not in tracked:
                tracked["bma_aggregate"] = {}
            if city not in tracked["bma_aggregate"]:
                tracked["bma_aggregate"][city] = {"errors": [], "dates": [], "count": 0}
            tracked["bma_aggregate"][city]["errors"].append(error)
            tracked["bma_aggregate"][city]["dates"].append(run_date)
            tracked["bma_aggregate"][city]["count"] += 1

    # Compute summary stats
    summary = {}
    for model, cities in tracked.items():
        all_errors = []
        for city, data in cities.items():
            all_errors.extend(data["errors"])
        if all_errors:
            avg_error = round(sum(all_errors) / len(all_errors), 2)
            mae = round(sum(abs(e) for e in all_errors) / len(all_errors), 2)
            rmse = round((sum(e**2 for e in all_errors) / len(all_errors)) ** 0.5, 2)
            summary[model] = {
                "total_predictions": len(all_errors),
                "avg_error": avg_error,
                "mae": mae,
                "rmse": rmse,
                "min_error": min(all_errors),
                "max_error": max(all_errors),
            }

    return {
        "last_updated": datetime.now(timezone.utc).isoformat(),
        "total_runs_analyzed": len(runs),
        "summary": summary,
        "tracked_models": tracked,
    }


def main():
    print("=" * 50)
    print("  MODEL ACCURACY TRACKER")
    print("=" * 50)

    acc_data = compute_model_accuracy()

    summary = acc_data.get("summary", {})
    for model, stats in summary.items():
        print(f"\n  {model}:")
        print(f"    Predictions: {stats['total_predictions']}")
        print(f"    Avg Error: {stats['avg_error']}°C")
        print(f"    MAE: {stats['mae']}°C")
        print(f"    RMSE: {stats['rmse']}°C")

    ACCURACY_LOG.write_text(json.dumps(acc_data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nSaved to {ACCURACY_LOG}")

    # Also generate per-city CSV
    csv_path = _SCRIPT_DIR / "_model_accuracy.csv"
    with open(csv_path, "w", encoding="utf-8") as f:
        f.write("model,city,count,avg_error,mae\n")
        for model, cities in sorted(acc_data.get("tracked_models", {}).items()):
            for city, data in sorted(cities.items()):
                errors = data["errors"]
                if errors:
                    avg = round(sum(errors) / len(errors), 2)
                    mae = round(sum(abs(e) for e in errors) / len(errors), 2)
                    f.write(f"{model},{city},{len(errors)},{avg},{mae}\n")
    print(f"CSV written to {csv_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
