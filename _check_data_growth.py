#!/usr/bin/env python3
"""Data-growth guard for the append-only artifacts (F2).

Three of the pipeline's files only ever grow: ``_model_quality_log.json``
(append-only runs[]), ``_pnl_log.json`` (upserted ledger) and
``_wunderground_real.json`` (one row per city per day). They are committed to
git every run, so unbounded growth is not just a disk cost — it is a checkout,
diff and transfer cost on every single workflow, and it eventually collides
with the job timeout budget that A3 just made rigorous.

This guard fails the build before an artifact becomes a liability:

    file                       budget
    -------------------------- ------
    _model_quality_log.json        25 MB
    _pnl_log.json                  40 MB
    _market_prices.json            15 MB
    _wunderground_real.json         5 MB
    _price_history.jsonl          120 MB

Measured baseline at the time this guard landed (2026-10-29):
    _model_quality_log.json   7.18 MB   (29 % of budget)
    _pnl_log.json             1.60 MB   ( 4 %)
    _market_prices.json       1.24 MB   ( 8 %)
    _wunderground_real.json   0.78 MB   (16 %)
    _price_history.jsonl      0.00 MB   ( 0 %)  - starts empty (P0.4)

``_price_history.jsonl`` is the one genuinely append-only file in the set, so it
carries an explicit budget AND its writer applies a retention rule: every row
for 7 days, one row per market per day up to 90 days, then dropped. An
unbounded append-only artifact is exactly the failure mode this guard exists to
catch, so the budget is not optional for it.

Behaviour:
  * missing file  -> note only (a job may legitimately not have produced it)
  * > 80 % budget -> ::warning:: (time to plan compaction/rotation)
  * > 100 %       -> ::error:: and exit 1

Usage:
    python _check_data_growth.py
    python _check_data_growth.py --dir path/to/repo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

MB = 1024 * 1024

# (filename, budget in MB)
BUDGETS: tuple[tuple[str, int], ...] = (
    ("_model_quality_log.json", 25),
    ("_pnl_log.json", 40),
    ("_market_prices.json", 15),
    ("_wunderground_real.json", 5),
    ("_price_history.jsonl", 120),
)

WARN_FRACTION = 0.80


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dir",
        default=str(BASE_DIR),
        help="Directory holding the artifacts (default: this script's directory)",
    )
    args = parser.parse_args(argv)

    root = Path(args.dir)
    if not root.is_dir():
        print(f"::error::data-growth guard: no such directory: {root}")
        return 2

    print(f"Data-growth guard — {len(BUDGETS)} artifact budget(s) in {root.name}/")

    errors: list[str] = []
    warnings: list[str] = []

    for name, budget_mb in BUDGETS:
        path = root / name
        if not path.exists():
            print(f"  {name}: not present (skipped)")
            continue
        size_mb = path.stat().st_size / MB
        used = size_mb / budget_mb
        print(
            f"  {name}: {size_mb:.2f} MB of {budget_mb} MB "
            f"({used:.0%} of budget)"
        )
        if size_mb > budget_mb:
            errors.append(
                f"::error::{name} is {size_mb:.2f} MB, over its {budget_mb} MB "
                f"budget by {size_mb - budget_mb:.2f} MB — compact or rotate it"
            )
        elif used > WARN_FRACTION:
            warnings.append(
                f"::warning::{name} is {size_mb:.2f} MB, "
                f"{used:.0%} of its {budget_mb} MB budget"
            )

    for warning in warnings:
        print(f"  {warning}")
    for error in errors:
        print(f"  {error}")

    if errors:
        print(f"\nFAILED: {len(errors)} artifact(s) over budget.")
        return 1

    print("\nOK: every artifact is within its growth budget.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
