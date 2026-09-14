# Modifisert strategy — confidence upgrade & honest assessment

Date: 2026-09-14 · Scope: v1 Polymarket Weather (`vær monitor/`)

## 1. What the user asked

Raise the *confidence* of the Modifisert strategy until it can "more or less
be regarded as passive income", be honest about missing data, and implement
improvements without waiting for approval.

## 2. Data actually available (and what is missing)

| Data | Status |
|---|---|
| Resolved Modifisert rows (city × date) | **1 679** rows, 2026-08-11 → 2026-09-14 (35 days, 51 cities) |
| BMA confidence per row (`bma_std`, `confidence`, `models`) | Complete (0 rows missing std) |
| Polymarket resolved outcomes (point, °F buckets, thresholds) | Complete for all days |
| **Historical market prices per day** | **MISSING** — `_market_prices.json` is a single live snapshot; `_pnl_log.json` has only 59/1686 rows with a price |
| Historical order-book liquidity | **MISSING** (only 24h volume in the live snapshot) |

**Honest conclusion on data:** historical **ROI cannot be reconstructed**.
Only *hit-rate* and *probability calibration* can be validated locally. Any
claim of "passive income" from the existing history would be unsupported.

## 3. Evidence from the data (`_modified_research.py`)

Overall Modifisert hit rate: **41.9 %** (703W / 976L). That alone is not
profitable at typical prices — so the value must come from *selection*.

### 3.1 Probability is well calibrated
Binning rows by the model's implied probability of the chosen bucket:

| P(central bucket) | n | realised hit rate | mean model P |
|---|---|---|---|
| < 0.20 | 107 | 31.8 % | 0.168 |
| 0.20–0.30 | 369 | 36.6 % | 0.259 |
| 0.30–0.40 | 614 | 41.4 % | 0.349 |
| 0.40–0.50 | 430 | 44.9 % | 0.444 |
| 0.50–0.60 | 150 | 54.7 % | 0.539 |
| ≥ 0.60 | 9 | 55.6 % | 0.615 |

Model probability ≈ realised frequency ⇒ **P is usable as an edge input**.

### 3.2 Confidence is monotonic
- `bma_std < 0.8` → 48.1 % vs `bma_std ≥ 1.5` → 33.3 %.
- `confidence ≥ 0.75` → ~48–49 % vs `< 0.55` → 37.6 %.

### 3.3 City dispersion is large and stable
Best (Wilson 95 % lower bound): Jinan 96.8 %, Singapore 74.2 %, Ankara/NY
64.7 %, Zhengzhou 64.5 %, Karachi/Lucknow 62.5 %.
Worst: Miami/Denver 11.8 %, San Francisco 14.7 %, Los Angeles 17.6 %.

### 3.4 Walk-forward (train 08-11→09-03, test 09-04→09-14)

| Rule (chosen on train only) | test n | test hit rate | Wilson LB |
|---|---|---|---|
| City train WR ≥ 60 % (n ≥ 5) | 103 | 57.3 % | — |
| City 60 % + p ≥ 0.35 + std ≤ 0.9 | 36 | **63.9 %** | 47.6 % |
| City 60 % + p ≥ 0.40 + std ≤ 1.0 | 38 | 57.9 % | 42.2 % |
| City 60 % + p ≥ 0.45 + std ≤ 0.9 | 14 | 71.4 % | 45.4 % |

The gate survives out-of-sample: combining the calibrated probability with the
city record and a model-agreement filter lifts hit rate from 42 % to ~58–64 %.

## 4. The strategy implemented

`_modified_confidence.py` + `_recommended_bets.py` now produce a conservative
**positive-expectancy selection** (not a blind "bet every day"):

1. **City track record** — chosen strategy must have `n ≥ 5` and Wilson 95 %
   lower bound ≥ 0.40 for that city/strategy.
2. **Confidence-adjusted probability**
   `p_final = (wins + k·p_model) / (n + k)`, `k = 5` (Beta-Binomial shrinkage of
   the calibrated BMA probability toward the city record).
3. **Model agreement** — `bma_std ≤ 1.0 °C`.
4. **Liquidity / price sanity** — 24h volume ≥ 5 000; `0.02 ≤ price ≤ 0.95`
   (excludes stale/illiquid quotes and resolved extremes).
5. **Bucket integrity** — the chosen market bucket must actually *contain* the
   spill (°C exact match / °F range containment). Threshold markets require
   **double** edge.
6. **Edge** — `p_final − price ≥ 0.05` (positive expected value after a margin).
7. **Sizing** — quarter-Kelly on `p_final`, capped by bankroll/stake cap.
8. **Forward ledger** — every qualified, open bet is written to
   `_modified_bets_log.json` with the real entry price; it is auto-resolved on
   later runs and reports hit-rate, ROI and Brier score.

Because `edge ≥ 5pp` implies `price ≤ p_final − 0.05`, a bet is only placed
when the entry price is *below* the estimated win probability — the necessary
(though not sufficient) condition for long-run profit.

## 5. Reality check — is this "passive income"?

Not yet provably. With ~1 680 resolved observations and **no stored historical
prices**, the honest statement is:

- the **hit-rate edge is real and out-of-sample** (≈58–64 % on gated picks vs
  42 % ungated);
- whether that survives **after price and fees** can only be shown by the new
  forward ledger, because historical prices do not exist in the repo;
- the strategy deliberately produces **few, high-confidence bets** (often zero
  on a given day when markets are resolved or fairly priced) rather than a
  steady stream — that is the right behaviour for positive-EV betting but
  means income is lumpy, not a metronome.

**What would make it conclusive:** ≥ 8–12 weeks of `_modified_bets_log.json`
with real entry prices; then ROI, drawdown and Brier score become measurable
and the gates can be re-tuned on out-of-sample data.

## 6. How to run / tune

```bash
python _modified_strategy.py        # full resolved daily series + today
python _modified_research.py        # confidence / walk-forward evidence
python _recommended_bets.py        # gated picks + forward ledger
```

All gates are env-overridable: `MOD_MIN_CITY_SAMPLE`, `MOD_MIN_CITY_WILSON_LB`,
`MOD_MIN_P`, `MOD_MAX_BMA_STD`, `MOD_MIN_EDGE`, `MOD_SHRINK_K`,
`MOD_THRESHOLD_EDGE_MULT`, `MOD_MIN_VOLUME`, `MOD_MIN_PRICE`, `MOD_MAX_PRICE`.

The pipeline (`full_auto_pipeline.yml`) runs both the strategy and the research
report daily, before the daily city log and recommended bets.
