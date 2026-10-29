#!/usr/bin/env python3
"""Tests for the evaluation harness and the instrument repairs (P0.1–P0.3, P1).

Run with:
    python _vmon_analysis/test_harness.py

Nothing here writes to a committed artifact: the ledger tests point
``_pnl_tracker.PNL_LOG_FILE`` at a temporary file and restore it afterwards.
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "_vmon_analysis"))

import eval_harness as H  # noqa: E402
import _pnl_tracker as P  # noqa: E402
import _model_quality_tracker as Q  # noqa: E402


# ────────────────────────────────────────────────────────────────────────────
# P0.2 — price-aware settlement (the four required cases)
# ────────────────────────────────────────────────────────────────────────────

class SettlementTests(unittest.TestCase):
    def _record(self, price, result, stake=100.0):
        return P.apply_settlement({
            "market_price": price,
            "result": result,
            "stake": stake,
            "pnl": None,
            "payout": None,
            "tradable": False,
        })

    def test_win_at_024(self):
        """A 0.24-priced win pays 1/0.24 − 1 per dollar, not +1."""
        rec = self._record(0.24, "WIN")
        self.assertTrue(rec["tradable"])
        self.assertAlmostEqual(rec["payout"], 1.0 / 0.24 - 1.0, places=6)
        self.assertAlmostEqual(rec["pnl"], 100.0 * (1.0 / 0.24 - 1.0), places=6)
        self.assertNotAlmostEqual(rec["payout"], 1.0, places=6)

    def test_win_at_095(self):
        """A 0.95-priced win pays ~0.0526 per dollar — the old code paid +1."""
        rec = self._record(0.95, "WIN")
        self.assertAlmostEqual(rec["payout"], 1.0 / 0.95 - 1.0, places=6)
        self.assertAlmostEqual(rec["pnl"], 100.0 * (1.0 / 0.95 - 1.0), places=6)
        self.assertLess(rec["pnl"], 6.0)

    def test_threshold_high_loss(self):
        """'26°C or higher' with a 25 °C spill is a LOSS, and pnl = −stake."""
        info = {
            "type": "threshold",
            "unit": "C",
            "bucket": "26°C or higher",
            "lower_bound_c": 26.0,
            "value": 26.0,
        }
        # The point rule must refuse a threshold market outright.
        self.assertIsNone(H.settle_point(25.0, info))
        self.assertFalse(H.settle_threshold(25.0, info))
        self.assertTrue(H.settle_threshold(26.0, info))

        rec = self._record(0.40, "LOSS")
        self.assertTrue(rec["tradable"])
        self.assertEqual(rec["payout"], -1.0)
        self.assertAlmostEqual(rec["pnl"], -100.0, places=6)

    def test_f_range_bucket_against_stored_bounds(self):
        """An °F range bucket is settled on [lo_f, hi_f], never on a °C rounding."""
        info = {
            "type": "point",
            "unit": "F",
            "value": 86.5,
            "lo_f": 86, "hi_f": 87,
            "lo_c": round((86 - 32) * 5 / 9, 3),
            "hi_c": round((87 - 32) * 5 / 9, 3),
        }
        # 30.3 °C = 86.54 °F -> inside [86, 87] -> WIN.
        self.assertTrue(H.settle_point(30.3, info))
        # 30.0 °C = 86.00 °F -> on the boundary -> WIN (inclusive).
        self.assertTrue(H.settle_point(30.0, info))
        # 30.7 °C = 87.26 °F -> outside -> LOSS.
        self.assertFalse(H.settle_point(30.7, info))
        # Contrast: the historical rounding comparison DISAGREES on this row —
        # round(86.54) = 87 against round(86.5) = 86, so it books a LOSS where
        # the true bucket [86, 87] pays. That disagreement is exactly the
        # instrument defect P0.1 removed.
        legacy = dict(info, lo_f=None, hi_f=None, lo_c=None, hi_c=None)
        self.assertFalse(H.settle_point(30.3, legacy, force_round=True))


class GuardTests(unittest.TestCase):
    def test_degenerate_prices_are_not_tradeable(self):
        for price in (0.0, 0.001, 0.999, 1.0, None):
            rec = P.apply_settlement({
                "market_price": price, "result": "WIN", "stake": 100.0,
                "pnl": None, "payout": None, "tradable": True,
            })
            self.assertFalse(rec["tradable"], msg=f"{price} must be untradeable")
            self.assertIsNone(rec["pnl"])

    def test_half_step_resolution_is_unsettleable(self):
        info = {"type": "point", "unit": "F", "value": 86.5}  # no bounds stored
        self.assertIsNone(H.settle_point(30.0, info))

    def test_threshold_markets_never_take_the_point_rule(self):
        info = {"type": "threshold", "unit": "C", "value": 30.0,
                "lower_bound_c": 30.0, "bucket": "30°C or higher"}
        self.assertIsNone(Q._spill_vs_polymarket_result(30.0, info))
        self.assertEqual(Q.settle_branch_counters()["threshold_rejected"], 1)


class FreezeTests(unittest.TestCase):
    def test_rebuild_twice_changes_no_price_field(self):
        """P0.3: a rebuild may fill nulls, never overwrite a price."""
        with tempfile.TemporaryDirectory() as tmp:
            original = P.PNL_LOG_FILE
            P.PNL_LOG_FILE = Path(tmp) / "_pnl_log.json"
            try:
                first = {
                    "date": "2026-10-01", "city": "Taipei, TW",
                    "market_price": 0.31, "price_ts": "2026-10-01T00:00:00Z",
                    "price_source": "yes_price_snapshot", "edge": 0.05,
                    "kelly": 12.0, "eligible": True, "stake": 100.0,
                    "result": "WIN", "settle_rule": "point_c",
                }
                ledger = {"records": [], "updated_at": None}
                P.upsert_bet(dict(first), ledger)
                snapshot = json.loads(json.dumps(ledger["records"][0]))

                # Second rebuild: the live market is gone, so the price is None.
                second = dict(first)
                second.update({"market_price": None, "price_ts": None,
                               "price_source": "none", "edge": None,
                               "kelly": None, "eligible": False})
                P.upsert_bet(second, ledger)
                after_two = ledger["records"][0]

                for field in P.FREEZE_PRICE_FIELDS:
                    self.assertEqual(after_two[field], snapshot[field],
                                     msg=f"{field} was not frozen")

                # And a third pass still changes nothing.
                P.upsert_bet(dict(second), ledger)
                for field in P.FREEZE_PRICE_FIELDS:
                    self.assertEqual(ledger["records"][0][field], snapshot[field])
                self.assertEqual(len(ledger["records"]), 1)
            finally:
                P.PNL_LOG_FILE = original


# ────────────────────────────────────────────────────────────────────────────
# P1 — harness determinism and the pinned baseline
# ────────────────────────────────────────────────────────────────────────────

class HarnessDeterminismTests(unittest.TestCase):
    def test_rerun_is_byte_identical(self):
        import argparse
        args = argparse.Namespace(
            folds="rolling", embargo=2, holdout_from="2026-10-10",
            price_mode="mid", mode="baseline", config=None,
            permutations=5, seed=H.DEFAULT_SEED, out=None, dry_run=True,
        )
        first = json.dumps(H.run(args), indent=2, sort_keys=True)
        second = json.dumps(H.run(args), indent=2, sort_keys=True)
        self.assertEqual(first, second)

    def test_permutation_seed_is_reproducible(self):
        rows, _ = H.build_rows()
        dev = [r for r in rows if r.target < "2026-10-10"]
        configs = H.build_configs()[:3]
        a = H.max_statistic(dev, configs, horizon=None, draws=3, seed=7)
        b = H.max_statistic(dev, configs, horizon=None, draws=3, seed=7)
        self.assertEqual(a["adjusted_p"], b["adjusted_p"])
        self.assertEqual(a["null_min"], b["null_min"])


class BaselinePinTests(unittest.TestCase):
    """Pin the panel the design document describes, and our own baseline level.

    The reference implementation (``_vmon_analysis/vm.js``) was committed EMPTY,
    so the exact p convention behind the published 1.7812 cannot be inspected.
    Everything that CAN be checked is pinned here; the LogLoss level is pinned
    at the value this ported implementation produces, and the gap to 1.7812 is
    documented in the harness output rather than papered over.
    """

    @classmethod
    def setUpClass(cls):
        cls.rows, cls.ledger = H.build_rows()
        cls.hist = H.aggregate(cls.rows, lambda r: r.mu_c, lambda r: r.sd_c,
                               native=False, force_round=True)

    def test_panel_size_matches_design_document(self):
        self.assertEqual(self.hist["n"], 3663)
        self.assertEqual(self.ledger["predictions_dedup_city_dates"], 3869)
        self.assertEqual(self.ledger["resolution_census"]["point"], 4103)
        self.assertEqual(self.ledger["resolution_census"]["threshold"], 137)

    def test_hit_rates_match_design_document(self):
        units = self.hist["per_unit"]
        self.assertAlmostEqual(units["C"]["hit_rate"], 0.3299, places=4)
        self.assertAlmostEqual(units["F"]["hit_rate"], 0.2251, places=4)

    def test_f_mean_stored_probability_is_3585(self):
        parity = H.f_parity_report(self.rows)
        self.assertAlmostEqual(parity["F"]["mean_stored_win_prob"], 0.3585, places=4)

    def test_logloss_pinned(self):
        # Pinned to THIS implementation (port of Appendix A). Tolerance 1e-4.
        self.assertAlmostEqual(self.hist["logloss"], 1.789466, delta=1e-4)


# ────────────────────────────────────────────────────────────────────────────
# P0.1 — bounds persistence
# ────────────────────────────────────────────────────────────────────────────

class BoundsTests(unittest.TestCase):
    def test_c_point_market_gets_one_degree_bounds(self):
        info = Q._parse_market_question("Will the highest temperature in Tokyo be 30°C on 2026-10-10?")
        self.assertIsNotNone(info)
        self.assertAlmostEqual(info["lo_c"], 29.5, places=6)
        self.assertAlmostEqual(info["hi_c"], 30.5, places=6)
        self.assertAlmostEqual(info["bounds_width_c"], 1.0, places=6)

    def test_f_range_market_gets_native_bounds(self):
        info = Q._parse_market_question("Highest temperature in New York be between 86-87°F on 2026-10-10?")
        self.assertIsNotNone(info)
        self.assertEqual(info["lo_f"], 86)
        self.assertEqual(info["hi_f"], 87)
        self.assertAlmostEqual(info["bounds_width_c"], 5 / 9, places=3)

    def test_f_single_market_is_one_f_wide(self):
        info = Q._parse_market_question("Highest temperature in Miami be 92°F on 2026-10-10?")
        self.assertIsNotNone(info)
        self.assertAlmostEqual(info["hi_f"] - info["lo_f"], 1.0, places=6)

    def test_bounds_branch_dominates(self):
        Q.reset_settle_branch_counters()
        info = {"type": "point", "unit": "F", "value": 86.5,
                "lo_f": 86, "hi_f": 87,
                "lo_c": round((86 - 32) * 5 / 9, 3), "hi_c": round((87 - 32) * 5 / 9, 3)}
        for spill in (30.0, 30.2, 30.4, 30.6, 30.8):
            Q._spill_vs_polymarket_result(spill, info)
        counters = Q.settle_branch_counters()
        self.assertEqual(counters["point_f_bounds"], 5)
        self.assertEqual(counters["rounding_fallback"], 0)
        self.assertEqual(Q.bounds_branch_share(), 1.0)


# ────────────────────────────────────────────────────────────────────────────
# P0.4 — price-history retention
# ────────────────────────────────────────────────────────────────────────────

class PriceHistoryTests(unittest.TestCase):
    def test_retention_compacts_an_oversized_file(self):
        import _fetch_market_prices as F
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as tmp:
            original = F.PRICE_HISTORY_FILE
            F.PRICE_HISTORY_FILE = Path(tmp) / "_price_history.jsonl"
            try:
                now = datetime(2026, 10, 29, tzinfo=timezone.utc)
                rows = []
                # 30 days of history, 6 snapshots a day, 2 markets -> 360 rows.
                for day in range(30):
                    ts_base = now - timedelta(days=day)
                    for hour in (0, 4, 8, 12, 16, 20):
                        ts = ts_base.replace(hour=hour).isoformat()
                        for bucket in (28.0, 29.0):
                            rows.append({
                                "city": "Tokyo, JP", "date": "2026-10-01",
                                "bucket_lo": bucket, "bucket_hi": bucket + 1,
                                "unit": "C", "snapshot_ts": ts,
                                "yes_price": 0.3, "no_price": 0.7, "volume": 10,
                                "question_hash": "abc",
                            })
                F.PRICE_HISTORY_FILE.write_text(
                    "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
                before = len(F.PRICE_HISTORY_FILE.read_text(encoding="utf-8").splitlines())

                stats = F.compact_price_history(now=now)
                after = len(F.PRICE_HISTORY_FILE.read_text(encoding="utf-8").splitlines())

                self.assertTrue(stats["rewritten"])
                self.assertLess(after, before)
                # Inside the full-resolution window nothing is dropped, so the
                # 7 most recent days keep all 12 rows each.
                self.assertGreaterEqual(after, 7 * 12)
                # Compaction is idempotent.
                again = F.compact_price_history(now=now)
                self.assertFalse(again["rewritten"])
                self.assertEqual(again["rows_after"], after)
            finally:
                F.PRICE_HISTORY_FILE = original

    def test_append_writes_one_row_per_market(self):
        import _fetch_market_prices as F
        with tempfile.TemporaryDirectory() as tmp:
            original = F.PRICE_HISTORY_FILE
            F.PRICE_HISTORY_FILE = Path(tmp) / "_price_history.jsonl"
            try:
                markets = [
                    {"city": "Tokyo, JP", "date": "2026-10-10", "volume": 5,
                     "question": "Highest temperature in Tokyo be 30°C",
                     "outcomes": [{"label": "Yes", "price": 0.4},
                                  {"label": "No", "price": 0.6}]},
                    {"city": "New York, US", "date": "2026-10-10", "volume": 9,
                     "question": "Highest temperature in New York be between 86-87°F",
                     "outcomes": [{"label": "Yes", "price": 0.25},
                                  {"label": "No", "price": 0.75}]},
                ]
                written = F.append_price_history(markets, "2026-10-29T12:00:00+00:00")
                self.assertEqual(written, 2)
                lines = [json.loads(line) for line in
                         F.PRICE_HISTORY_FILE.read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(lines), 2)
                f_row = [r for r in lines if r["unit"] == "F"][0]
                self.assertEqual((f_row["bucket_lo"], f_row["bucket_hi"]), (86.0, 87.0))
                for row in lines:
                    for field in ("city", "date", "bucket_lo", "bucket_hi", "unit",
                                  "snapshot_ts", "yes_price", "volume", "question_hash"):
                        self.assertIn(field, row)
            finally:
                F.PRICE_HISTORY_FILE = original


if __name__ == "__main__":
    unittest.main(verbosity=2)
