# Weighted-Mean vs Equal-Weight Mean — Curve-Fit Comparison

For each city we apply the same 5 per-city correction methods (baseline, additive, multiplicative, linear, median) to the equal-weight `bma_mean` and to the new provider-weighted mean, selecting each series' best method by in-sample (full-history) win rate and reporting the chronological 50/50 hold-out win rate. Win rule: `|corrected − resolved| ≤ 0.5 °C`.

**Aggregate (pooled, per-city best-method correction applied):**

| Series | In-sample WR | In-sample n | Hold-out WR | Hold-out n |
|---|---|---|---|---|
| Equal-weight (bma_mean) | 55.02% | 1145 | 46.52% | 589 |
| Weighted mean | 60.52% | 1145 | 53.48% | 589 |
| Δ (weighted − equal) | +5.50 pp | | +6.96 pp | |

---

| City | n | Eq best | Eq IS | Eq OOS | W best | W IS | W OOS | Δ IS (pp) | Δ OOS (pp) | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|
| Amsterdam, NL | 25 | median | 44.00% | 46.15% | additive | 48.00% | 53.85% | +4.00 | +7.69 | WEIGHTED BETTER |
| Ankara, TR | 25 | additive | 72.00% | 38.46% | additive | 76.00% | 53.85% | +4.00 | +15.38 | WEIGHTED BETTER |
| Atlanta, US | 25 | linear | 72.00% | 23.08% | linear | 68.00% | 53.85% | -4.00 | +30.77 | WEIGHTED BETTER |
| Austin, US | 25 | additive | 68.00% | 69.23% | multiplicative | 80.00% | 76.92% | +12.00 | +7.69 | WEIGHTED BETTER |
| Beijing, CN | 21 | linear | 57.14% | 27.27% | additive | 80.95% | 72.73% | +23.81 | +45.45 | WEIGHTED BETTER |
| Buenos Aires, AR | 25 | multiplicative | 44.00% | 46.15% | linear | 60.00% | 46.15% | +16.00 | +0.00 | TIE |
| Busan, KR | 21 | additive | 47.62% | 45.45% | linear | 52.38% | 45.45% | +4.76 | +0.00 | TIE |
| Cape Town, ZA | 24 | linear | 41.67% | 66.67% | baseline | 54.17% | 58.33% | +12.50 | -8.33 | EQUAL BETTER |
| Chengdu, CN | 20 | additive | 30.00% | 40.00% | linear | 30.00% | 10.00% | +0.00 | -30.00 | EQUAL BETTER |
| Chicago, US | 25 | median | 56.00% | 15.38% | baseline | 56.00% | 46.15% | +0.00 | +30.77 | WEIGHTED BETTER |
| Chongqing, CN | 20 | additive | 55.00% | 50.00% | additive | 75.00% | 50.00% | +20.00 | +0.00 | TIE |
| Dallas, US | 24 | linear | 75.00% | 25.00% | median | 79.17% | 66.67% | +4.17 | +41.67 | WEIGHTED BETTER |
| Denver, US | 25 | median | 60.00% | 53.85% | multiplicative | 60.00% | 46.15% | +0.00 | -7.69 | EQUAL BETTER |
| Guangzhou, CN | 20 | additive | 35.00% | 20.00% | additive | 40.00% | 40.00% | +5.00 | +20.00 | WEIGHTED BETTER |
| Helsinki, FI | 25 | additive | 44.00% | 53.85% | baseline | 56.00% | 46.15% | +12.00 | -7.69 | EQUAL BETTER |
| Hong Kong, HK | 21 | median | 52.38% | 36.36% | linear | 57.14% | 45.45% | +4.76 | +9.09 | WEIGHTED BETTER |
| Houston, US | 25 | linear | 68.00% | 53.85% | additive | 80.00% | 69.23% | +12.00 | +15.38 | WEIGHTED BETTER |
| Istanbul, TR | 25 | median | 68.00% | 76.92% | median | 56.00% | 69.23% | -12.00 | -7.69 | EQUAL BETTER |
| Jeddah, SA | 22 | additive | 45.45% | 27.27% | additive | 54.55% | 36.36% | +9.09 | +9.09 | WEIGHTED BETTER |
| Jinan, CN | 0 | — | — | — | — | — | — | — | — | too little data |
| Karachi, PK | 24 | linear | 58.33% | 50.00% | linear | 66.67% | 66.67% | +8.33 | +16.67 | WEIGHTED BETTER |
| Kuala Lumpur, MY | 21 | linear | 61.90% | 63.64% | linear | 71.43% | 81.82% | +9.52 | +18.18 | WEIGHTED BETTER |
| London, UK | 24 | median | 45.83% | 50.00% | median | 50.00% | 33.33% | +4.17 | -16.67 | EQUAL BETTER |
| Los Angeles, US | 24 | linear | 58.33% | 41.67% | linear | 58.33% | 58.33% | +0.00 | +16.67 | WEIGHTED BETTER |
| Lucknow, IN | 24 | linear | 58.33% | 75.00% | additive | 66.67% | 83.33% | +8.33 | +8.33 | WEIGHTED BETTER |
| Madrid, ES | 24 | additive | 50.00% | 33.33% | multiplicative | 58.33% | 33.33% | +8.33 | +0.00 | TIE |
| Manila, PH | 21 | additive | 42.86% | 9.09% | additive | 47.62% | 54.55% | +4.76 | +45.45 | WEIGHTED BETTER |
| Mexico City, MX | 23 | median | 56.52% | 41.67% | baseline | 65.22% | 58.33% | +8.70 | +16.67 | WEIGHTED BETTER |
| Miami, US | 25 | median | 60.00% | 23.08% | additive | 52.00% | 38.46% | -8.00 | +15.38 | WEIGHTED BETTER |
| Milan, IT | 24 | linear | 70.83% | 66.67% | median | 75.00% | 66.67% | +4.17 | +0.00 | TIE |
| Moscow, RU | 25 | median | 60.00% | 30.77% | additive | 56.00% | 46.15% | -4.00 | +15.38 | WEIGHTED BETTER |
| Munich, DE | 25 | multiplicative | 60.00% | 53.85% | multiplicative | 64.00% | 69.23% | +4.00 | +15.38 | WEIGHTED BETTER |
| New York, US | 25 | baseline | 56.00% | 46.15% | baseline | 76.00% | 69.23% | +20.00 | +23.08 | WEIGHTED BETTER |
| Panama City, PA | 21 | median | 61.90% | 36.36% | median | 57.14% | 54.55% | -4.76 | +18.18 | WEIGHTED BETTER |
| Paris, FR | 24 | baseline | 37.50% | 50.00% | baseline | 45.83% | 41.67% | +8.33 | -8.33 | EQUAL BETTER |
| Qingdao, CN | 21 | additive | 52.38% | 45.45% | baseline | 57.14% | 63.64% | +4.76 | +18.18 | WEIGHTED BETTER |
| San Francisco, US | 25 | median | 44.00% | 61.54% | median | 40.00% | 46.15% | -4.00 | -15.38 | EQUAL BETTER |
| Sao Paulo, BR | 24 | linear | 45.83% | 33.33% | linear | 50.00% | 25.00% | +4.17 | -8.33 | EQUAL BETTER |
| Seattle, US | 25 | additive | 44.00% | 38.46% | linear | 56.00% | 38.46% | +12.00 | +0.00 | TIE |
| Seoul (Incheon), KR | 21 | linear | 52.38% | 18.18% | linear | 57.14% | 72.73% | +4.76 | +54.55 | WEIGHTED BETTER |
| Shanghai, CN | 21 | linear | 71.43% | 63.64% | median | 71.43% | 45.45% | +0.00 | -18.18 | EQUAL BETTER |
| Shenzhen, CN | 21 | median | 57.14% | 63.64% | linear | 52.38% | 36.36% | -4.76 | -27.27 | EQUAL BETTER |
| Singapore, SG | 21 | linear | 76.19% | 72.73% | additive | 66.67% | 81.82% | -9.52 | +9.09 | WEIGHTED BETTER |
| Taipei, TW | 21 | median | 42.86% | 18.18% | linear | 47.62% | 9.09% | +4.76 | -9.09 | EQUAL BETTER |
| Tel Aviv, IL | 25 | median | 76.00% | 76.92% | linear | 80.00% | 69.23% | +4.00 | -7.69 | EQUAL BETTER |
| Tokyo, JP | 21 | baseline | 47.62% | 54.55% | baseline | 61.90% | 72.73% | +14.29 | +18.18 | WEIGHTED BETTER |
| Toronto, CA | 25 | baseline | 56.00% | 61.54% | linear | 56.00% | 46.15% | +0.00 | -15.38 | EQUAL BETTER |
| Warsaw, PL | 23 | baseline | 65.22% | 83.33% | additive | 73.91% | 75.00% | +8.70 | -8.33 | EQUAL BETTER |
| Wellington, NZ | 24 | multiplicative | 41.67% | 50.00% | linear | 62.50% | 41.67% | +20.83 | -8.33 | EQUAL BETTER |
| Wuhan, CN | 20 | additive | 35.00% | 30.00% | additive | 45.00% | 30.00% | +10.00 | +0.00 | TIE |
| Zhengzhou, CN | 10 | median | 60.00% | 60.00% | additive | 70.00% | 80.00% | +10.00 | +20.00 | WEIGHTED BETTER |

**Per-city verdicts:** weighted better in **27** cities, equal-weight better in **16**, tie in **7** (out-of-sample).
