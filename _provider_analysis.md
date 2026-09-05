# Provider-Level Analysis — v1 Polymarket Weather

> **Data note.** `_model_quality_log.json` stores only the BMA aggregate (`bma_mean`, `bma_std`, `p5`, `p95`, `bma_probs`) and a `models` **count** — it does **not** persist the 8 individual NWP provider forecasts. Provider forecasts used here were therefore reconstructed from Open-Meteo's per-model archives (`past_days` on the forecast API), the closest available proxy for the system's 0-lead (same-day) daily-max forecasts.

The 8 providers (from [`ensemble.py`](src/strategies/weather/ensemble.py:105) `MODEL_DEFINITIONS`): ECMWF IFS, GFS, ICON, GEM, UKMO, JMA, HRRR (CONUS only), AIFS.

**Metrics per provider per city** (computed over days where that provider has a value and the market resolved):
- `n` = resolved days; `closest` = days the provider had the minimum |error| (ties credited to each tied provider)
- `bias` = mean(forecast − resolved) °C; `median`, `MAD`, `std` = °C of daily error
- `sign agree` = share of days on the dominant side of resolution (≈100% ⇒ consistently one-sided; ≈50% ⇒ flips around resolution)

**Classification rules:** BEST = most closest-days (tie-broken by MAD, then |bias|); MISSER = |bias| ≥ 1.0 °C and sign-agreement ≥ 70%; OSCILLATOR = std ≥ 1.0 °C and sign-agreement in 45–55%.

**Weighting:** inverse-MSE (`1/(bias² + var + 0.25)`) per provider, normalised; oscillators penalised ×0.2; global floor 0.02 so no provider is fully zeroed.

Cities analysed: **51**  ·  resolved (city,date) records: **1158**

---

## Amsterdam, NL

- **BEST:** GEM, ICON
- **Consistent missers:** JMA, AIFS
- **Oscillators:** GFS

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 4 | -0.95 | -0.80 | 0.60 | 0.79 | 92% | — | 0.161 |
| GFS | 25 | 7 | +0.07 | -0.10 | 0.90 | 1.16 | 52% | OSC | 0.036 |
| ICON | 25 | 9 | -0.58 | -0.40 | 0.50 | 0.82 | 72% | BEST | 0.225 |
| GEM | 25 | 9 | -0.50 | -0.40 | 0.50 | 0.87 | 76% | BEST | 0.229 |
| UKMO | 25 | 4 | -0.47 | -0.40 | 0.70 | 0.80 | 60% | — | 0.255 |
| JMA | 25 | 0 | -2.38 | -2.10 | 0.90 | 1.32 | 100% | MISSER | 0.037 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 1 | -1.90 | -1.60 | 0.70 | 1.09 | 100% | MISSER | 0.057 |

## Ankara, TR

- **BEST:** ECMWF IFS
- **Consistent missers:** GFS, ICON, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 13 | -0.67 | -0.70 | 0.30 | 0.61 | 80% | BEST | 0.254 |
| GFS | 25 | 5 | -1.08 | -1.10 | 0.40 | 0.55 | 96% | MISSER | 0.158 |
| ICON | 25 | 5 | -1.12 | -1.10 | 0.30 | 0.56 | 96% | MISSER | 0.151 |
| GEM | 25 | 7 | -0.84 | -0.80 | 0.40 | 0.53 | 96% | — | 0.222 |
| UKMO | 25 | 0 | -1.70 | -1.60 | 0.20 | 0.86 | 100% | MISSER | 0.070 |
| JMA | 25 | 1 | -1.67 | -1.70 | 0.50 | 0.67 | 100% | MISSER | 0.078 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 0 | -1.78 | -1.60 | 0.50 | 0.85 | 100% | MISSER | 0.066 |

## Atlanta, US

- **BEST:** GFS, HRRR
- **Consistent missers:** GEM, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 3 | -0.90 | -0.91 | 0.71 | 1.09 | 72% | — | 0.090 |
| GFS | 25 | 12 | -0.10 | -0.11 | 0.58 | 0.72 | 56% | BEST | 0.262 |
| ICON | 25 | 6 | -0.66 | -0.51 | 0.41 | 0.76 | 88% | — | 0.160 |
| GEM | 25 | 0 | -1.21 | -1.11 | 0.58 | 1.11 | 84% | MISSER | 0.068 |
| UKMO | 25 | 3 | -1.04 | -0.91 | 0.41 | 0.80 | 100% | MISSER | 0.102 |
| JMA | 25 | 1 | -2.94 | -2.90 | 1.01 | 1.39 | 96% | MISSER | 0.020 |
| HRRR | 25 | 12 | -0.10 | -0.11 | 0.58 | 0.72 | 56% | BEST | 0.262 |
| AIFS | 25 | 0 | -2.02 | -2.01 | 0.71 | 1.10 | 100% | MISSER | 0.037 |

## Austin, US

- **BEST:** ICON
- **Consistent missers:** JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 4 | -0.97 | -0.97 | 0.40 | 1.06 | 80% | — | 0.077 |
| GFS | 25 | 7 | -0.64 | -0.76 | 0.41 | 0.51 | 84% | — | 0.195 |
| ICON | 25 | 9 | -0.02 | +0.26 | 0.38 | 0.94 | 60% | BEST | 0.156 |
| GEM | 25 | 3 | -0.83 | -0.74 | 0.47 | 0.73 | 88% | — | 0.121 |
| UKMO | 25 | 4 | -0.70 | -0.67 | 0.31 | 0.51 | 96% | — | 0.178 |
| JMA | 25 | 0 | -2.84 | -2.38 | 0.60 | 1.20 | 100% | MISSER | 0.020 |
| HRRR | 25 | 7 | -0.64 | -0.76 | 0.41 | 0.51 | 84% | — | 0.195 |
| AIFS | 25 | 1 | -1.45 | -1.44 | 0.53 | 0.83 | 100% | MISSER | 0.058 |

## Beijing, CN

- **BEST:** JMA
- **Consistent missers:** ICON, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 3 | -0.98 | -0.80 | 0.80 | 1.00 | 76% | — | 0.137 |
| GFS | 21 | 5 | -0.48 | -0.40 | 0.80 | 1.48 | 62% | — | 0.114 |
| ICON | 21 | 1 | -1.18 | -1.00 | 0.30 | 0.78 | 95% | MISSER | 0.136 |
| GEM | 21 | 5 | +0.35 | +0.50 | 0.70 | 1.11 | 57% | — | 0.191 |
| UKMO | 21 | 2 | -0.88 | -0.90 | 0.60 | 0.84 | 76% | — | 0.178 |
| JMA | 21 | 8 | -0.90 | -0.60 | 0.60 | 1.18 | 71% | BEST | 0.124 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 1 | -1.06 | -0.90 | 0.70 | 1.08 | 81% | MISSER | 0.120 |

## Buenos Aires, AR

- **BEST:** ECMWF IFS, GEM
- **Consistent missers:** AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 6 | -0.35 | -0.60 | 0.50 | 0.80 | 64% | BEST | 0.248 |
| GFS | 25 | 4 | -0.73 | -0.80 | 0.80 | 1.39 | 72% | — | 0.093 |
| ICON | 25 | 4 | -0.88 | -1.00 | 0.40 | 0.62 | 88% | — | 0.178 |
| GEM | 25 | 6 | +0.44 | +0.50 | 1.00 | 1.27 | 64% | BEST | 0.122 |
| UKMO | 25 | 3 | -0.71 | -0.70 | 0.40 | 0.85 | 80% | — | 0.170 |
| JMA | 25 | 5 | -0.99 | -0.80 | 1.00 | 1.44 | 64% | — | 0.076 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 1 | -1.10 | -1.20 | 0.50 | 0.86 | 84% | MISSER | 0.113 |

## Busan, KR

- **BEST:** GFS
- **Consistent missers:** ECMWF IFS, ICON, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 3 | -1.35 | -1.10 | 0.90 | 1.15 | 90% | MISSER | 0.116 |
| GFS | 21 | 7 | -0.71 | -0.50 | 0.50 | 0.91 | 76% | BEST | 0.246 |
| ICON | 21 | 1 | -2.34 | -2.30 | 0.50 | 1.05 | 95% | MISSER | 0.057 |
| GEM | 21 | 4 | -0.99 | -1.10 | 0.70 | 0.85 | 86% | — | 0.203 |
| UKMO | 21 | 5 | -1.20 | -1.30 | 0.60 | 0.95 | 90% | MISSER | 0.152 |
| JMA | 21 | 0 | -1.84 | -1.70 | 0.60 | 0.92 | 100% | MISSER | 0.088 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 4 | -1.23 | -1.50 | 0.60 | 1.05 | 86% | MISSER | 0.138 |

## Cape Town, ZA

- **BEST:** ECMWF IFS
- **Consistent missers:** JMA, AIFS
- **Oscillators:** ECMWF IFS, GFS, ICON

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 8 | -0.10 | -0.05 | 0.40 | 1.03 | 50% | OSC | 0.066 |
| GFS | 24 | 2 | +0.59 | +0.20 | 1.00 | 1.69 | 54% | OSC | 0.025 |
| ICON | 24 | 4 | -0.15 | -0.20 | 0.95 | 1.30 | 50% | OSC | 0.044 |
| GEM | 24 | 5 | +0.96 | +0.80 | 0.50 | 0.92 | 83% | — | 0.216 |
| UKMO | 24 | 7 | +0.10 | +0.10 | 0.55 | 0.77 | 54% | — | 0.508 |
| JMA | 24 | 0 | -2.07 | -2.00 | 0.90 | 1.06 | 96% | MISSER | 0.077 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 3 | -2.17 | -2.00 | 1.00 | 1.41 | 96% | MISSER | 0.063 |

## Chengdu, CN

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS, GFS, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 20 | 2 | -2.22 | -2.60 | 1.05 | 1.71 | 85% | MISSER | 0.090 |
| GFS | 20 | 3 | -1.11 | -1.20 | 1.15 | 1.62 | 75% | MISSER | 0.178 |
| ICON | 20 | 10 | -0.62 | -0.30 | 1.00 | 1.69 | 65% | BEST | 0.210 |
| GEM | 20 | 3 | -1.14 | -0.75 | 1.30 | 1.88 | 65% | — | 0.144 |
| UKMO | 20 | 3 | -0.88 | -1.25 | 1.05 | 1.69 | 70% | — | 0.188 |
| JMA | 20 | 2 | -2.14 | -2.05 | 1.45 | 2.10 | 95% | MISSER | 0.079 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 20 | 0 | -2.10 | -2.35 | 0.75 | 1.37 | 90% | MISSER | 0.112 |

## Chicago, US

- **BEST:** GFS, HRRR
- **Consistent missers:** —
- **Oscillators:** UKMO

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 5 | -0.41 | -0.44 | 0.51 | 0.92 | 64% | — | 0.149 |
| GFS | 25 | 8 | +0.37 | +0.19 | 0.69 | 0.83 | 64% | BEST | 0.176 |
| ICON | 25 | 4 | +0.09 | +0.23 | 0.66 | 0.88 | 56% | — | 0.184 |
| GEM | 25 | 4 | -0.68 | -0.70 | 0.56 | 0.95 | 80% | — | 0.117 |
| UKMO | 25 | 3 | -0.07 | -0.06 | 0.57 | 1.14 | 52% | OSC | 0.024 |
| JMA | 25 | 3 | -0.43 | -0.42 | 0.85 | 1.36 | 60% | — | 0.083 |
| HRRR | 25 | 8 | +0.37 | +0.19 | 0.69 | 0.83 | 64% | BEST | 0.176 |
| AIFS | 25 | 3 | -0.96 | -0.94 | 0.58 | 0.95 | 80% | — | 0.091 |

## Chongqing, CN

- **BEST:** ICON, GEM
- **Consistent missers:** ECMWF IFS, GFS, UKMO, JMA, AIFS
- **Oscillators:** GEM

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 20 | 2 | -1.42 | -1.45 | 0.30 | 0.87 | 90% | MISSER | 0.161 |
| GFS | 20 | 4 | -1.61 | -1.55 | 0.60 | 1.02 | 95% | MISSER | 0.126 |
| ICON | 20 | 7 | -0.84 | -0.75 | 0.40 | 0.57 | 95% | BEST | 0.381 |
| GEM | 20 | 7 | -0.74 | -0.35 | 0.95 | 1.54 | 55% | OSC | 0.031 |
| UKMO | 20 | 0 | -2.44 | -2.50 | 0.45 | 0.92 | 100% | MISSER | 0.069 |
| JMA | 20 | 0 | -2.59 | -2.65 | 1.00 | 1.43 | 95% | MISSER | 0.054 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 20 | 1 | -1.28 | -1.40 | 0.55 | 0.92 | 90% | MISSER | 0.178 |

## Dallas, US

- **BEST:** JMA, ECMWF IFS
- **Consistent missers:** —
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 6 | +0.19 | +0.30 | 0.46 | 0.70 | 67% | BEST | 0.159 |
| GFS | 24 | 2 | +0.92 | +0.98 | 0.23 | 0.46 | 96% | — | 0.095 |
| ICON | 24 | 5 | +0.11 | +0.29 | 0.35 | 0.75 | 67% | — | 0.150 |
| GEM | 24 | 3 | -0.39 | -0.47 | 0.39 | 0.87 | 79% | — | 0.107 |
| UKMO | 24 | 3 | +0.32 | +0.23 | 0.71 | 0.79 | 58% | — | 0.128 |
| JMA | 24 | 6 | -0.44 | -0.36 | 0.43 | 0.78 | 62% | BEST | 0.117 |
| HRRR | 24 | 2 | +0.92 | +0.98 | 0.23 | 0.46 | 96% | — | 0.095 |
| AIFS | 24 | 5 | -0.35 | -0.33 | 0.41 | 0.67 | 58% | — | 0.149 |

## Denver, US

- **BEST:** GFS, HRRR
- **Consistent missers:** ICON, GEM, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 8 | -0.97 | -1.09 | 0.71 | 1.00 | 84% | — | 0.110 |
| GFS | 25 | 11 | -0.35 | -0.39 | 0.49 | 0.77 | 68% | BEST | 0.249 |
| ICON | 25 | 1 | -1.25 | -1.50 | 0.74 | 1.19 | 84% | MISSER | 0.075 |
| GEM | 25 | 4 | -1.35 | -1.42 | 0.82 | 1.30 | 92% | MISSER | 0.064 |
| UKMO | 25 | 3 | -0.37 | -0.51 | 0.80 | 1.04 | 68% | — | 0.165 |
| JMA | 25 | 0 | -1.94 | -1.91 | 0.83 | 1.29 | 96% | MISSER | 0.042 |
| HRRR | 25 | 11 | -0.35 | -0.39 | 0.49 | 0.77 | 68% | BEST | 0.249 |
| AIFS | 25 | 0 | -1.92 | -1.91 | 0.60 | 1.12 | 96% | MISSER | 0.046 |

## Guangzhou, CN

- **BEST:** GFS
- **Consistent missers:** ECMWF IFS, GFS, ICON, GEM, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 20 | 3 | -1.74 | -2.00 | 0.95 | 1.19 | 95% | MISSER | 0.168 |
| GFS | 20 | 8 | -1.09 | -1.20 | 0.90 | 1.37 | 80% | MISSER | 0.238 |
| ICON | 20 | 2 | -1.95 | -1.85 | 0.95 | 1.16 | 100% | MISSER | 0.146 |
| GEM | 20 | 5 | -1.57 | -1.15 | 1.05 | 1.67 | 85% | MISSER | 0.142 |
| UKMO | 20 | 0 | -2.26 | -2.00 | 0.85 | 1.34 | 95% | MISSER | 0.110 |
| JMA | 20 | 0 | -3.16 | -3.30 | 1.10 | 1.54 | 95% | MISSER | 0.062 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 20 | 4 | -1.79 | -1.70 | 0.85 | 1.54 | 85% | MISSER | 0.134 |

## Helsinki, FI

- **BEST:** UKMO
- **Consistent missers:** AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 3 | -0.79 | -0.90 | 0.60 | 1.02 | 76% | — | 0.090 |
| GFS | 25 | 6 | +0.36 | +0.40 | 0.60 | 0.82 | 56% | — | 0.164 |
| ICON | 25 | 5 | -0.45 | -0.40 | 0.50 | 0.67 | 64% | — | 0.189 |
| GEM | 25 | 4 | -0.62 | -0.70 | 0.70 | 0.67 | 68% | — | 0.157 |
| UKMO | 25 | 7 | -0.24 | -0.10 | 0.60 | 0.83 | 52% | BEST | 0.173 |
| JMA | 25 | 4 | -0.15 | -0.10 | 0.60 | 0.90 | 52% | — | 0.158 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 0 | -1.20 | -1.30 | 0.70 | 0.87 | 92% | MISSER | 0.070 |

## Hong Kong, HK

- **BEST:** UKMO
- **Consistent missers:** AIFS
- **Oscillators:** UKMO

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 7 | -0.45 | -0.50 | 0.60 | 0.82 | 62% | — | 0.242 |
| GFS | 21 | 4 | -0.47 | -0.40 | 0.70 | 1.07 | 62% | — | 0.168 |
| ICON | 21 | 1 | -0.83 | -0.90 | 0.50 | 0.75 | 86% | — | 0.182 |
| GEM | 21 | 2 | -0.58 | -0.90 | 0.80 | 1.13 | 67% | — | 0.146 |
| UKMO | 21 | 8 | -0.07 | -0.10 | 0.80 | 1.12 | 52% | OSC | 0.036 |
| JMA | 21 | 1 | -0.97 | -0.70 | 0.50 | 1.01 | 90% | — | 0.123 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 1 | -1.18 | -1.00 | 0.70 | 1.00 | 86% | MISSER | 0.103 |

## Houston, US

- **BEST:** GEM
- **Consistent missers:** JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 4 | -0.30 | +0.28 | 0.81 | 1.48 | 56% | — | 0.084 |
| GFS | 25 | 1 | +0.85 | +0.76 | 0.38 | 0.56 | 96% | — | 0.164 |
| ICON | 25 | 3 | +0.58 | +0.66 | 0.61 | 0.98 | 80% | — | 0.137 |
| GEM | 25 | 9 | +0.31 | +0.28 | 0.58 | 0.87 | 68% | BEST | 0.193 |
| UKMO | 25 | 4 | +0.82 | +0.98 | 0.54 | 0.98 | 76% | — | 0.112 |
| JMA | 25 | 0 | -2.12 | -2.11 | 0.55 | 1.10 | 96% | MISSER | 0.035 |
| HRRR | 25 | 1 | +0.85 | +0.76 | 0.38 | 0.56 | 96% | — | 0.164 |
| AIFS | 25 | 6 | -0.95 | -0.82 | 0.31 | 0.88 | 96% | — | 0.110 |

## Istanbul, TR

- **BEST:** ICON, GEM
- **Consistent missers:** GFS, UKMO, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 3 | +0.15 | +0.40 | 0.50 | 0.89 | 64% | — | 0.228 |
| GFS | 25 | 3 | +1.63 | +1.70 | 0.70 | 1.09 | 92% | MISSER | 0.059 |
| ICON | 25 | 8 | -0.63 | -0.50 | 0.40 | 0.87 | 84% | BEST | 0.171 |
| GEM | 25 | 8 | +0.11 | +0.30 | 0.50 | 0.85 | 64% | BEST | 0.245 |
| UKMO | 25 | 1 | -2.67 | -2.40 | 0.50 | 1.35 | 96% | MISSER | 0.026 |
| JMA | 25 | 0 | -2.72 | -2.70 | 0.60 | 1.03 | 100% | MISSER | 0.028 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 7 | +0.16 | +0.40 | 0.50 | 0.84 | 64% | — | 0.243 |

## Jeddah, SA

- **BEST:** ICON, GEM
- **Consistent missers:** ECMWF IFS, JMA
- **Oscillators:** GFS

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 22 | 1 | +2.45 | +2.30 | 0.60 | 0.96 | 95% | MISSER | 0.053 |
| GFS | 22 | 4 | -0.18 | -0.20 | 0.90 | 1.20 | 55% | OSC | 0.044 |
| ICON | 22 | 7 | +0.47 | +0.50 | 0.65 | 1.02 | 68% | BEST | 0.251 |
| GEM | 22 | 7 | -0.85 | -0.80 | 0.85 | 1.22 | 73% | BEST | 0.154 |
| UKMO | 22 | 3 | +0.77 | +0.85 | 0.85 | 1.15 | 68% | — | 0.175 |
| JMA | 22 | 0 | -5.76 | -5.75 | 0.80 | 1.23 | 100% | MISSER | 0.020 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 22 | 2 | +0.44 | +0.60 | 0.30 | 0.89 | 77% | — | 0.304 |

## Jinan, CN

- **BEST:** ECMWF IFS, UKMO
- **Consistent missers:** —
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 1 | 1 | +0.00 | +0.00 | 0.00 | — | 0% | BEST | 0.000 |
| GFS | 1 | 0 | -0.70 | -0.70 | 0.00 | — | 100% | — | 0.000 |
| ICON | 1 | 0 | -0.60 | -0.60 | 0.00 | — | 100% | — | 0.000 |
| GEM | 1 | 0 | +0.20 | +0.20 | 0.00 | — | 100% | — | 0.000 |
| UKMO | 1 | 1 | +0.00 | +0.00 | 0.00 | — | 0% | BEST | 0.000 |
| JMA | 1 | 0 | -0.30 | -0.30 | 0.00 | — | 100% | — | 0.000 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 1 | 0 | -0.50 | -0.50 | 0.00 | — | 100% | — | 0.000 |

## Karachi, PK

- **BEST:** ICON
- **Consistent missers:** GEM, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 6 | +0.80 | +0.70 | 0.30 | 0.74 | 88% | — | 0.168 |
| GFS | 24 | 6 | -0.50 | -0.50 | 0.70 | 0.87 | 71% | — | 0.193 |
| ICON | 24 | 9 | +0.27 | +0.30 | 0.45 | 0.70 | 71% | BEST | 0.298 |
| GEM | 24 | 4 | -1.03 | -0.70 | 0.35 | 0.92 | 96% | MISSER | 0.112 |
| UKMO | 24 | 2 | +1.28 | +1.20 | 0.55 | 0.86 | 96% | MISSER | 0.092 |
| JMA | 24 | 2 | -1.60 | -1.75 | 0.55 | 0.81 | 100% | MISSER | 0.070 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 0 | -1.71 | -1.60 | 0.50 | 0.62 | 100% | MISSER | 0.068 |

## Kuala Lumpur, MY

- **BEST:** ECMWF IFS, UKMO, ICON
- **Consistent missers:** GFS, GEM, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 6 | -0.62 | -0.30 | 0.40 | 0.88 | 71% | BEST | 0.183 |
| GFS | 21 | 1 | -1.80 | -1.70 | 0.40 | 0.68 | 100% | MISSER | 0.066 |
| ICON | 21 | 6 | -0.44 | -0.30 | 0.50 | 0.81 | 62% | BEST | 0.235 |
| GEM | 21 | 1 | -1.68 | -1.70 | 0.90 | 1.11 | 100% | MISSER | 0.061 |
| UKMO | 21 | 6 | -0.38 | -0.30 | 0.50 | 0.67 | 62% | BEST | 0.306 |
| JMA | 21 | 0 | -4.26 | -4.10 | 0.60 | 0.67 | 100% | MISSER | 0.020 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 2 | -0.90 | -0.70 | 0.50 | 0.98 | 81% | — | 0.129 |

## London, UK

- **BEST:** UKMO
- **Consistent missers:** JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 2 | -0.44 | -0.30 | 1.00 | 1.27 | 67% | — | 0.107 |
| GFS | 24 | 7 | +0.00 | +0.05 | 0.70 | 0.89 | 50% | — | 0.214 |
| ICON | 24 | 1 | -0.17 | -0.20 | 0.80 | 0.96 | 62% | — | 0.184 |
| GEM | 24 | 7 | +0.30 | +0.20 | 0.65 | 1.02 | 58% | — | 0.159 |
| UKMO | 24 | 8 | -0.65 | -0.60 | 0.60 | 0.74 | 71% | BEST | 0.181 |
| JMA | 24 | 2 | -1.14 | -1.60 | 0.70 | 1.16 | 79% | MISSER | 0.077 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 1 | -0.96 | -0.90 | 0.90 | 1.28 | 79% | — | 0.078 |

## Los Angeles, US

- **BEST:** GEM
- **Consistent missers:** ECMWF IFS, ICON, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 0 | +5.67 | +5.78 | 1.50 | 1.82 | 100% | MISSER | 0.020 |
| GFS | 24 | 8 | +0.41 | +0.33 | 0.39 | 0.50 | 71% | — | 0.275 |
| ICON | 24 | 2 | +2.28 | +2.08 | 0.52 | 1.13 | 100% | MISSER | 0.027 |
| GEM | 24 | 10 | +0.18 | +0.21 | 0.25 | 0.86 | 79% | BEST | 0.180 |
| UKMO | 24 | 4 | +0.00 | -0.20 | 0.65 | 0.98 | 54% | — | 0.153 |
| JMA | 24 | 1 | +0.86 | +1.08 | 1.43 | 1.66 | 71% | — | 0.049 |
| HRRR | 24 | 8 | +0.41 | +0.33 | 0.39 | 0.50 | 71% | — | 0.275 |
| AIFS | 24 | 0 | +3.85 | +4.00 | 1.34 | 1.74 | 100% | MISSER | 0.020 |

## Lucknow, IN

- **BEST:** AIFS
- **Consistent missers:** GFS, GEM, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 6 | -0.74 | -0.70 | 0.45 | 0.92 | 79% | — | 0.177 |
| GFS | 24 | 3 | +1.49 | +1.70 | 1.05 | 1.71 | 79% | MISSER | 0.054 |
| ICON | 24 | 4 | -0.69 | -0.60 | 0.60 | 1.02 | 75% | — | 0.165 |
| GEM | 24 | 2 | -1.18 | -1.30 | 0.85 | 1.36 | 79% | MISSER | 0.084 |
| UKMO | 24 | 2 | +0.70 | +0.90 | 0.50 | 0.93 | 79% | — | 0.182 |
| JMA | 24 | 1 | -2.24 | -2.15 | 0.85 | 1.22 | 92% | MISSER | 0.043 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 10 | -0.58 | -0.45 | 0.45 | 0.63 | 79% | BEST | 0.295 |

## Madrid, ES

- **BEST:** UKMO
- **Consistent missers:** GEM, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 7 | -0.61 | -0.50 | 0.35 | 0.80 | 83% | — | 0.173 |
| GFS | 24 | 5 | -0.79 | -0.90 | 0.50 | 0.70 | 83% | — | 0.157 |
| ICON | 24 | 3 | -0.77 | -0.60 | 0.35 | 0.68 | 92% | — | 0.167 |
| GEM | 24 | 1 | -1.30 | -1.30 | 0.40 | 0.62 | 96% | MISSER | 0.094 |
| UKMO | 24 | 9 | -0.55 | -0.45 | 0.45 | 0.63 | 79% | BEST | 0.225 |
| JMA | 24 | 4 | -0.76 | -0.60 | 0.55 | 1.00 | 79% | — | 0.118 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 0 | -1.58 | -1.40 | 0.45 | 0.72 | 100% | MISSER | 0.066 |

## Manila, PH

- **BEST:** UKMO
- **Consistent missers:** ECMWF IFS, GEM, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 1 | -1.71 | -1.80 | 0.40 | 0.71 | 100% | MISSER | 0.081 |
| GFS | 21 | 4 | -0.72 | -0.80 | 0.70 | 1.31 | 81% | — | 0.121 |
| ICON | 21 | 6 | -0.55 | -0.60 | 0.60 | 0.88 | 76% | — | 0.227 |
| GEM | 21 | 0 | -2.11 | -2.30 | 0.60 | 1.03 | 95% | MISSER | 0.052 |
| UKMO | 21 | 10 | +0.02 | -0.20 | 0.80 | 0.87 | 52% | BEST | 0.298 |
| JMA | 21 | 3 | -0.91 | -1.00 | 0.50 | 1.04 | 86% | — | 0.138 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 0 | -1.68 | -1.70 | 0.40 | 0.70 | 100% | MISSER | 0.084 |

## Mexico City, MX

- **BEST:** GFS
- **Consistent missers:** JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 23 | 3 | +0.57 | +0.60 | 0.50 | 0.78 | 74% | — | 0.199 |
| GFS | 23 | 7 | +0.24 | +0.20 | 0.70 | 0.99 | 57% | BEST | 0.183 |
| ICON | 23 | 4 | +0.30 | +0.40 | 0.70 | 0.91 | 57% | — | 0.202 |
| GEM | 23 | 1 | -0.73 | -1.00 | 1.40 | 1.78 | 57% | — | 0.060 |
| UKMO | 23 | 5 | +0.56 | +0.40 | 0.50 | 0.98 | 61% | — | 0.155 |
| JMA | 23 | 5 | -1.67 | -1.90 | 0.90 | 1.24 | 87% | MISSER | 0.052 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 23 | 4 | -0.29 | -0.40 | 0.60 | 1.11 | 61% | — | 0.150 |

## Miami, US

- **BEST:** UKMO
- **Consistent missers:** ECMWF IFS, ICON, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 1 | -1.27 | -1.02 | 0.60 | 0.89 | 96% | MISSER | 0.092 |
| GFS | 25 | 4 | -0.88 | -0.81 | 0.40 | 0.74 | 92% | — | 0.157 |
| ICON | 25 | 3 | -1.02 | -1.11 | 0.40 | 0.74 | 88% | MISSER | 0.132 |
| GEM | 25 | 8 | -0.66 | -0.51 | 0.49 | 1.12 | 92% | — | 0.126 |
| UKMO | 25 | 9 | -0.12 | +0.08 | 0.70 | 0.93 | 52% | BEST | 0.215 |
| JMA | 25 | 2 | -0.88 | -1.11 | 1.09 | 1.25 | 64% | — | 0.094 |
| HRRR | 25 | 4 | -0.88 | -0.81 | 0.40 | 0.74 | 92% | — | 0.157 |
| AIFS | 25 | 0 | -2.78 | -2.51 | 0.50 | 1.12 | 96% | MISSER | 0.026 |

## Milan, IT

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS, JMA, AIFS
- **Oscillators:** GFS

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 4 | -1.10 | -1.10 | 0.65 | 0.98 | 83% | MISSER | 0.101 |
| GFS | 24 | 0 | +0.00 | -0.25 | 0.95 | 1.41 | 54% | OSC | 0.022 |
| ICON | 24 | 12 | -0.21 | -0.10 | 0.40 | 0.71 | 54% | BEST | 0.308 |
| GEM | 24 | 11 | -0.19 | -0.10 | 0.30 | 0.74 | 54% | — | 0.293 |
| UKMO | 24 | 2 | -0.78 | -0.60 | 0.30 | 0.55 | 96% | — | 0.211 |
| JMA | 24 | 1 | -2.49 | -2.55 | 0.95 | 1.26 | 100% | MISSER | 0.030 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 0 | -2.30 | -1.90 | 0.60 | 1.14 | 100% | MISSER | 0.036 |

## Moscow, RU

- **BEST:** UKMO
- **Consistent missers:** —
- **Oscillators:** GFS

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 5 | -0.31 | -0.40 | 0.50 | 0.78 | 60% | — | 0.177 |
| GFS | 25 | 5 | -0.00 | +0.00 | 0.70 | 1.15 | 48% | OSC | 0.022 |
| ICON | 25 | 4 | -0.58 | -0.70 | 0.50 | 0.77 | 76% | — | 0.143 |
| GEM | 25 | 6 | -0.03 | +0.00 | 0.70 | 0.77 | 48% | — | 0.201 |
| UKMO | 25 | 9 | +0.08 | +0.00 | 0.60 | 0.89 | 48% | BEST | 0.162 |
| JMA | 25 | 5 | +0.01 | +0.10 | 0.60 | 0.93 | 52% | — | 0.151 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 1 | -0.61 | -0.80 | 0.40 | 0.74 | 80% | — | 0.145 |

## Munich, DE

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS, GFS, ICON, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 3 | -1.82 | -1.60 | 0.70 | 1.19 | 92% | MISSER | 0.115 |
| GFS | 25 | 3 | -1.09 | -1.30 | 0.40 | 1.26 | 88% | MISSER | 0.189 |
| ICON | 25 | 9 | -1.33 | -0.90 | 0.40 | 0.92 | 100% | MISSER | 0.200 |
| GEM | 25 | 7 | -0.86 | -1.10 | 0.50 | 1.61 | 80% | — | 0.160 |
| UKMO | 25 | 4 | -1.29 | -1.50 | 0.50 | 1.27 | 84% | MISSER | 0.162 |
| JMA | 25 | 1 | -2.94 | -3.40 | 0.50 | 1.64 | 92% | MISSER | 0.049 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 5 | -1.80 | -1.40 | 0.50 | 1.06 | 100% | MISSER | 0.124 |

## New York, US

- **BEST:** ICON
- **Consistent missers:** AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 4 | -0.38 | -0.53 | 0.51 | 1.10 | 72% | — | 0.095 |
| GFS | 25 | 7 | +0.60 | +0.62 | 0.59 | 0.63 | 80% | — | 0.150 |
| ICON | 25 | 8 | +0.13 | +0.16 | 0.42 | 0.75 | 64% | BEST | 0.185 |
| GEM | 25 | 0 | -0.53 | -0.77 | 0.69 | 1.11 | 72% | — | 0.086 |
| UKMO | 25 | 5 | +0.30 | +0.24 | 0.78 | 0.79 | 68% | — | 0.158 |
| JMA | 25 | 3 | -0.32 | -0.58 | 0.49 | 0.81 | 68% | — | 0.151 |
| HRRR | 25 | 7 | +0.60 | +0.62 | 0.59 | 0.63 | 80% | — | 0.150 |
| AIFS | 25 | 1 | -2.20 | -2.18 | 0.69 | 0.94 | 100% | MISSER | 0.025 |

## Panama City, PA

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 1 | -1.11 | -1.20 | 0.50 | 1.41 | 86% | MISSER | 0.101 |
| GFS | 21 | 5 | -0.78 | -1.10 | 1.10 | 1.45 | 62% | — | 0.118 |
| ICON | 21 | 8 | +0.39 | +0.40 | 0.40 | 0.86 | 71% | BEST | 0.305 |
| GEM | 21 | 4 | -0.39 | -0.60 | 0.80 | 1.09 | 62% | — | 0.219 |
| UKMO | 21 | 3 | -0.72 | -0.80 | 1.10 | 1.30 | 67% | — | 0.141 |
| JMA | 21 | 0 | -2.20 | -2.20 | 0.40 | 1.04 | 95% | MISSER | 0.057 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 3 | -2.03 | -2.00 | 1.00 | 1.26 | 100% | MISSER | 0.059 |

## Paris, FR

- **BEST:** GEM
- **Consistent missers:** ECMWF IFS, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 2 | -1.05 | -0.90 | 0.40 | 0.85 | 88% | MISSER | 0.130 |
| GFS | 24 | 6 | +0.13 | +0.15 | 0.65 | 0.90 | 54% | — | 0.251 |
| ICON | 24 | 3 | -0.94 | -0.95 | 0.60 | 0.91 | 83% | — | 0.138 |
| GEM | 24 | 7 | -0.46 | -0.50 | 0.55 | 0.97 | 71% | BEST | 0.192 |
| UKMO | 24 | 4 | -1.28 | -1.25 | 0.75 | 0.85 | 100% | MISSER | 0.103 |
| JMA | 24 | 2 | -1.88 | -1.95 | 1.15 | 1.22 | 96% | MISSER | 0.051 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 2 | -1.03 | -0.95 | 0.60 | 0.82 | 96% | MISSER | 0.135 |

## Qingdao, CN

- **BEST:** GFS
- **Consistent missers:** JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 5 | -0.66 | -0.60 | 0.50 | 0.92 | 86% | — | 0.158 |
| GFS | 21 | 7 | +0.49 | +0.20 | 0.60 | 1.21 | 57% | BEST | 0.124 |
| ICON | 21 | 4 | -0.24 | -0.30 | 0.50 | 0.88 | 71% | — | 0.225 |
| GEM | 21 | 2 | -0.12 | -0.30 | 0.90 | 1.12 | 57% | — | 0.160 |
| UKMO | 21 | 4 | +0.30 | +0.30 | 0.50 | 1.04 | 71% | — | 0.172 |
| JMA | 21 | 1 | -1.67 | -1.70 | 0.60 | 1.15 | 90% | MISSER | 0.056 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 2 | -1.04 | -0.90 | 0.40 | 0.98 | 81% | MISSER | 0.106 |

## San Francisco, US

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS, GFS, GEM, JMA, HRRR
- **Oscillators:** ICON

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 3 | -1.72 | -1.89 | 0.73 | 1.32 | 88% | MISSER | 0.130 |
| GFS | 25 | 6 | +1.12 | +0.81 | 0.79 | 1.32 | 84% | MISSER | 0.198 |
| ICON | 25 | 9 | +0.53 | +0.11 | 0.63 | 1.53 | 52% | OSC | 0.045 |
| GEM | 25 | 2 | -2.21 | -2.19 | 0.82 | 1.19 | 100% | MISSER | 0.098 |
| UKMO | 25 | 3 | +1.18 | +1.12 | 1.51 | 2.02 | 64% | — | 0.113 |
| JMA | 25 | 0 | -3.50 | -3.38 | 0.79 | 1.42 | 100% | MISSER | 0.044 |
| HRRR | 25 | 6 | +1.12 | +0.81 | 0.79 | 1.32 | 84% | MISSER | 0.198 |
| AIFS | 25 | 4 | -0.96 | -1.39 | 0.91 | 1.57 | 72% | — | 0.176 |

## Sao Paulo, BR

- **BEST:** AIFS
- **Consistent missers:** UKMO, JMA
- **Oscillators:** GFS, GEM

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 3 | -0.44 | -0.60 | 0.40 | 1.12 | 71% | — | 0.268 |
| GFS | 24 | 6 | +0.20 | +0.35 | 0.75 | 1.17 | 54% | OSC | 0.055 |
| ICON | 24 | 3 | -0.38 | -0.70 | 0.80 | 1.52 | 67% | — | 0.169 |
| GEM | 24 | 3 | +0.25 | -0.15 | 1.25 | 2.33 | 50% | OSC | 0.020 |
| UKMO | 24 | 2 | -1.43 | -1.75 | 0.85 | 1.21 | 83% | MISSER | 0.121 |
| JMA | 24 | 3 | -1.87 | -2.00 | 1.10 | 1.70 | 88% | MISSER | 0.068 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 7 | -0.78 | -0.80 | 0.65 | 0.82 | 75% | BEST | 0.298 |

## Seattle, US

- **BEST:** GFS, HRRR
- **Consistent missers:** ICON, GEM, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 4 | -0.60 | -0.71 | 0.62 | 1.15 | 72% | — | 0.088 |
| GFS | 25 | 13 | -0.18 | -0.19 | 0.48 | 0.52 | 60% | BEST | 0.305 |
| ICON | 25 | 1 | -1.45 | -1.39 | 0.69 | 0.93 | 96% | MISSER | 0.053 |
| GEM | 25 | 1 | -1.29 | -1.43 | 0.44 | 0.88 | 96% | MISSER | 0.064 |
| UKMO | 25 | 7 | -0.18 | -0.18 | 0.45 | 1.04 | 60% | — | 0.125 |
| JMA | 25 | 2 | -1.43 | -1.63 | 1.00 | 1.51 | 84% | MISSER | 0.037 |
| HRRR | 25 | 13 | -0.18 | -0.19 | 0.48 | 0.52 | 60% | BEST | 0.305 |
| AIFS | 25 | 0 | -2.52 | -2.41 | 0.72 | 0.97 | 100% | MISSER | 0.023 |

## Seoul (Incheon), KR

- **BEST:** ICON
- **Consistent missers:** GFS, UKMO, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 3 | -0.61 | -0.40 | 0.60 | 0.84 | 71% | — | 0.211 |
| GFS | 21 | 0 | -2.62 | -2.80 | 0.90 | 1.38 | 95% | MISSER | 0.031 |
| ICON | 21 | 14 | -0.16 | -0.10 | 0.40 | 0.77 | 52% | BEST | 0.326 |
| GEM | 21 | 6 | -0.65 | -0.60 | 0.70 | 0.96 | 67% | — | 0.178 |
| UKMO | 21 | 1 | -1.88 | -2.10 | 0.80 | 1.42 | 95% | MISSER | 0.049 |
| JMA | 21 | 1 | -2.04 | -2.20 | 1.00 | 1.37 | 90% | MISSER | 0.045 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 0 | -0.97 | -1.10 | 0.50 | 0.75 | 86% | — | 0.161 |

## Shanghai, CN

- **BEST:** UKMO
- **Consistent missers:** ECMWF IFS, GFS, ICON, GEM, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 5 | -1.01 | -1.00 | 0.40 | 0.68 | 86% | MISSER | 0.151 |
| GFS | 21 | 1 | -1.09 | -1.20 | 0.60 | 0.89 | 90% | MISSER | 0.118 |
| ICON | 21 | 0 | -1.20 | -1.20 | 0.50 | 0.89 | 90% | MISSER | 0.106 |
| GEM | 21 | 5 | -1.01 | -1.20 | 0.50 | 0.80 | 81% | MISSER | 0.137 |
| UKMO | 21 | 10 | -0.36 | -0.40 | 0.50 | 0.67 | 52% | BEST | 0.316 |
| JMA | 21 | 0 | -1.70 | -2.20 | 0.70 | 1.45 | 90% | MISSER | 0.050 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 4 | -1.07 | -1.00 | 0.50 | 0.87 | 90% | MISSER | 0.122 |

## Shenzhen, CN

- **BEST:** UKMO
- **Consistent missers:** GFS, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 1 | -0.85 | -0.80 | 0.60 | 1.02 | 76% | — | 0.143 |
| GFS | 21 | 4 | +1.27 | +1.40 | 1.30 | 1.70 | 81% | MISSER | 0.061 |
| ICON | 21 | 1 | -0.98 | -1.10 | 0.40 | 0.93 | 81% | — | 0.140 |
| GEM | 21 | 4 | -0.42 | -0.30 | 0.80 | 1.06 | 67% | — | 0.186 |
| UKMO | 21 | 7 | +0.22 | +0.20 | 0.40 | 0.88 | 67% | BEST | 0.269 |
| JMA | 21 | 3 | -1.47 | -1.10 | 0.70 | 1.08 | 100% | MISSER | 0.081 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 3 | -1.13 | -1.20 | 0.70 | 0.94 | 86% | MISSER | 0.120 |

## Singapore, SG

- **BEST:** UKMO
- **Consistent missers:** GFS, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 1 | -0.83 | -0.70 | 0.30 | 0.65 | 90% | — | 0.154 |
| GFS | 21 | 0 | -1.01 | -1.20 | 0.20 | 0.78 | 95% | MISSER | 0.110 |
| ICON | 21 | 7 | -0.09 | +0.00 | 0.50 | 0.91 | 48% | — | 0.193 |
| GEM | 21 | 2 | -0.97 | -1.00 | 0.40 | 0.89 | 86% | — | 0.106 |
| UKMO | 21 | 11 | +0.26 | +0.00 | 0.30 | 0.76 | 48% | BEST | 0.232 |
| JMA | 21 | 0 | -2.76 | -2.90 | 0.40 | 0.66 | 100% | MISSER | 0.025 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 2 | -0.76 | -0.70 | 0.30 | 0.57 | 90% | — | 0.180 |

## Taipei, TW

- **BEST:** GFS
- **Consistent missers:** ECMWF IFS, GFS, ICON, GEM, UKMO, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 5 | -1.62 | -1.30 | 0.70 | 1.14 | 95% | MISSER | 0.205 |
| GFS | 21 | 8 | -1.16 | -1.40 | 1.10 | 1.53 | 76% | MISSER | 0.217 |
| ICON | 21 | 4 | -1.74 | -1.80 | 0.90 | 1.15 | 95% | MISSER | 0.186 |
| GEM | 21 | 0 | -2.86 | -3.10 | 0.90 | 1.03 | 100% | MISSER | 0.090 |
| UKMO | 21 | 5 | -1.72 | -1.60 | 0.50 | 1.13 | 95% | MISSER | 0.190 |
| JMA | 21 | 0 | -2.94 | -2.90 | 1.00 | 1.11 | 100% | MISSER | 0.084 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 0 | -5.20 | -4.50 | 1.50 | 1.77 | 100% | MISSER | 0.028 |

## Tel Aviv, IL

- **BEST:** ICON
- **Consistent missers:** ECMWF IFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 0 | +1.96 | +1.90 | 0.50 | 0.63 | 100% | MISSER | 0.028 |
| GFS | 25 | 8 | +0.26 | +0.20 | 0.30 | 0.61 | 72% | — | 0.183 |
| ICON | 25 | 10 | -0.22 | -0.30 | 0.40 | 0.60 | 60% | BEST | 0.194 |
| GEM | 25 | 9 | +0.19 | +0.10 | 0.40 | 0.56 | 56% | — | 0.211 |
| UKMO | 25 | 2 | +0.88 | +0.80 | 0.40 | 0.69 | 92% | — | 0.085 |
| JMA | 25 | 3 | +0.50 | +0.60 | 0.30 | 0.60 | 72% | — | 0.147 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 2 | +0.48 | +0.50 | 0.30 | 0.60 | 80% | — | 0.152 |

## Tokyo, JP

- **BEST:** UKMO
- **Consistent missers:** JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 21 | 3 | -0.04 | -0.30 | 0.50 | 1.38 | 67% | — | 0.138 |
| GFS | 21 | 5 | +0.15 | +0.20 | 0.70 | 1.16 | 57% | — | 0.185 |
| ICON | 21 | 3 | -0.80 | -0.70 | 0.60 | 1.14 | 76% | — | 0.135 |
| GEM | 21 | 2 | -0.57 | -0.60 | 1.00 | 1.31 | 67% | — | 0.131 |
| UKMO | 21 | 6 | +0.76 | +0.70 | 0.50 | 0.85 | 90% | BEST | 0.193 |
| JMA | 21 | 2 | -1.31 | -1.00 | 0.80 | 1.20 | 95% | MISSER | 0.087 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 21 | 4 | -0.80 | -0.40 | 0.50 | 1.18 | 71% | — | 0.131 |

## Toronto, CA

- **BEST:** UKMO
- **Consistent missers:** JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 25 | 7 | -0.23 | -0.30 | 0.50 | 1.01 | 56% | — | 0.157 |
| GFS | 25 | 6 | +0.02 | +0.10 | 0.60 | 0.72 | 52% | — | 0.270 |
| ICON | 25 | 6 | -0.16 | -0.20 | 0.60 | 0.96 | 52% | — | 0.176 |
| GEM | 25 | 6 | -0.80 | -0.60 | 0.40 | 0.94 | 84% | — | 0.118 |
| UKMO | 25 | 9 | +0.40 | +0.30 | 0.50 | 0.90 | 68% | BEST | 0.171 |
| JMA | 25 | 1 | -2.66 | -2.80 | 0.80 | 1.29 | 96% | MISSER | 0.023 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 25 | 1 | -1.14 | -0.70 | 0.50 | 0.96 | 96% | MISSER | 0.085 |

## Warsaw, PL

- **BEST:** UKMO
- **Consistent missers:** JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 23 | 2 | -0.88 | -0.70 | 0.50 | 1.06 | 87% | — | 0.096 |
| GFS | 23 | 4 | +0.68 | +1.00 | 0.50 | 1.12 | 83% | — | 0.104 |
| ICON | 23 | 8 | -0.40 | -0.50 | 0.40 | 0.61 | 70% | — | 0.261 |
| GEM | 23 | 4 | -0.27 | -0.10 | 0.40 | 0.79 | 61% | — | 0.215 |
| UKMO | 23 | 10 | -0.00 | +0.10 | 0.50 | 0.85 | 52% | BEST | 0.209 |
| JMA | 23 | 2 | -1.49 | -1.50 | 0.50 | 1.15 | 87% | MISSER | 0.054 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 23 | 0 | -1.53 | -1.20 | 0.40 | 0.88 | 100% | MISSER | 0.061 |

## Wellington, NZ

- **BEST:** GEM
- **Consistent missers:** GFS, JMA, AIFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 24 | 2 | -0.98 | -1.15 | 0.75 | 0.97 | 88% | — | 0.147 |
| GFS | 24 | 3 | -1.07 | -1.30 | 0.55 | 0.78 | 92% | MISSER | 0.158 |
| ICON | 24 | 5 | -0.75 | -0.90 | 0.35 | 0.88 | 83% | — | 0.200 |
| GEM | 24 | 8 | -0.47 | -0.55 | 0.80 | 1.44 | 75% | BEST | 0.124 |
| UKMO | 24 | 7 | -0.60 | -0.75 | 0.55 | 1.07 | 79% | — | 0.182 |
| JMA | 24 | 3 | -1.39 | -1.45 | 0.75 | 1.06 | 79% | MISSER | 0.096 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 24 | 1 | -1.43 | -1.35 | 0.80 | 1.06 | 92% | MISSER | 0.093 |

## Wuhan, CN

- **BEST:** JMA
- **Consistent missers:** ECMWF IFS, GFS, ICON, JMA
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 20 | 3 | -1.27 | -1.30 | 0.90 | 1.35 | 80% | MISSER | 0.122 |
| GFS | 20 | 3 | -1.40 | -1.45 | 0.80 | 1.24 | 85% | MISSER | 0.120 |
| ICON | 20 | 3 | -1.27 | -1.00 | 0.80 | 0.99 | 90% | MISSER | 0.159 |
| GEM | 20 | 1 | -0.88 | -1.30 | 0.60 | 1.62 | 75% | — | 0.122 |
| UKMO | 20 | 5 | -0.54 | -0.80 | 0.90 | 1.46 | 75% | — | 0.168 |
| JMA | 20 | 7 | -1.28 | -0.95 | 1.00 | 1.45 | 80% | MISSER | 0.113 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 20 | 0 | -0.98 | -1.00 | 0.75 | 1.04 | 85% | — | 0.195 |

## Zhengzhou, CN

- **BEST:** AIFS
- **Consistent missers:** ECMWF IFS, GFS
- **Oscillators:** —

| Provider | n | closest | bias | median | MAD | std | sign agree | class | weight |
|---|---|---|---|---|---|---|---|---|---|
| ECMWF IFS | 10 | 2 | -1.44 | -1.20 | 0.80 | 1.04 | 100% | MISSER | 0.094 |
| GFS | 10 | 1 | -1.21 | -1.45 | 0.35 | 1.14 | 90% | MISSER | 0.106 |
| ICON | 10 | 2 | -0.99 | -1.05 | 0.30 | 0.64 | 90% | — | 0.195 |
| GEM | 10 | 2 | +0.08 | -0.25 | 1.40 | 1.87 | 60% | — | 0.086 |
| UKMO | 10 | 2 | -0.43 | -0.90 | 0.40 | 1.20 | 80% | — | 0.170 |
| JMA | 10 | 2 | -0.49 | -0.75 | 1.15 | 1.26 | 60% | — | 0.154 |
| HRRR | 0 | 0 | — | — | — | — | — | no data | 0.000 |
| AIFS | 10 | 3 | -0.97 | -0.90 | 0.55 | 0.67 | 100% | BEST | 0.195 |
