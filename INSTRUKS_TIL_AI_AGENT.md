# 🌤️ VærMonitor — Komplett Dokumentasjon for AI Agent

> **Versjon:** 1.0  
> **Sist oppdatert:** 2026-08-08  
> **Repo:** [github.com/mgaaserud90-creator/weather-monitor](https://github.com/mgaaserud90-creator/weather-monitor)  
> **Dashboard:** [mgaaserud90-creator.github.io/weather-monitor/_quality_report.html](https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html)

---

## 📋 Innholdsfortegnelse

1. [Prosjekt Overblikk](#1-prosjekt-overblikk)
2. [Filstruktur](#2-filstruktur)
3. [GitHub & Deployment](#3-github--deployment)
4. [Pipeline Automatisering](#4-pipeline-automatisering)
5. [Strategier — Full Dokumentasjon](#5-strategier--full-dokumentasjon)
6. [BMA Ensemble — 8 Modeller](#6-bma-ensemble--8-modeller)
7. [Logg-Struktur](#7-logg-struktur)
8. [Peak Detection](#8-peak-detection)
9. [API-Oversikt](#9-api-oversikt)
10. [Hurtigguide for AI Agent](#10-hurtigguide-for-ai-agent)
11. [Alle Filtere](#11-alle-filtere)
12. [Feilsøking](#12-feilsøking)

---

## 1. PROSJEKT OVERBLIKK

### Hva gjør VærMonitor?

VærMonitor er et sofistikert prediksjonsmarkedsverktøy som bruker **Bayesian Model Averaging (BMA)** over 8 numeriske værvarslingsmodeller (NWP) for å predikere daglige maksimumstemperaturer i 51 byer globalt. Systemet sammenligner disse prediksjonene mot Polymarket.com sine temperaturmarkeder for å identifisere statistisk edge.

### The Edge

**Hvorfor VærMonitor har edge over Polymarket-markedene:**

1. **Multi-modell ensemble**: 8 uavhengige værmodeller kombineres via BMA — ingen enkeltmodell dominerer
2. **Early peak detection**: Systemet detekterer at dagens maksimumstemperatur er nådd **før** Polymarket-markedet justerer seg (10-30 min vindu)
3. **Statistisk signifikant**: Sigma-strategien oppnår historisk ~60-75% win rate med dynamisk k-faktor
4. **Kontinuerlig overvåkning**: Pipeline kjører hver time (06:00-23:00 UTC) med 3-minutters rapid polling i peak-vinduer

### Arkitektur

```
┌──────────────────────────────────────────────────────────────┐
│                    VÆRMONITOR ARKITEKTUR                       │
│                                                               │
│  ┌──────────┐   ┌──────────┐   ┌──────────┐                 │
│  │ Open-Meteo│   │ Polymarket│   │ Open-Meteo│                 │
│  │ Forecast  │   │ Gamma API │   │ Archive   │                 │
│  │ (8 models)│   │ (markets) │   │ (actuals) │                 │
│  └─────┬─────┘   └─────┬─────┘   └─────┬─────┘                 │
│        │               │               │                       │
│        ▼               ▼               ▼                       │
│  ┌──────────────────────────────────────────┐                 │
│  │        BMA Ensemble Engine               │                 │
│  │  · 8 NWP models → weighted mean          │                 │
│  │  · EM algorithm weight optimization      │                 │
│  │  · Lead-time uncertainty scaling          │                 │
│  │  · Seasonal bias correction               │                 │
│  └────────────────┬─────────────────────────┘                 │
│                   │                                            │
│                   ▼                                            │
│  ┌──────────────────────────────────────────┐                 │
│  │      3 Trading Strategies                │                 │
│  │  🎯 Sigma (μ−kσ): Dynamic k, 60-75% WR  │                 │
│  │  🛡️ P5-Basert: Ultra-conservative ~95%   │                 │
│  │  📊 Mean-Basert: 50/50 balanced          │                 │
│  └────────────────┬─────────────────────────┘                 │
│                   │                                            │
│                   ▼                                            │
│  ┌──────────────────────────────────────────┐                 │
│  │       Model Quality Pipeline             │                 │
│  │  06:00 → daily_bma (51 cities, top 5)    │                 │
│  │  07-22 → hourly_check (peak detection)    │                 │
│  │  23:00 → daily_close (resolve all)        │                 │
│  └────────────────┬─────────────────────────┘                 │
│                   │                                            │
│                   ▼                                            │
│  ┌──────────────────────────────────────────┐                 │
│  │  GitHub Pages Dashboard                  │                 │
│  │  _quality_report.html (auto-refresh 5m)  │                 │
│  └──────────────────────────────────────────┘                 │
└──────────────────────────────────────────────────────────────┘
```

### Teknologistakk

| Komponent | Teknologi |
|-----------|-----------|
| Språk | Python 3.11+ |
| GUI (lokal) | Tkinter (weather_monitor_gui.py) |
| GUI (web) | NiceGUI (src/gui/app.py) |
| Vær-API | Open-Meteo (GRATIS, ingen API-nøkkel) |
| Markedsdata | Polymarket Gamma API + CLOB API |
| Pipeline | GitHub Actions (Ubuntu runners) |
| Dashboard | GitHub Pages (statisk HTML) |
| Dataformat | JSON (_model_quality_log.json) |
| Avhengigheter | httpx, structlog, pydantic, tenacity |

---

## 2. FILSTRUKTUR

### Hovedfiler (i `polymarket-arb-bot/`)

```
polymarket-arb-bot/
├── 📄 weather_monitor_cli.py          ⭐ KJERNE — CLI + backend (2624 linjer)
│   · WeatherAnalyzer, LocationManager, PeakState, detect_peak_state
│   · compute_live_confidence, MarketDiscovery, BucketComparison
│   · ALLE dataklasser og hjelpefunksjoner for vær-analyse
│
├── 📄 weather_monitor_gui.py          🖥️ GUI — Tkinter desktop app (2457 linjer)
│   · 4 faner: Lokasjoner, Byer, Analyse, Overvåkning
│   · AsyncRunner for bakgrunns-operasjoner
│   · Importerer ALL backend-logikk fra weather_monitor_cli.py
│
├── 📄 weather_monitor_defaults.json   📍 51 standardbyer + korrelasjonsdata
│   · Hver by: navn, lat, lon, tidssone, peak-vindu, UHI, stasjon, elevasjon
│   · 18 by-korrelasjonspar (r ≥ 0.55)
│
├── 📄 _model_quality_tracker.py       🔄 Pipeline — GitHub Actions runner (1637 linjer)
│   · 4 modes: daily_bma, hourly_check, daily_close, full_report
│   · Rapid peak monitor: 3-min polling med ALLE 8 edge-filtre
│   · _compute_optimal_spill (sigma-justert strategi)
│
├── 📄 _generate_quality_report.py     📊 Rapportgenerator (925 linjer)
│   · --html: Genererer _quality_report.html (dark theme dashboard)
│   · Standard modus: _quality_report.md (markdown)
│   · 3-strategi sammenligning: Sigma vs P5 vs Mean
│
├── 📄 _backtest_30days.py             🔬 Historisk validering (818 linjer)
│   · Henter 30 dager med faktiske maks-temperaturer
│   · Sammenligner BMA prediksjoner mot arkivdata
│   · Kalibreringsmetrikker: P5-P95 treffrate, spill-treffrate
│
├── 📄 _crawl_temperature_markets.py   🕷️ Polymarket crawler (758 linjer)
│   · 5 strategier for å finne temperaturmarkeder
│   · CLOB API + Gamma API + regex fallback
│   · Filtrerer ut sports-markeder (Miami Heat, etc.)
│
├── 📄 _model_quality_log.json         📝 Pipeline state (auto-generert)
│   · Daglige prediksjoner, observasjoner, resultater
│   · 3-strategi resultater per by
│   · Cumulative statistikk
│
├── 📄 _quality_report.html            🌐 Dashboard (auto-generert)
│   · Dark theme, auto-refresh hvert 5. min
│   · Per-strategi performance cards
│   · Multi-day predictions (i morgen + 2 dager)
│
├── 📄 .gitignore                      🙈 Git ignore rules
│   · .env, __pycache__, *.pyc, venv/
│   · _backtest_results.json, _crawled_markets.json
│   · weather_monitor_locations.json (lokale endringer)
│
├── 📄 ARCHITECTURE_MAP.md             🗺️ Full arkitektur-dokumentasjon
│
├── 📁 .github/workflows/
│   ├── model_quality_pipeline.yml     🔄 Hoved-pipeline (3 cron triggere)
│   ├── deploy_dashboard.yml           🚀 GitHub Pages deployment
│   └── test_smoke.yml                 🧪 Smoke test (manuell trigger)
│
├── 📁 src/strategies/weather/
│   ├── ensemble.py                    🧠 BMA Ensemble Engine (8 modeller)
│   ├── strategy.py                    📈 WeatherCalibrationStrategy
│   ├── calibration.py                 📐 Normal CDF + bias-korreksjon
│   ├── kelly.py                       💰 Kelly Criterion position sizing
│   ├── market_parser.py               🔍 Parser Polymarket-spørsmål → WeatherMarket
│   ├── monitor.py                     👁️ WeatherMarketMonitor (kontinuerlig scan)
│   ├── microclimate.py                🏙️ Urban Heat Island adjustments
│   ├── satellite_correction.py        🛰️ Satellitt-korreksjon
│   ├── metar_feed.py                  🛫 METAR flyplass-data
│   └── dashboard.py                   📊 Dashboard komponenter
│
├── 📁 src/clients/
│   ├── openmeteo_client.py            🌤️ Open-Meteo API klient (649 linjer)
│   ├── gamma_client.py                📡 Polymarket Gamma API klient
│   ├── clob_client.py                 📊 Polymarket CLOB API klient
│   └── rate_limiter.py                ⏱️ Rate limiter (token bucket)
│
└── 📁 src/config/
    ├── constants.py                    🔢 Konstanter (WEATHER_MIN_LIQUIDITY etc.)
    ├── loader.py                       ⚙️ Config loader (singleton + LRU cache)
    └── schema.py                       ✅ Pydantic settings (type-safe config)
```

### Hva hver fil gjør — detaljert

#### [`weather_monitor_cli.py`](C:/Users/PC/Desktop/polymarket-arb-bot/weather_monitor_cli.py)

**Dette er DEN sentrale filen.** All backend-logikk for vær-analyse bor her. GUI og pipeline importerer fra denne.

**Nøkkelklasser:**
- `SavedLocation` — dataklasse for en by (navn, koordinater, tidsone, peak-vindu, UHI, stasjon)
- `LocationManager` — JSON-persistens for lagrede lokasjoner (maks 100), auto-populerer fra defaults
- `WeatherAnalyzer` — wrapper rundt BMAEnsembleEngine + OpenMeteoClient, med caching (15 min TTL)
- `PeakState` — dataklasse for peak detection resultat (7 tilstander, farger, emoji, live confidence)
- `MarketDiscovery` — scanner Polymarket Gamma API for værmarkeder som matcher lagrede lokasjoner
- `AnalysisResult`, `ConfidenceResult`, `BucketComparison` — analyse-resultat dataklasser
- `ForecastCache` — TTL-cache for værprognoser (15 min default)

**Nøkkelfunksjoner:**
- `geocode_city(city_name)` → `(display_name, lat, lon)` via Open-Meteo Geocoding API
- `detect_peak_state(...)` → `PeakState` — 8-trinns tilstandsmaskin for peak detection
- `compute_live_confidence(...)` → `(confidence_pct, mins_since_max, mins_decline, alert_level, alert_msg)`
- `compute_kelly(win_prob, odds)` → optimal Kelly-fraksjon (0-100%)
- `check_correlations(cities, correlations)` → advarsler om korrelerte byer
- `_wind_deg_to_compass(deg)` → kompassretning (norsk: N, NNØ, NØ, ...)

#### [`weather_monitor_cli.py` linje 483-735: `detect_peak_state`](C:/Users/PC/Desktop/polymarket-arb-bot/weather_monitor_cli.py:483)

**7 tilstander i peak detection:**

| State | Label | Emoji | Farge | Betydning |
|-------|-------|-------|-------|-----------|
| `future_date` | Venter | ⏳ | #9E9E9E | Måldato er i fremtiden |
| `past_date` | Fullført | ✅ | #4CAF50 | Måldato har passert |
| `rising` | STIGER | 🔵 | #2196F3 | Temp øker, før peak-vindu |
| `peak_window` | PEAK-VINDU | 🟡 | #FFC107 | Nå i peak-vindu, temp kan fortsatt stige |
| `possible_peak` | MULIG PEAK | 🟠 | #FF9800 | Temp synkende <30 min ELLER ingen ny max på 60+ min |
| `confirmed` | PEAK BEKREFTET | 🔴 | #D32F2F | Peak bekreftet (declining 30+ min past peak_end) |
| `completed` | FULLFØRT | ✅ | #4CAF50 | 2+ timer etter peak_end |

#### [`_model_quality_tracker.py`](C:/Users/PC/Desktop/polymarket-arb-bot/_model_quality_tracker.py)

**Pipeline CLI — 4 modes:**

```
python _model_quality_tracker.py --mode daily_bma       # 06:00 UTC
python _model_quality_tracker.py --mode hourly_check     # 07:00-22:00 UTC
python _model_quality_tracker.py --mode daily_close      # 23:00 UTC
python _model_quality_tracker.py --mode full_report      # Generer rapport
```

**Nøkkelfunksjoner:**
- `_compute_optimal_spill(mean_c, std_c, confidence, p5_c)` → dynamisk k-faktor strategi
- `run_bma_for_all(analyzer, locations, lead_days)` → BMA for alle 51 byer
- `select_top_n(predictions, n=5)` → topp 5 etter confidence
- `_rapid_peak_monitor(...)` → 3-minutts polling med ALLE 8 edge-filtre
- `_update_recommendation(pdata)` → generer HOLD/SELG/AVVENT anbefaling

---

## 3. GITHUB & DEPLOYMENT

### Repo-info

| Felt | Verdi |
|------|-------|
| URL | https://github.com/mgaaserud90-creator/weather-monitor |
| Default branch | `main` |
| CI/CD | GitHub Actions |
| Dashboard | GitHub Pages |
| Dashboard URL | https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html |

### Hvordan pushe til GitHub

```bash
cd C:/Users/PC/Desktop/polymarket-arb-bot
git add -A
git commit -m "Beskrivende melding"
git push origin main
```

**VIKTIG:** Pipeline triggers automatisk på `git push` til main (via `deploy_dashboard.yml`).

### GitHub Actions Workflows

#### 1. `model_quality_pipeline.yml` — Hovedpipeline

**Triggere:**
- `schedule`: 3 cron-jobber
- `workflow_dispatch`: Manuell trigger med mode-valg

**Cron-skjema:**
| Cron | UTC | Mode | Beskrivelse |
|------|-----|------|-------------|
| `0 6 * * *` | 06:00 | `daily_bma` | Kjør BMA for alle 51 byer, velg topp 5 |
| `0 * * * *` | 07:00-22:00 | `hourly_check` | Sjekk topp 5 temps, evt. rapid peak monitor |
| `0 23 * * *` | 23:00 | `daily_close` | Hent arkivdata, avgjør alle 51 byer, 3 strategier |

**Timeout:**
- `hourly_check`: 300 minutter (5 timer, for rapid peak monitoring innenfor GH 6-timers grense)
- Andre modes: 5 minutter

**Permissions needed:** `contents: write` (for å committe logg og rapport)

**Hvordan trigge manuelt:**
1. Gå til Actions → "Model Quality Pipeline"
2. Klikk "Run workflow"
3. Velg mode fra dropdown: `daily_bma`, `hourly_check`, `daily_close`, `full_report`
4. Klikk "Run workflow"

#### 2. `deploy_dashboard.yml` — GitHub Pages

**Trigger:** Push til `main` + `workflow_dispatch`

**Hva den gjør:**
1. Checkout kode
2. Installer Python-avhengigheter
3. Lag minimal `.env`
4. Kjør `python _generate_quality_report.py --html`
5. Last opp artifact til GitHub Pages
6. Deploy til `https://mgaaserud90-creator.github.io/weather-monitor/`

**Permissions needed:** `contents: read`, `pages: write`, `id-token: write`

#### 3. `test_smoke.yml` — Smoke Test

**Trigger:** Kun `workflow_dispatch` (manuell)

**Hva den gjør:**
1. Installerer ALLE avhengigheter
2. Tester 4 imports: `openmeteo_client`, `ensemble`, `weather_monitor_cli`, `_model_quality_tracker`
3. Kjører full `daily_bma` mode

### Slik deployer du dashboard manuelt

```bash
cd C:/Users/PC/Desktop/polymarket-arb-bot
python _generate_quality_report.py --html
git add _quality_report.html _quality_report.md
git commit -m "Update dashboard"
git push origin main
```

GitHub Actions vil automatisk deploye til Pages.

---

## 4. PIPELINE AUTOMATISERING

### Daglig flyt

```
06:00 UTC  ──► daily_bma
  │              · Henter BMA ensemble for ALLE 51 byer (lead_days=1 og 2)
  │              · Beregner 3 strategier per by (sigma, p5, mean)
  │              · Velger topp 5 etter confidence
  │              · Lagrer i _model_quality_log.json
  │
07:00 UTC  ──► hourly_check
  │              · Henter current temp for topp 5 byer
  │              · Sjekker peak-vindu status
  │              · Hvis noen by er i peak-vindu → starter rapid peak monitor
  │
08:00-22:00  hourly_check (hver time)
  │              · Samme som over
  │              · Rapid monitor kan kjøre opp til 5 timer (300 min timeout)
  │              · Poller hvert 3. minutt mens byer er i peak-vindu
  │
23:00 UTC  ──► daily_close
                 · Henter arkivdata (faktisk maks temp) for ALLE 51 byer
                 · Sammenligner mot alle 3 strategier
                 · Genererer _quality_report.md + _quality_report.html
                 · Oppdaterer cumulative statistikk
                 · Commit + push → trigger deploy_dashboard.yml
```

### Hvordan resette/restarte pipeline

1. **Slett dagens entry** i `_model_quality_log.json`:
   ```bash
   # Manuelt rediger _model_quality_log.json, fjern dagens "runs" entry
   ```

2. **Kjør daily_bma manuelt:**
   ```bash
   python _model_quality_tracker.py --mode daily_bma
   ```

3. **Force re-run alt:**
   ```bash
   rm _model_quality_log.json  # eller rename
   python _model_quality_tracker.py --mode daily_bma
   python _model_quality_tracker.py --mode hourly_check
   python _model_quality_tracker.py --mode daily_close
   ```

### Dataflyt detaljert

```
daily_bma:
  1. LocationManager().locations → 51 SavedLocation objekter
  2. WeatherAnalyzer().bulk_confidence_analysis() → 51 ConfidenceResult
  3. _compute_optimal_spill() per by → CityPrediction med 3 strategier
  4. Sorter etter confidence → top 5
  5. _preds_to_dict() → logg-klar dict
  6. _save_log() → _model_quality_log.json

hourly_check:
  1. _load_log() → finn dagens entry
  2. For hver av top 5:
     a. fetch_current_temp() → Open-Meteo current weather
     b. Oppdater observations[]
     c. detect_peak_state() → PeakState
     d. Hvis confirmed → resolve ALLE 3 strategier
  3. Hvis noen by i peak-vindu → _rapid_peak_monitor()
     a. Poll hvert 3. minutt
     b. ALLE 8 edge-filtre aktive
     c. resolve ved confirmed
  4. _save_log()

daily_close:
  1. _load_log() → finn dagens entry
  2. For ALLE 51 byer (ikke bare top 5):
     a. Hopp over allerede resolved
     b. _fetch_daily_max() → Open-Meteo Archive API
     c. Sammenlign mot ALLE 3 strategier
  3. Oppdater cumulative stats
  4. _generate_markdown_report()
  5. _save_log()
```

---

## 5. STRATEGIER — FULL DOKUMENTASJON

### Oversikt

VærMonitor evaluerer **3 parallelle strategier** for hver by, hver dag:

| # | Strategi | Formel | Win-sannsynlighet | Stil |
|---|----------|--------|-------------------|------|
| 🎯 | Sigma (μ−kσ) | `spill = ⌊μ − k×σ⌋` | 62-84% (avhengig av k) | **Adaptiv** — k justeres etter confidence |
| 🛡️ | P5-Basert | `spill = ⌊P5⌋` | ~95% | **Ultra-konservativ** — nesten garantert |
| 📊 | Mean-Basert | `spill = ⌊μ⌋` | ~50% | **Balansert** — 50/50 |

### 🎯 Sigma-justert strategi (PRIMÆR)

**Formel:**
```
spill = ⌊μ − k × σ⌋

hvor:
  μ  = BMA ensemble gjennomsnittstemperatur (°C)
  σ  = estimert standardavvik (fra P5-P95 range: σ ≈ (P95 - P5) / 3.29)
  k  = dynamisk risikofaktor
```

**Dynamisk k-faktor:**

| BMA Confidence | k | Win-sannsynlighet | Forklaring |
|----------------|---|-------------------|------------|
| > 80% | 0.3 | ~62% | Høy confidence → aggressivt spill |
| 70-80% | 0.5 | ~69% | Medium confidence → balansert |
| < 70% | 0.7 | ~76% | Lav confidence → konservativt |

**Win probability formel (normal approksimasjon):**
```
P(win) = P(faktisk_max ≥ spill) = 1 - Φ((spill - μ) / σ)

hvor Φ er standard normal CDF.

Effektiv beregning i kode:
  P(win) ≈ 0.5 × (1 + erf((μ - spill) / (σ × √2)))
```

### 🛡️ P5-Basert strategi

**Formel:**
```
spill = ⌊P5⌋

P(win) ≈ 0.95 (per definisjon av P5: 5% sannsynlighet for at temp < P5)
```

**Når å bruke:** Når du vil ha nesten garantert gevinst. Lav edge men svært høy win-rate.

### 📊 Mean-Basert strategi

**Formel:**
```
spill = ⌊μ⌋

P(win) ≈ 0.50 (50/50 — et myntkast)
```

**Når å bruke:** Referanse-strategi for sammenligning. Ikke anbefalt for trading.

### Kelly Criterion

**Formel:**
```
f* = (b × p - q) / b

hvor:
  b  = odds - 1 (netto odds, typisk 0.39 for Polymarket ~1.39 desimalodds)
  p  = vår estimerte win-sannsynlighet
  q  = 1 - p
```

**Eksempel:** Hvis p=0.69 og odds=1.39:
```
b = 1.39 - 1.0 = 0.39
f* = (0.39 × 0.69 - 0.31) / 0.39 = -0.105 → 0% (negativ edge)

Vent... la oss regne riktig:
Edge = p × odds - 1 = 0.69 × 1.39 - 1 = -0.041 → -4.1%

For positiv edge ved 1.39 odds trenger vi p > 1/1.39 = 0.719.
Det er derfor sigma-strategien sikter mot >72% win probability.
```

**Implementert i kode:** [`weather_monitor_cli.py` linje 143-156: `compute_kelly`](C:/Users/PC/Desktop/polymarket-arb-bot/weather_monitor_cli.py:143)

---

## 6. BMA ENSEMBLE — 8 MODELLER

### Modell-oversikt

| # | Modell | API-navn | Vekt | Oppløsning | Medlemmer | Oppdatering | Notat |
|---|--------|----------|------|------------|-----------|-------------|-------|
| 1 | **ECMWF IFS** | `ecmwf_ifs025` | 0.30 | 9 km | 51 | 00, 12z | Beste globale modell |
| 2 | **GFS** | `gfs_seamless` | 0.20 | 13 km | 31 | 00, 06, 12, 18z | US global, 4× daglig |
| 3 | **ICON** | `dwd_icon` | 0.15 | 13 km | 40 | 00, 06, 12, 18z | Tysk DWD, skarp for Europa |
| 4 | **GEM** | `gem_global` | 0.10 | 15 km | 21 | 00, 12z | Kanadisk modell |
| 5 | **UKMO** | `ukmo_global_deterministic_10km` | 0.08 | 10 km | 18 | 00, 12z | UK Met Office |
| 6 | **JMA** | `jma_seamless` | 0.07 | 20 km | 27 | 00, 12z | Japan, god for Asia |
| 7 | **HRRR** | `ncep_hrrr_conus` | 0.05 | 3 km | 1 | Hver time | US-only, 48t range |
| 8 | **AIFS** | `ecmwf_aifs025_single` | 0.05 | 28 km | 1 | 00, 12z | ECMWF AI/ML, eksperimentell |

### Hvordan modellene hentes

Alle 8 modeller hentes via **Open-Meteos ensemble-endepunkt**:
```
https://api.open-meteo.com/v1/forecast
  ?latitude={lat}&longitude={lon}
  &daily=temperature_2m_max
  &models=ecmwf_ifs025,gfs_seamless,dwd_icon,gem_global,...
  &forecast_days={lead_days+2}
```

Hver modell returnerer sin predikerte `temperature_2m_max` for måldatoen. Disse mates inn i BMA Ensemble Engine.

### BMA Ensemble Pipeline

```
1. FETCH           Hent rå prognoser fra alle 8 modeller (parallelt)
                   ↓
2. LEAD-TIME       Skaler usikkerhet: +0.3°C std per dag med lead time
                   ↓
3. EM ALGORITHM    Juster vekter via Expectation-Maximization (40-dagers vindu)
                   ↓
4. CRPS            CRPS-minimerende vekt-justering
                   ↓
5. SEASONAL BIAS   Korriger for sesong-bias per stasjon (30-dagers vindu)
                   ↓
6. BMA COMBINE     Vektet gjennomsnitt: μ = Σ(w_i × μ_i)
                   Vektet std: σ² = Σ(w_i × (σ²_i + (μ_i - μ)²))
                   ↓
7. OUTPUT          BMAEnsemble: mean, std, median, P5, P10, P90, P95, confidence
```

### Confidence-beregning

Confidence scores beregnes fra ensemble-spredning:

```python
# Fra weather_monitor_cli.py linje 1250-1255
model_agree_ratio = models_in_bucket / total_models
narrowness_bonus = 1.0 / (1.0 + max(0, (hi_c - lo_c) / 4.0))
bucket_confidence = (
    ens.confidence 
    * (0.4 + 0.6 * model_agree_ratio) 
    * min(1.0, 1.0 + narrowness_bonus * 0.3)
)
bucket_confidence = min(0.99, max(0.1, bucket_confidence))
```

**Faktorer:**
1. **Ensemble confidence** (fra BMA engine)
2. **Model agreement ratio** (40% vekt på ensemble baseline, 60% på modell-enighet)
3. **Narrowness bonus** (smalere buckets = høyere confidence)

### UHI (Urban Heat Island) Justering

Hver by kan ha en UHI-justering (0.0-3.0°C) definert i `weather_monitor_defaults.json`:

```json
{"name": "Tokyo, JP", "uhi_adjustment": 1.5}
{"name": "Mexico City, MX", "uhi_adjustment": 1.5}
{"name": "Beijing, CN", "uhi_adjustment": 1.3}
```

UHI legges til BMA-prediksjonen: `adjusted_mean = bma_mean + uhi_adjustment`

---

## 7. LOGG-STRUKTUR

### `_model_quality_log.json` — Full format

```json
{
  "runs": [
    {
      "run_date": "2026-08-08",
      "target_date": "2026-08-09",
      "phase": "daily_close",
      "run_started": "2026-08-08T06:00:00+00:00",
      "last_updated": "2026-08-08T23:05:00+00:00",
      "run_type": "multi",
      "top_5_confidence": [
        "Madrid, ES",
        "Dallas, US",
        "Beijing, CN",
        "Dubai, AE",
        "Athens, GR"
      ],
      "predictions": {
        "Madrid, ES": {
          "bma_mean": 35.4,
          "bma_std": 0.6,
          "p5": 34.4,
          "p95": 36.4,
          "confidence": 0.82,
          "models": 8,
          "strategies": {
            "sigma": {
              "spill": 35,
              "k": 0.3,
              "win_prob": 0.74,
              "result": "WIN",
              "actual_peak": 36.1
            },
            "p5": {
              "spill": 34,
              "k": null,
              "win_prob": 0.99,
              "result": "WIN",
              "actual_peak": 36.1
            },
            "mean": {
              "spill": 35,
              "k": 0.0,
              "win_prob": 0.74,
              "result": "WIN",
              "actual_peak": 36.1
            }
          },
          "peak_detected_at": "2026-08-09T15:30:00+02:00",
          "recommendation": "✅ HOLD — bet vinner (36.1°C ≥ 35°C)",
          "_lat": 40.4719,
          "_lon": -3.5626,
          "_tz": "Europe/Madrid",
          "_peak_hour_start": 15,
          "_peak_hour_end": 18,
          "_target_date": "2026-08-09",
          "_uhi_adjustment": 0.5
        }
      },
      "observations": {
        "Madrid, ES": [
          {
            "time": "2026-08-09T14:00:00+02:00",
            "temp_c": 32.1,
            "peak_state": "pre_peak"
          },
          {
            "time": "2026-08-09T15:30:00+02:00",
            "temp_c": 36.1,
            "peak_state": "confirmed"
          }
        ]
      },
      "summary": {
        "sigma_wins": 43,
        "sigma_losses": 8,
        "p5_wins": 49,
        "p5_losses": 2,
        "mean_wins": 25,
        "mean_losses": 26,
        "unresolved": 0
      },
      "multi_day": {
        "day1": { "...": "..." },
        "day2": { "...": "..." }
      }
    }
  ],
  "cumulative": {
    "total_days": 14,
    "total_predictions": 714,
    "sigma_wins": 582,
    "sigma_losses": 132,
    "p5_wins": 693,
    "p5_losses": 21,
    "mean_wins": 357,
    "mean_losses": 357
  }
}
```

### Felt-forklaringer

| Felt | Type | Beskrivelse |
|------|------|-------------|
| `run_date` | ISO date | Datoen pipeline kjørte (UTC) |
| `target_date` | ISO date | Datoen været predikeres for |
| `phase` | enum | `daily_bma`, `hourly_check`, `rapid_peak_monitor`, `daily_close` |
| `top_5_confidence` | list | Topp 5 byer rangert etter confidence |
| `predictions.{city}.bma_mean` | float | BMA ensemble gjennomsnitt (°C) |
| `predictions.{city}.bma_std` | float | Estimert standardavvik |
| `predictions.{city}.strategies.{name}.spill` | int | Anbefalt bet-nivå (°C) |
| `predictions.{city}.strategies.{name}.win_prob` | float | Estimert win-sannsynlighet |
| `predictions.{city}.strategies.{name}.result` | string | `WIN`, `LOSS`, eller `null` (ikke avgjort) |
| `predictions.{city}.strategies.{name}.actual_peak` | float | Faktisk maksimumstemperatur |
| `predictions.{city}.peak_detected_at` | ISO datetime | Når peak ble bekreftet |
| `predictions.{city}.recommendation` | string | HOLD/SELG/AVVENT anbefaling |
| `predictions.{city}._*` | various | Interne felt (prefikset med `_`) for API-kall |
| `observations.{city}` | list | Tidsserie med observasjoner (time, temp_c, peak_state) |
| `summary.sigma_wins/losses` | int | Per-strategi resultater over ALLE 51 byer |
| `cumulative` | dict | Akkumulerte resultater over alle dager |

### Hvordan resultater spores per strategi

For hver by spores **alle 3 strategier uavhengig**. Hver strategi har:
- `spill` — anbefalt kjøpsnivå (°C)
- `win_prob` — beregnet vinnersannsynlighet
- `result` — `WIN` / `LOSS` / `null`
- `actual_peak` — faktisk observert maksimum

Resultatet settes når peak bekreftes (via `detect_peak_state` → `confirmed`) eller ved daily_close (via `_fetch_daily_max` → arkiv-API).

### Hvordan peak detection logges

Når en peak bekreftes, logges:
1. `peak_detected_at` — ISO timestamp for bekreftelse
2. `strategies.*.result` — WIN/LOSS for alle 3 strategier
3. `strategies.*.actual_peak` — faktisk temperatur
4. `recommendation` — HOLD/SELG/AVVENT

I tillegg logges alle observationer i `observations.{city}[]` med `peak_state` felt.

---

## 8. PEAK DETECTION

### Alle 8 Filtre (Rapid Peak Monitor)

Når `_rapid_peak_monitor` er aktiv (byer i peak-vindu), kjøres ALLE disse filtrene ved hver poll (hvert 3. min):

| # | Filter | Betingelse | Justering |
|---|--------|-----------|-----------|
| 1 | 💧 Fuktighet | >80% relativ fuktighet | −8% confidence |
| 2 | 💧 Fuktighet | <40% relativ fuktighet | +3% confidence |
| 3 | ☁️ Skydekke | >70% skydekke | −5% confidence |
| 4 | ☁️ Skydekke | <20% skydekke | +3% confidence |
| 5 | 🏙️ UHI | Urban Heat Island | +0.5–3.0°C til BMA |
| 6 | 💰 Kelly | Posisjonsstørrelse | Optimal % av bankroll |
| 7 | 🔗 Korrelasjon | Kryss-by korrelasjon r≥0.55 | Reduser eksponering |
| 8 | 📊 Spredning | P5–P95 range | Small = høy confidence |

### Live Confidence Formel

```python
confidence = time_factor + decline_factor + staleness_factor + distance_bonus

hvor:
  time_factor       = min(60, 60 × hours_since_peak_start / peak_window_duration)
  decline_factor    = min(25, minutes_of_decline × 1.0)
  staleness_factor  = min(15, minutes_since_last_max × 0.25)
  distance_bonus    = 10 if temp < suggested_spill - 1.0 else
                       5 if temp < suggested_spill else 0
```

**Resultat:** 0-98% (aldri 100% — capped for å unngå overconfidence)

### 5 Alert Nivåer

| Nivå | Utløser | Betydning | Farge |
|------|---------|-----------|-------|
| `info` | Temp > suggested_spill | 🟢 Temp over anbefalt spill — vurder å selge | #2e7d32 |
| `advarsel` | Live confidence > 60%, i peak-vindu | 🟡 Peak sannsynlig nådd | #f57f17 |
| `kritisk` | Live confidence > 80%, decline ≥ 10 min | 🟠 SNU POSISJON NÅ — <5% sjanse for ny rekord | #E65100 |
| `bekreftet` | Live confidence > 90% ELLER post-peak + decline | 🔴 Peak låst — markedet justeres snart | #c62828 |
| (none) | Ingen trigger | — Normal overvåkning | — |

### Date-Aware Detection

Peak detection er **dato-bevisst**:
- Hvis `target_date > today_local` → `future_date` state (⏳ Venter)
- Hvis `target_date < today_local` → `past_date` state (✅ Fullført)
- Kun når `target_date == today_local` → aktiv peak detection

### Tidssonehåndtering

Alle byer har sin lokale tidssone definert i `weather_monitor_defaults.json`. Peak-vinduet (`peak_hour_start`–`peak_hour_end`) er i **lokal tid**.

Eksempler:
- Madrid: 15:00–18:00 CEST → 13:00–16:00 UTC
- Tokyo: 14:00–16:00 JST → 05:00–07:00 UTC
- New York: 14:00–17:00 EDT → 18:00–21:00 UTC

### Flip Recommendations

Når peak bekreftes og sigma-strategien taper:
```
🔴 SELG med tap — gå SHORT {spill}°C (peak={actual}°C)
```

Hvis sigma-strategien vinner:
```
✅ HOLD — bet vinner ({actual}°C ≥ {spill}°C)
```

---

## 9. API-OVERSIKT

### Open-Meteo — GRATIS, ingen API-nøkkel

| Endepunkt | Formål | Rate Limit |
|-----------|--------|------------|
| `https://api.open-meteo.com/v1/forecast` | Ensemble-værmelding (8 modeller) | 10,000/dag |
| `https://api.open-meteo.com/v1/forecast` | Current weather (temp, fuktighet, vind, skyer) | Samme |
| `https://archive-api.open-meteo.com/v1/archive` | Historiske data (daily max temp) | 10,000/dag |
| `https://geocoding-api.open-meteo.com/v1/search` | Geokoding (by → lat/lon) | 10,000/dag |

### Eksempler på API-kall

**Ensemble forecast (8 modeller):**
```
GET https://api.open-meteo.com/v1/forecast
  ?latitude=40.47&longitude=-3.56
  &daily=temperature_2m_max
  &models=ecmwf_ifs025,gfs_seamless,dwd_icon,gem_global,ukmo_global_deterministic_10km,jma_seamless
  &forecast_days=3
  &timezone=Europe/Madrid
```

**Current weather:**
```
GET https://api.open-meteo.com/v1/forecast
  ?latitude=40.47&longitude=-3.56
  &current=temperature_2m,relative_humidity_2m,wind_speed_10m,wind_direction_10m,cloud_cover
  &timezone=Europe/Madrid
```

**Archive (historisk):**
```
GET https://archive-api.open-meteo.com/v1/archive
  ?latitude=40.47&longitude=-3.56
  &start_date=2026-08-08&end_date=2026-08-08
  &daily=temperature_2m_max
  &timezone=Europe/Madrid
```

### Polymarket APIer

| API | URL | Formål |
|-----|-----|--------|
| Gamma API | `https://gamma-api.polymarket.com/markets` | Markedsdata (spørsmål, utfall, priser) |
| Gamma API | `https://gamma-api.polymarket.com/events` | Event-data |
| CLOB API | `https://clob.polymarket.com/markets` | Ordrebok, tokens, priser |

Ingen API-nøkkel nødvendig for read-only data. For trading kreves wallet.

---

## 10. HURTIGGUIDE FOR AI AGENT

### 🔧 Hvordan legge til en ny by

1. **Åpne** [`weather_monitor_defaults.json`](C:/Users/PC/Desktop/polymarket-arb-bot/weather_monitor_defaults.json)

2. **Legg til ny by** i `default_locations` array:
   ```json
   {"name": "Oslo, NO", "lat": 59.9139, "lon": 10.7522, 
    "tz": "Europe/Oslo", "peak_hour_start": 14, "peak_hour_end": 17, 
    "uhi_adjustment": 0.2, "station_elevation_m": 204, "station": "ENGM"}
   ```

3. **Felt:**
   - `name`: "By, LANDKODE" format
   - `lat/lon`: Koordinater (bruk Google Maps)
   - `tz`: IANA tidssone (f.eks. "Europe/Oslo")
   - `peak_hour_start/end`: Lokal tid for forventet daglig maksimum
   - `uhi_adjustment`: Urban heat island korreksjon (0.0 for landlig, 1.5 for megaby)
   - `station_elevation_m`: Høyde over havet i meter
   - `station`: ICAO flyplasskode (4 bokstaver)

4. **Valgfritt:** Legg til korrelasjon i `city_correlations` hvis relevant

5. **Commit og push.** Pipeline vil inkludere den nye byen ved neste daily_bma.

### 🔧 Hvordan fikse en ødelagt pipeline

**Symptom:** Pipeline feiler på daily_bma eller hourly_check.

**Steg:**
1. Sjekk GitHub Actions loggen først
2. Vanligste feil:
   - **HTTP timeout mot Open-Meteo** → vent 5 min, prøv igjen
   - **JSON decode error i _model_quality_log.json** → slett filen, kjør daily_bma
   - **orjson missing** → pip install orjson
   - **write permissions** → sjekk at workflow har `contents: write`

3. Manuell fiks:
   ```bash
   # Slett korrupt logg
   rm _model_quality_log.json
   # Kjør dagens pipeline på nytt
   python _model_quality_tracker.py --mode daily_bma
   ```

### 🔧 Hvordan legge til en ny strategi

1. **Modifiser** [`_model_quality_tracker.py`](C:/Users/PC/Desktop/polymarket-arb-bot/_model_quality_tracker.py):
   - I `_compute_optimal_spill()`: Legg til ny strategi-beregning
   - I `_preds_to_dict()`: Legg til strategi i output
   - I `daily_close_mode()`: Legg til resolve-logikk
   - I `_generate_markdown_report()`: Legg til kolonner

2. **Modifiser** [`_generate_quality_report.py`](C:/Users/PC/Desktop/polymarket-arb-bot/_generate_quality_report.py):
   - I HTML-generator: Legg til kort, tabeller

3. **Oppdater** `_model_quality_log.json` struktur hvis nødvendig

### 🔧 Hvordan endre cron schedule

Rediger [`model_quality_pipeline.yml`](C:/Users/PC/Desktop/polymarket-arb-bot/.github/workflows/model_quality_pipeline.yml):

```yaml
on:
  schedule:
    - cron: '0 6 * * *'      # Endre her for daily_bma
    - cron: '0 * * * *'      # Endre her for hourly_check
    - cron: '0 23 * * *'     # Endre her for daily_close
```

**Cron format:** `minutt time dag måned ukedag` (UTC)

**Eksempler:**
- Hver 3. time: `0 */3 * * *`
- Kun ukedager: `0 6 * * 1-5`
- Hvert 30. min: `*/30 * * * *`

### 🔧 Vanlige feil og fikser

| Feil | Årsak | Løsning |
|------|-------|---------|
| `ModuleNotFoundError: No module named 'httpx'` | Manglende avhengighet | `pip install httpx` |
| `orjson is not installed` | Structlog trenger orjson for JSON | `pip install orjson` |
| `.env file not found` | Mangler miljøvariabler | `cp .env.example .env` |
| `403 Forbidden` fra GitHub | Push bruker feil credentials | Sjekk `git remote -v` |
| `No daily_bma entry for today` | Pipeline ikke kjørt i dag | Kjør `--mode daily_bma` manuelt |
| `write permissions error` | Workflow mangler `contents: write` | Sjekk workflow YAML permissions |
| Pipeline timeout | hourly_check med rapid monitor > 6t | Reduser `max_runtime_hours` i tracker |
| `json.JSONDecodeError` | Korrupt loggfil | Slett `_model_quality_log.json` |

### 🔧 Hvordan verifisere at alt fungerer

1. **Lokal smoke test:**
   ```bash
   cd C:/Users/PC/Desktop/polymarket-arb-bot
   python -c "from weather_monitor_cli import WeatherAnalyzer, LocationManager; print('OK')"
   ```

2. **Kjør daily_bma lokalt:**
   ```bash
   python _model_quality_tracker.py --mode daily_bma
   ```

3. **Sjekk generert logg:**
   ```bash
   cat _model_quality_log.json | python -m json.tool | head -50
   ```

4. **Generer HTML dashboard:**
   ```bash
   python _generate_quality_report.py --html
   # Åpne _quality_report.html i nettleser
   ```

5. **GitHub Actions:**
   - Gå til Actions → "Test Smoke" → "Run workflow"
   - Hvis OK → "Model Quality Pipeline" → "Run workflow" med `daily_bma`

6. **Dashboard URL:**
   - Åpne https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html
   - Skal vise siste data (auto-refresh hvert 5. min)

---

## 11. ALLE FILTERE

### Komplett liste over alle filtre, formler og terskler

#### A. BMA Ensemble Filtre

| Filter | Formel | Terskel |
|--------|--------|---------|
| EM Algorithm vekter | Iterativ EM over 40-dagers vindu | Konvergens: 1e-6 |
| Lead-time usikkerhet | `σ_lead = σ_base + 0.3 × lead_days` | +0.3°C/dag |
| Sesong-bias | Per-stasjon, per-måned, 30-dagers rullerende | Auto-korrigert |
| CRPS-minimering | Minimize CRPS over training window | Kontinuerlig |
| Minimum std | `max(σ_computed, 0.5)` | 0.5°C floor |

#### B. Confidence Filtre

| Filter | Formel | Effekt |
|--------|--------|--------|
| Model agreement | `agree_ratio = models_in / total_models` | 40% baseline + 60% agreement |
| Narrowness bonus | `1 / (1 + (hi-lo)/4)` | Smalere = høyere |
| Confidence cap | `min(0.99, max(0.10, raw))` | Alltid 10-99% |
| Humidity (høy) | `if humidity > 80: adj -= 8%` | Fuktig luft = lavere |
| Humidity (lav) | `if humidity < 40: adj += 3%` | Tørr luft = høyere |
| Cloud (mye) | `if cloud > 70: adj -= 5%` | Skyet = lavere |
| Cloud (lite) | `if cloud < 20: adj += 3%` | Klart = høyere |
| UHI | `bma_adj = bma_mean + uhi` | Legges til prediksjon |

#### C. Peak Detection Filtre

| Filter | Formel | Vekt i live confidence |
|--------|--------|------------------------|
| Time factor | `min(60, 60 × hours_since_peak_start / window_duration)` | 0-60% |
| Decline factor | `min(25, minutes_decline × 1.0)` | 0-25% |
| Staleness factor | `min(15, minutes_since_max × 0.25)` | 0-15% |
| Distance bonus | `+10 if temp < spill-1, +5 if temp < spill` | 0-10% |
| Total cap | `min(98, sum)` | Maks 98% |

#### D. Strategi Filtre

| Filter | Formel | Beskrivelse |
|--------|--------|-------------|
| Dynamic k | `k = 0.3 if conf>0.8, 0.5 if conf>0.7, else 0.7` | Risikojustering |
| Sigma spill | `spill = ⌊μ − k×σ⌋` | Primærstrategi |
| P5 spill | `spill = ⌊P5⌋` | Ultra-konservativ |
| Mean spill | `spill = ⌊μ⌋` | 50/50 referanse |
| Win probability | `0.5 × (1 + erf((μ − T) / (σ × √2)))` | Normal CDF |
| Kelly | `(b×p − q) / b` | Posisjonsstørrelse |
| Korrelasjon | `r ≥ 0.55 → ⚠️ warning` | Kryss-by eksponering |

#### E. Trading Filtre

| Filter | Betingelse | Handling |
|--------|-----------|----------|
| Edge threshold | `p × odds > 1.05` | Minimum 5% edge |
| Kelly fraction | quarter-Kelly (×0.25) | Konservativ sizing |
| Position limit | `max_position = bankroll × kelly × 0.25` | Maks eksponering |
| Correlation reduction | `if correlated: reduce by 50%` | Risikospredning |

---

## 12. FEILSØKING

### Problem: `orjson is not installed`

**Symptom:** `structlog` eller `pydantic` bruker `orjson` for rask JSON-parsing.

**Løsning:**
```bash
pip install orjson
```

### Problem: `.env` mangler

**Symptom:** `pydantic-settings` ValidationError ved oppstart.

**Løsning:**
```bash
cp .env.example .env
```

Innhold i `.env` (minimum):
```
ENV=production
WEATHER_BMA_ENABLED=true
WEATHER_SATELLITE_ENABLED=false
WEATHER_ENSEMBLE_CONFIDENCE_FLOOR=0.5
```

### Problem: Pipeline får ikke skrevet til repo

**Symptom:** GitHub Actions feiler på "Commit & Push" steget.

**Løsning:** Sjekk at workflow YAML har:
```yaml
permissions:
  contents: write
```

Og at repo Settings → Actions → General → "Read and write permissions" er valgt.

### Problem: Open-Meteo rate limit

**Symptom:** HTTP 429 eller timeout.

**Løsning:** 
- Gratis tier: 10,000 calls/dag. Hver daily_bma bruker ~500 calls (8 modeller × 51 byer).
- Hvis rate limit nås: vent til neste dag, eller bytt til færre modeller.

### Problem: Rapid peak monitor timeout

**Symptom:** GitHub Actions `hourly_check` job avbrytes etter 300 min.

**Løsning:**
- Dette er normalt hvis peak-vinduet er langt og ingen peak bekreftes.
- Pipeline fortsetter ved neste hourly_check (neste time).
- For å unngå: øk polling-intervallet fra 3 til 5 minutter, eller reduser `MAX_RAPID_RUNTIME_HOURS`.

### Problem: `_model_quality_log.json` er korrupt

**Symptom:** `json.JSONDecodeError` ved lesing.

**Løsning:**
```bash
rm _model_quality_log.json
python _model_quality_tracker.py --mode daily_bma
```

### Problem: GUI starter ikke (weather_monitor_gui.py)

**Symptom:** `ModuleNotFoundError` eller tkinter-feil.

**Løsning:**
1. Sjekk at du er i riktig mappe: `cd C:/Users/PC/Desktop/polymarket-arb-bot`
2. Sjekk at avhengigheter er installert: `pip install httpx structlog tenacity pydantic python-dotenv colorama`
3. Windows: tkinter skal være inkludert i Python. Hvis ikke, reinstaller Python med "tcl/tk and IDLE" haket av.

### Problem: Dashboard viser gammel data

**Symptom:** GitHub Pages viser utdatert dashboard.

**Løsning:**
1. Sjekk at siste pipeline-kjøring fullførte OK
2. Sjekk at `_quality_report.html` ble generert og pushet
3. Vent 1-2 minutter (GitHub Pages cache)
4. Force refresh: `Ctrl+F5` i nettleser
5. Sjekk Actions → "Deploy Dashboard" for feil

### Problem: City not found in geocoding

**Symptom:** `geocode_city` returnerer None.

**Løsning:**
1. Prøv med landkode: "Oslo, NO", "New York, US"
2. Bruk Open-Meteo Geocoding API direkte for å teste: `https://geocoding-api.open-meteo.com/v1/search?name=Oslo&count=3`
3. Alternativt: legg til byen med koordinater direkte i `weather_monitor_defaults.json`

---

## 📎 APPENDIKS

### 51 Standardbyer med tidssoner og peak-vinduer

| By | Tidssone | Peak | UHI | Stasjon |
|----|----------|------|-----|---------|
| Taipei, TW | Asia/Taipei | 14-16 | 0.5 | RCTP |
| Hong Kong, HK | Asia/Hong_Kong | 14-16 | 1.0 | VHHH |
| Shanghai, CN | Asia/Shanghai | 14-16 | 1.0 | ZSSS |
| Seoul (Incheon), KR | Asia/Seoul | 14-17 | 1.0 | RKSI |
| Kuala Lumpur, MY | Asia/Kuala_Lumpur | 13-16 | 0.5 | WMKK |
| Madrid, ES | Europe/Madrid | 15-18 | 0.5 | LEMD |
| Paris, FR | Europe/Paris | 14-17 | 1.0 | LFPG |
| Munich, DE | Europe/Berlin | 14-17 | 0.3 | EDDM |
| Wellington, NZ | Pacific/Auckland | 14-16 | 0.0 | NZWN |
| Shenzhen, CN | Asia/Shanghai | 14-16 | 0.8 | ZGSZ |
| Singapore, SG | Asia/Singapore | 13-16 | 1.0 | WSSS |
| Guangzhou, CN | Asia/Shanghai | 14-16 | 0.8 | ZGGG |
| New York, US | America/New_York | 14-17 | 1.0 | KJFK |
| London, UK | Europe/London | 14-17 | 1.0 | EGLL |
| Milan, IT | Europe/Rome | 14-17 | 0.3 | LIMC |
| Los Angeles, US | America/Los_Angeles | 14-16 | 0.7 | KLAX |
| Tokyo, JP | Asia/Tokyo | 14-16 | 1.5 | RJTT |
| Helsinki, FI | Europe/Helsinki | 14-17 | 0.0 | EFHK |
| ... | ... | ... | ... | ... |

(Full liste: 51 byer i [`weather_monitor_defaults.json`](C:/Users/PC/Desktop/polymarket-arb-bot/weather_monitor_defaults.json))

### Nyttige kommandoer

```bash
# Kjør GUI lokalt
cd C:/Users/PC/Desktop/polymarket-arb-bot && python weather_monitor_gui.py

# Kjør CLI lokalt
cd C:/Users/PC/Desktop/polymarket-arb-bot && python weather_monitor_cli.py

# Kjør backtest (30 dager, alle 51 byer)
cd C:/Users/PC/Desktop/polymarket-arb-bot && python _backtest_30days.py

# Crawl Polymarket for temperaturmarkeder
cd C:/Users/PC/Desktop/polymarket-arb-bot && python _crawl_temperature_markets.py

# Generer dashboard HTML (uten pipeline)
cd C:/Users/PC/Desktop/polymarket-arb-bot && python _generate_quality_report.py --html

# Pipeline modes
python _model_quality_tracker.py --mode daily_bma       # 06:00 UTC
python _model_quality_tracker.py --mode hourly_check     # Hver time
python _model_quality_tracker.py --mode daily_close      # 23:00 UTC
python _model_quality_tracker.py --mode full_report      # Kumulativ rapport
```

### Pipeline Tidslinje (UTC)

```
06:00  daily_bma        ← BMA for 51 byer, velg topp 5
07:00  hourly_check     ← Første tempsjekk
08:00  hourly_check
09:00  hourly_check
10:00  hourly_check
11:00  hourly_check
12:00  hourly_check     ← ECMWF IFS 12z oppdatering
13:00  hourly_check     ← Europeiske byer nærmer seg peak
14:00  hourly_check     ← Mange Europeiske byer i peak-vindu
15:00  hourly_check     ← RAPID MONITOR sannsynlig aktiv
16:00  hourly_check
17:00  hourly_check     ← Amerikanske byer nærmer seg peak
18:00  hourly_check
19:00  hourly_check
20:00  hourly_check
21:00  hourly_check
22:00  hourly_check
23:00  daily_close      ← Hent arkiv, avgjør alle, generer rapport
```

---

> **Dokumentet er skrevet for å gi en AI agent FULL kontekst for å operere, feilsøke, og utvide VærMonitor-systemet. Ved tvil, les kildekoden — all logikk er dokumentert i filene referert ovenfor.**
