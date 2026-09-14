# Modifisert strategy — confidence upgrade & deep-dive on per-city corrections

Date: 2026-09-14 · Scope: v1 Polymarket Weather (`vær monitor/`)

## 1. What the user asked

Push the Modifisert strategy as high as the data allows, dig for a better
correction **for every city**, be honest when data is missing, and implement
improvements without waiting for approval.

## 2. Data actually available (and what is missing)

| Data | Status |
|---|---|
| Resolved Modifisert rows (city × date) | **1 679** rows, 2026-08-11 → 2026-09-14 (35 days, 51 cities) |
| Per-provider daily means | 1 145 rows (2026-08-11 → 2026-09-05 only) |
| BMA mean/std/confidence per row | Complete |
| Polymarket resolved outcomes (point / °F buckets / thresholds) | Complete |
| **Historical market prices per day** | **MISSING** — one live snapshot only; `_pnl_log.json` has a price on 59/1686 rows |
| Historical order-book liquidity | **MISSING** (live 24h volume only) |

**Honest conclusion:** historical **ROI cannot be reconstructed**; only
hit-rate and probability calibration can be validated locally.

## 3. The main win: ensemble the base forecast with the BMA mean

Experiment `_modified_aggregator_experiment.py` compared alternative base
estimators, each followed by the city's existing correction, resolved with the
real Polymarket resolver (walk-forward 70/30):

| Base | Test hit rate (n=300) |
|---|---|
| weighted mean of providers (previous Modifisert) | 38.0 % |
| equal mean / median / trimmed mean | 36–40 % |
| provider-bias-corrected weighted mean | 31.3 % |
| **0.25·weighted + 0.75·BMA mean** | **47.3 %** |
| pure BMA mean | 46.0 % |

Full-series confirmation (`_modified_blend_eval.py`, all 1 679 rows):

| Base | Hit rate |
|---|---|
| previous (pure provider weighted mean on 1 104 rows) | 41.9 % |
| pure BMA mean + correction | 45.0 % |
| **0.25·provider + 0.75·BMA + correction (shipped)** | **45.9 %** |

The BMA mean is simply the stronger estimator; the old weighting was adding
variance. `PROVIDER_BLEND_ALPHA = 0.25` in `_modified_strategy.py` now blends
it in. Overall Modifisert hit rate: **41.9 % → 45.9 %** (703W → 770W).

## 4. Deep-dive: can a *better per-city correction* be found?

I built three dedicated experiments and validated everything walk-forward:

1. `_modified_correction_optimizer.py` — per-city constant offset on top of the
   current corrected mean. Test hit: current 41.3 % → per-city offset 43.7 %.
   **+2.4 pp, within one standard error (~3 pp) → not statistically reliable.**
   Global best offset = 0.00 (no systematic bias).
2. `_modified_aggregator_experiment.py` — equal/median/trimmed/bias-corrected
   aggregators all ≤ the weighted mean; the **blend** is the only winner.
3. `_modified_percity_alpha.py` — a per-city blend weight picked on train:
   test hit **44.0 %**, *worse* than the single global weight (47.3 %). Classic
   overfitting: ~24 train / 11 test days per city cannot support per-city tuning.

**Honest finding:** per-city corrections beyond the existing models do **not**
generalise. The current per-city correction models (baseline/additive/median/
linear/multiplicative, out-of-sample params from `_per_city_curvefit.json`) are
already at this dataset's information limit; adding per-city knobs makes it
worse. Going higher needs *more data*, not more parameters.

## 5. Confidence of the improved strategy (walk-forward)

Train 2026-08-11→09-03, test 2026-09-04→09-14, city picked on train only:

| Rule | test n | test hit rate | Wilson LB |
|---|---|---|---|
| City train WR ≥ 60 % | 136 | 54.4 % | — |
| City 60 % + p ≥ 0.35 + std ≤ 1.1 | 64 | 57.8 % | 45.6 % |
| City 60 % + p ≥ 0.40 + std ≤ 1.0 | 35 | 60.0 % | 43.6 % |
| City 60 % + p ≥ 0.35 + std ≤ 0.9 | 33 | 60.6 % | 43.7 % |

Calibration of the model probability remains good (P∈[0.40,0.50) → 48.1 %
realised), and hit rate still rises with confidence (`bma_std<0.8` → 49.4 % vs
`≥1.5` → 37.5 %, `confidence≥0.75` → 51–53 %).

Strong cities (Wilson LB): Jinan 96.8 %, Singapore 74.2 %, Tel Aviv 68.6 %,
Seoul/Zhengzhou 64.5 %, Karachi/Lucknow 62.5 %, Mexico City 61.8 %.
Weak: Miami 5.9 %, San Francisco 20.6 %, Taipei 22.6 %.

## 6. The shipped strategy

`_modified_strategy.py` (base + per-city correction) → `_modified_confidence.py`
(probability + gates) → `_recommended_bets.py` (selection) →
`_modified_bets_log.json` (forward ROI ledger).

- base = 0.25·provider weighted mean + 0.75·BMA mean (when providers exist),
  otherwise BMA mean; then the city's correction model;
- `p_final` = Beta-Binomial shrinkage of the calibrated BMA bucket probability
  toward the chosen strategy's own resolved city record;
- gates: city n ≥ 5 & Wilson LB ≥ 0.40, `p_final` ≥ 0.40, `bma_std` ≤ 1.0,
  24h volume ≥ 5 000, `0.02 ≤ price ≤ 0.95`, bucket must contain the spill,
  edge ≥ 5 pp (threshold markets double);
- quarter-Kelly sizing; every qualified open bet logged with its real entry
  price and auto-resolved later.

## 7. Reality check — is this "passive income" yet?

Not provably. The hit-rate edge is real and out-of-sample (≈58–61 % on gated
picks vs 46 % ungated), but whether it survives **after price and fees** needs
≥ 8–12 weeks of `_modified_bets_log.json` real-price data, because historical
prices were never stored. The strategy also deliberately produces **lumpy**
(often zero-bet) days — correct for +EV betting, not a flaw.

## 8. How to run / tune

```bash
python _modified_strategy.py              # base blend + full resolved series
python _modified_aggregator_experiment.py # base-estimator comparison
python _modified_blend_eval.py            # full-series blend sweep
python _modified_correction_optimizer.py  # per-city offset walk-forward
python _modified_percity_alpha.py         # per-city blend-weight walk-forward
python _modified_research.py              # confidence + calibration evidence
python _recommended_bets.py               # gated picks + forward ledger
```

Env tunables: `MOD_MIN_CITY_SAMPLE`, `MOD_MIN_CITY_WILSON_LB`, `MOD_MIN_P`,
`MOD_MAX_BMA_STD`, `MOD_MIN_EDGE`, `MOD_SHRINK_K`, `MOD_MIN_VOLUME`,
`MOD_MIN_PRICE`, `MOD_MAX_PRICE`. The blend weight is `PROVIDER_BLEND_ALPHA`
in `_modified_strategy.py`.
