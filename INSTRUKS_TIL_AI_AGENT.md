# 🌤️ VærMonitor — Komplett Dokumentasjon for AI Agent

> **Versjon:** 2.1
> **Sist oppdatert:** 2026-08-11
> **Repo:** [github.com/mgaaserud90-creator/weather-monitor](https://github.com/mgaaserud90-creator/weather-monitor)
> **Dashboard:** [mgaaserud90-creator.github.io/weather-monitor/_quality_report.html](https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html)
> **Hjemmeside:** [mgaaserud90-creator.github.io/weather-monitor/](https://mgaaserud90-creator.github.io/weather-monitor/)

---

## 📋 Innholdsfortegnelse

1. [Nåværende Status](#1-nåværende-status)
2. [Prosjekt Overblikk](#2-prosjekt-overblikk)
3. [Filstruktur](#3-filstruktur)
4. [Edge Signal — BUY/SHORT Logikk](#4-edge-signal--buyshort-logikk)
5. [Dedup-Logikk](#5-dedup-logikk)
6. [Tidssoner & Aktive Vinduer](#6-tidssoner--aktive-vinduer)
7. [GitHub & Deployment](#7-github--deployment)
8. [Pipeline Automatisering](#8-pipeline-automatisering)
9. [Strategier — Full Dokumentasjon](#9-strategier--full-dokumentasjon)
10. [BMA Ensemble — 8 Modeller](#10-bma-ensemble--8-modeller)
11. [Alle Filtre](#11-alle-filtre)
12. [Peak Detection](#12-peak-detection)
13. [Logg-Struktur — Komplett JSON Schema](#13-logg-struktur--komplett-json-schema)
14. [Twilio SMS](#14-twilio-sms)
15. [API-Oversikt](#15-api-oversikt)
16. [Hurtigguide for AI Agent](#16-hurtigguide-for-ai-agent)
17. [Feilsøking](#17-feilsøking)

---

## 1. NÅVÆRENDE STATUS

### Hva fungerer ✅

| Komponent | Status | Detaljer |
|-----------|--------|----------|
| BMA Ensemble (8 modeller) | ✅ | Semaphore(5) rate limit, alle 8 modeller via Open-Meteo |
| 51 byer | ✅ | Alle med koordinater, tidssoner, peak-vinduer, UHI |
| Tidssone-aware pipeline | ✅ | `hourly_active` mode — prosesserer kun byer i aktivt vindu |
| 3 strategier (Sigma/P5/Mean) | ✅ | Tracket per by, per dag |
| Markedspriser (Polymarket) | ✅ | `_fetch_market_prices.py` + `_compute_market_edge.py` |
| Edge-kalkulasjon | ✅ | `edge = BMA_prob - market_price` (BUY/SHORT signal) |
| Resolution Arbitrage | ✅ | Post-peak scanner for gratis penger |
| Peak Detection (7 states) | ✅ | Med live confidence og 5 alert-nivåer |
| Live Peak Auto-Select | ✅ | `live_peak_selector.yml` — hvert 5. min, 0 API-kall, auto-velger byer i peak-vindu |
| Peak Verify vs Polymarket | ✅ | `peak_verify_polymarket.yml` — 23:30 UTC, sammenligner våre peaks mot Polymarket resolved |
| GitHub Actions (4 workflows) | ✅ | `model_quality_pipeline` + `live_peak_selector` + `fetch_market_prices` + `peak_verify_polymarket` |
| GitHub Pages dashboard | ✅ | Auto-deploy ved push til main |
| Per-by strategi-anbefaling | ✅ | Resultant Monitor — viser "Ingen data" når ingen resolved |
| Twilio SMS alerts | ✅ | Sendes når peak confidence > 70% OG strategi i fare |
| Model Agreement tracking | ✅ | 4 tiers (8/8, 7/8, 6/8, <6) |
| Edge Validation (Real vs Imagined) | ✅ | A/B-test per feature |
| Today vs Tomorrow edge decay | ✅ | Lead_days=0 vs lead_days=1 |

### Hva fungerer IKKE / Trenger arbeid ⚠️

| Komponent | Problem | Prioritet |
|-----------|---------|-----------|
| Auto-trader (CLOB) | Manuell trading kun; CLOB-integrasjon ikke implementert | Middels |
| METAR live-feed | `metar_feed.py` finnes men ikke integrert i pipeline | Lav |
| Satellitt-korreksjon | `satellite_correction.py` finnes men `WEATHER_SATELLITE_ENABLED=false` | Lav |
| Multi-source verifisering | Kun Open-Meteo; WeatherAPI/METAR ikke lagt til | Lav |
| Dynamisk korrelasjon | Statisk hardkodet i `weather_monitor_defaults.json` | Lav |
| Redis caching | `redis_client.py` finnes men Redis ikke deployet i pipeline | Lav |

### Viktige arkitektoniske notater

- **Pipeline er nå i `vær monitor/`** — IKKE `polymarket-arb-bot/`. Alle filer refereres relativt til `vær monitor/`.
- **`hourly_active` erstatter gammel `daily_bma` + `hourly_check`** — tidssone-aware, prosesserer kun aktive byer.
- **Lead days**: `lead_days=0` = i dag (model quality tracking), `lead_days=1` = i morgen (marked edge).
- **Polymarket resolution**: Vinner = `round(faktisk_max) == spill_bucket`. Eksakt avrunding.

---

## 2. PROSJEKT OVERBLIKK

### Hva gjør VærMonitor?

VærMonitor er et sofistikert prediksjonsmarkedsverktøy som bruker **Bayesian Model Averaging (BMA)** over 8 numeriske værvarslingsmodeller (NWP) for å predikere daglige maksimumstemperaturer i 51 byer globalt. Systemet sammenligner disse prediksjonene mot Polymarket.com sine temperaturmarkeder for å identifisere statistisk edge.

### The Edge

**Hvorfor VærMonitor har edge over Polymarket-markedene:**

1. **Multi-modell ensemble**: 8 uavhengige værmodeller kombineres via BMA — ingen enkeltmodell dominerer
2. **Early peak detection**: Systemet detekterer at dagens maksimumstemperatur er nådd **før** Polymarket-markedet justerer seg (10-30 min vindu)
3. **Statistisk signifikant**: Sigma-strategien oppnår historisk ~60-75% win rate med dynamisk k-faktor
4. **Kontinuerlig overvåkning**: Pipeline kjører hver time med 3-minutters rapid polling i peak-vinduer

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
│  │  Hver time → hourly_active (aktive byer) │                 │
│  │  23:00 UTC → daily_close (resolve all)   │                 │
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
| Vær-API | Open-Meteo (GRATIS, ingen API-nøkkel) |
| Markedsdata | Polymarket Gamma API + CLOB API |
| Pipeline | GitHub Actions (Ubuntu runners) |
| Dashboard | GitHub Pages (statisk HTML) |
| Dataformat | JSON (`_model_quality_log.json`) |
| SMS | Twilio API |
| Avhengigheter | httpx, structlog, tenacity, pydantic, pydantic-settings, python-dotenv, orjson |

---

## 3. FILSTRUKTUR

### Hovedfiler (i `vær monitor/`)

```
vær monitor/
├── 📄 weather_monitor_cli.py              ⭐ KJERNE — CLI + backend (2624 linjer)
│   · WeatherAnalyzer, LocationManager, PeakState, detect_peak_state
│   · compute_live_confidence, MarketDiscovery, BucketComparison
│   · ALLE dataklasser og hjelpefunksjoner for vær-analyse
│
├── 📄 weather_monitor_gui.py              🖥️ GUI — Tkinter desktop app
│   · 4 faner: Lokasjoner, Byer, Analyse, Overvåkning
│
├── 📄 weather_monitor_defaults.json       📍 51 standardbyer + korrelasjonsdata
│   · Hver by: name, lat, lon, tz, peak_hour_start/end, uhi_adjustment, station, elevation
│   · 18 by-korrelasjonspar (r ≥ 0.55)
│
├── 📄 _model_quality_tracker.py           🔄 Pipeline — GitHub Actions runner
│   · Modes: hourly_active, daily_bma, hourly_check, daily_close, full_report
│   · Rapid peak monitor: 3-min polling med ALLE 8 edge-filtre
│   · _compute_optimal_spill (sigma-justert strategi)
│
├── 📄 _generate_quality_report.py         📊 Rapportgenerator
│   · --html: _quality_report.html (dark theme dashboard)
│   · --all-cities: _all_cities.html
│   · --index: index.html
│   · --peak: _peak_detection.html
│   · 3-strategi sammenligning: Sigma vs P5 vs Mean
│
├── 📄 _compute_market_edge.py             💹 BMA vs Polymarket edge-kalkulator
│   · compute_bma_prob() — Normal CDF-basert sannsynlighet
│   · compute_edges() — edge = BMA_prob - market_price
│   · compute_resolution_arbitrage() — post-peak scanner
│   · build_market_lookup() — for all-cities dashboard
│
├── 📄 _fetch_market_prices.py             🕷️ Henter Polymarket markedspriser
│
├── 📄 _backtest_30days.py                 🔬 Historisk validering
│
├── 📄 _sms_alert.py                       📱 Twilio SMS-varsler
│
├── 📄 _model_quality_log.json             📝 Pipeline state (auto-generert)
│
├── 📄 _quality_report.html                🌐 Dashboard (auto-generert)
├── 📄 _all_cities.html                    🌍 Alle 51 byer tabell
├── 📄 _peak_detection.html               📈 Live peak detection
├── 📄 index.html                          🏠 Hjemmeside
├── 📄 brukermanual.html                   📖 Brukermanual (denne)
│
├── 📄 .gitignore
├── 📄 README.md
├── 📄 EDGE_OPTIMIZATION_PLAN.md           📋 10 edge-forbedringer
├── 📄 VærMonitor.bat                      🚀 Windows launch script
│
├── 📁 .github/workflows/
│   ├── model_quality_pipeline.yml         🔄 Hoved-pipeline (2 cron triggere)
│   ├── deploy_dashboard.yml               🚀 GitHub Pages deployment
│   └── test_smoke.yml                     🧪 Smoke test (manuell trigger)
│
├── 📁 src/
│   ├── event_bus.py
│   ├── 📁 clients/
│   │   ├── openmeteo_client.py            🌤️ Open-Meteo API klient (649 linjer)
│   │   ├── gamma_client.py                📡 Polymarket Gamma API klient
│   │   ├── clob_client.py                 📊 Polymarket CLOB API klient
│   │   └── rate_limiter.py                ⏱️ Rate limiter (token bucket)
│   ├── 📁 config/
│   │   ├── constants.py                    🔢 Konstanter
│   │   ├── loader.py                       ⚙️ Config loader (singleton + LRU cache)
│   │   └── schema.py                       ✅ Pydantic settings
│   ├── 📁 core/
│   │   ├── exceptions.py
│   │   └── models.py
│   ├── 📁 data/
│   │   ├── database.py
│   │   ├── models.py
│   │   ├── redis_client.py
│   │   └── repository.py
│   └── 📁 strategies/weather/
│       ├── ensemble.py                    🧠 BMA Ensemble Engine (8 modeller)
│       ├── strategy.py                    📈 WeatherCalibrationStrategy
│       ├── calibration.py                 📐 Normal CDF + bias-korreksjon
│       ├── kelly.py                       💰 Kelly Criterion
│       ├── market_parser.py               🔍 Parser Polymarket-spørsmål
│       ├── monitor.py                     👁️ WeatherMarketMonitor
│       ├── microclimate.py                🏙️ Urban Heat Island
│       ├── satellite_correction.py        🛰️ Satellitt-korreksjon (disabled)
│       ├── metar_feed.py                  🛫 METAR flyplass-data (disabled)
│       └── dashboard.py                   📊 Dashboard komponenter
```

---

## 4. EDGE SIGNAL — BUY/SHORT LOGIKK

### Verifisert korrekt — 2026-08-10

Edge-signalet beregnes i [`_compute_market_edge.py`](_compute_market_edge.py:369):

```
edge = BMA_prob - market_price   (i prosentpoeng)
```

### Signal-generering

| Betingelse | Signal | Forklaring |
|-----------|--------|------------|
| `edge > 0` | 🟢 **BUY** | BMA mener utfallet er MER sannsynlig enn markedet priser → undervurdert |
| `edge < 0` | 🔴 **SHORT** | BMA mener utfallet er MINDRE sannsynlig enn markedet priser → overvurdert |
| `edge == 0` | ⚪ FLAT | Ingen edge |

### BMA Probability-beregning

```python
# Fra _compute_market_edge.py:195-222
def compute_bma_prob(mean_c, std_c, temp, qtype):
    if qtype == "exact":
        prob = Φ((temp+0.5−μ)/σ) − Φ((temp−0.5−μ)/σ)
    elif qtype == "higher":
        prob = 1 − Φ((temp−0.5−μ)/σ)
    elif qtype == "below":
        prob = Φ((temp+0.5−μ)/σ)

# Deretter: edge = BMA_prob − market_prob
```

### Resolution Arbitrage — Post-Peak

Når peak-vinduet har passert OG faktisk temperatur er kjent, skanner [`compute_resolution_arbitrage()`](_compute_market_edge.py:561) etter markeder som fortsatt handler som om utfallet er usikkert:

| Betingelse | Handling | Profitt |
|-----------|----------|---------|
| Taper-bøtte @ 1-50¢ | 🔴 SHORT | `100 − price` ¢ |
| Vinner-bøtte @ 50-95¢ | 🟢 BUY | `100 − price` ¢ |

---

## 5. DEDUP-LOGIKK

### Edge-resultater (by, temperatur, type)

I [`compute_edges()`](_compute_market_edge.py:369), etter matching mot BMA:

```python
# Linje 484-492
seen: dict[tuple[str, int, str], dict] = {}
for r in results:
    key = (r["city"].lower(), r["temp"], r["qtype"])
    if key not in seen or r.get("volume", 0) > seen[key].get("volume", 0):
        seen[key] = r
results = list(seen.values())
```

**Nøkkel:** `(city_lowercase, temperature_celsius, question_type)` — beholder entry med høyest volum.

### Resolution Arbitrage

```python
# Linje 707-713
seen: dict[tuple[str, int, str], dict] = {}
for r in results:
    key = (r["city"], r["temp"], r["action"])
    if key not in seen or r.get("volume", 0) > seen[key].get("volume", 0):
        seen[key] = r
```

**Nøkkel:** `(city, temp, action)` — action er "BUY" eller "SHORT".

### Market Lookup (all-cities dashboard)

I [`build_market_lookup()`](_compute_market_edge.py:341):

```python
key = (city_raw_lowercase, temp_celsius)
# Også lagret uten parentes: "Seoul (Incheon)" → også som "Seoul"
```

### City-Matching mellom BMA og Polymarket

I [`_match_city()`](_compute_market_edge.py:386-422), 4-stegs matching:

1. **Eksakt match** (case-insensitive)
2. **Uten landkode**: "Moscow" matcher "Moscow, RU"
3. **Strip parentes fra market**: "Seoul (Incheon)" → matcher "Seoul (Incheon), KR"
4. **Strip parentes fra BMA base**: BMA "Seoul (Incheon), KR" → base "seoul" → matcher market "seoul"

### SMS Dedup

I [`_sms_alert.py`](_sms_alert.py:52-97): Kun én SMS per by per dag. Trackes i `_sms_log.json` med `{city_name: date_sent}`.

---

## 6. TIDSSONER & AKTIVE VINDUER

### Aktivt Vindu — Full Definisjon

En by er "aktiv" når lokal tid er mellom **04:00** og **peak_hour_end + 2 timer**.

```
aktiv_start = 04:00 lokal tid
aktiv_slutt = peak_hour_end + 2  (f.eks. 16 + 2 = 18:00)
```

Dette betyr:
- Før 04:00 lokal tid: For tidlig, ingen data tilgjengelig
- 04:00 til peak_hour_end+2: Aktivt vindu — BMA kjøres, peak overvåkes
- Etter peak_hour_end+2: Markedet er i ferd med å settle, ingen nye posisjoner

### Implementasjon

[`_is_city_active()`](_model_quality_tracker.py:424-447) og [`_get_active_cities()`](_model_quality_tracker.py:450-480):

```python
offset = _get_utc_offset_for_city(city_name, tz_str)
local_hour = (utc_hour + offset + 24) % 24
active_start = 4
active_end = peak_hour_end + 2
# Håndterer wrap-around over midnatt
```

### Region-Kartlegging

[`REGION_TZ_MAP`](_model_quality_tracker.py:304-372) mapper alle 51 byer til geografisk region og UTC-offset:

| Region | UTC Offset | Eksempler | Aktivt ved UTC |
|--------|-----------|-----------|----------------|
| ASIA | +5.5 til +9 | Tokyo, Shanghai, Mumbai | 19:00-01:00 UTC |
| EUROPE | +1 til +3 | London, Paris, Moscow | 02:00-16:00 UTC |
| AMERICAS | -7 til -4 | New York, LA, Dallas | 08:00-23:00 UTC |
| MIDDLE_EAST | +3 til +4 | Dubai, Tel Aviv, Jeddah | 01:00-15:00 UTC |
| OCEANIA | +10 til +12 | Sydney, Wellington | 18:00-06:00 UTC |
| AFRICA | +1 til +3 | Cape Town, Cairo, Lagos | 03:00-17:00 UTC |
| SOUTH_AM | -5 til -3 | Buenos Aires, Sao Paulo | 07:00-22:00 UTC |

### Eksempler — Peak-vindu i UTC

| By | Lokalt Peak | UTC Peak |
|----|------------|----------|
| Madrid, ES | 15:00-18:00 CEST | 13:00-16:00 UTC |
| Tokyo, JP | 14:00-16:00 JST | 05:00-07:00 UTC |
| New York, US | 14:00-17:00 EDT | 18:00-21:00 UTC |
| Dubai, AE | 14:00-16:00 GST | 10:00-12:00 UTC |
| Sydney, AU | 14:00-17:00 AEST | 04:00-07:00 UTC |

### Pipeline-timing per region

```
UTC 00-03:  ASIA avslutter peak, OCEANIA starter
UTC 04-07:  EUROPE våkner, OCEANIA i peak
UTC 08-11:  AMERICAS våkner, MIDDLE_EAST i peak
UTC 12-15:  EUROPE i peak, AMERICAS pre-peak
UTC 16-19:  AMERICAS i peak, EUROPE avslutter
UTC 20-23:  AMERICAS avslutter, ASIA starter ny dag
```

---

## 7. GITHUB & DEPLOYMENT

### Repo-info

| Felt | Verdi |
|------|-------|
| URL | https://github.com/mgaaserud90-creator/weather-monitor |
| Default branch | `main` |
| CI/CD | GitHub Actions |
| Dashboard | GitHub Pages |
| Dashboard URL | https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html |

### Hvordan pushe til GitHub

```powershell
cd "C:\Users\PC\Desktop\vær monitor"
git add -A
git commit -m "Beskrivende melding"
git push origin main
```

### GitHub Actions Workflows (4 aktive)

| # | Workflow | Cron | API-kall | Hensikt |
|---|----------|------|----------|---------|
| 1 | `model_quality_pipeline.yml` | Hver time + 23:00 UTC | ~827/dag (Open-Meteo) | BMA, peak check, daily_close, quality report, all-cities |
| 2 | `live_peak_selector.yml` | Hvert 5. min | **0** (kun tidssone-matematikk) | Auto-velger byer i peak-vindu → `_peak_detection.html` |
| 3 | `fetch_market_prices.yml` | Hvert 5. min | Polymarket API | Henter markedspriser → `_market_prices.json` |
| 4 | `peak_verify_polymarket.yml` | 23:30 UTC | 0 (leser JSON) | Sammenligner våre archive peaks mot Polymarket resolved |

#### 1. `model_quality_pipeline.yml` — Hovedpipeline

**Triggere:** `push`, `schedule`, `workflow_dispatch`
**Timeout:** 10 min
**Steg:** Checkout → Python 3.11 → pip install → smoke test → bestem mode → run tracker → generer HTML → SMS alerts → commit

#### 2. `live_peak_selector.yml` — Auto-Select Peak Window Cities

**Trigger:** `*/5 * * * *` (hvert 5. min) + `workflow_dispatch`
**Timeout:** 3 min
**API-kall:** 0 (kun tidssone-matematikk, `date.today()` + `ZoneInfo`)
**Output:** `_peak_detection.html` med `AUTO_SELECT_CITIES` innebygd. Browser JS henter temperaturer fra brukerens IP (separat rate limit).

#### 3. `fetch_market_prices.yml` — Polymarket Priser

**Trigger:** `*/5 * * * *` + `workflow_dispatch`
**Timeout:** 5 min
**API-kall:** Polymarket CLOB + Gamma (separat fra Open-Meteo)
**Output:** `_market_prices.json`

#### 4. `peak_verify_polymarket.yml` — Peak Verifisering

**Trigger:** `30 23 * * *` (23:30 UTC, 30 min etter daily_close) + `workflow_dispatch`
**Timeout:** 5 min
**Steg:** Leser `_model_quality_log.json` + `_market_prices.json` → sammenligner våre archive peaks mot Polymarket resolved outcomes → `_peak_verification_log.json`
**Toleranser:** ≤0.5°C = OK | 0.5–1.0°C = MINOR | >1.0°C = STASJONSFEIL

#### `deploy_dashboard.yml` — GitHub Pages Deploy

**Trigger:** Push til `main` + `workflow_dispatch`
Genererer alle HTML-filer og deployer til GitHub Pages.

---

## 8. PIPELINE AUTOMATISERING

### Daglig flyt (nåværende — tidssone-aware)

```
Hver time (00:00-23:00 UTC) → hourly_active
  │
  ├── 1. Finn aktive byer (04:00 lokal → peak_end+2 lokal)
  ├── 2. Kjør BMA for aktive byer (lead_days=0 = i dag)
  ├── 3. Peak check for aktive byer
  ├── 4. Hvis byer i peak-vindu → rapid peak monitor (3-min polling)
  ├── 5. Resolve ved confirmed peak
  └── 6. Save log
  │
23:00 UTC → daily_close
  │
  ├── 1. For ALLE 51 byer (ikke allerede resolved):
  ├── 2. Hent archive API for faktisk daily max
  ├── 3. Sammenlign mot ALLE 3 strategier
  ├── 4. Oppdater cumulative stats
  ├── 5. Generer rapport (MD + HTML)
  ├── 6. Save log
  └── 7. Auto-commit → trigger deploy_dashboard
```

### Hvordan resette/restarte pipeline

1. **Slett dagens entry** i `_model_quality_log.json`:
   - Manuelt rediger, fjern dagens entry i `runs` array

2. **Kjør daily_bma manuelt:**
   ```powershell
   python _model_quality_tracker.py --mode daily_bma
   ```

3. **Force re-run alt:**
   ```powershell
   Remove-Item _model_quality_log.json
   python _model_quality_tracker.py --mode daily_bma
   python _model_quality_tracker.py --mode hourly_check
   python _model_quality_tracker.py --mode daily_close
   ```

---

## 9. STRATEGIER — FULL DOKUMENTASJON

### Oversikt

VærMonitor evaluerer **3 parallelle strategier** for hver by, hver dag:

| # | Strategi | Formel | Win-sannsynlighet | Stil |
|---|----------|--------|-------------------|------|
| 🎯 | Sigma (μ−kσ) | `spill = round(μ − k×σ)` | 62-84% (avhengig av k) | **Adaptiv** — k justeres etter confidence |
| 🛡️ | P5-Basert | `spill = ⌊P5⌋` | ~95% | **Ultra-konservativ** — nesten garantert |
| 📊 | Mean-Basert | `spill = ⌊μ⌋` | ~50% | **Balansert** — 50/50 |

### 🎯 Sigma-justert strategi (PRIMÆR)

**Formel:**
```
spill = round(μ − k × σ)

hvor:
  μ  = BMA ensemble gjennomsnittstemperatur (°C)
  σ  = estimert standardavvik (fra P5-P95 range: σ ≈ (P95 − P5) / 3.29)
  k  = dynamisk risikofaktor
```

**Dynamisk k-faktor:**

| BMA Confidence | k | Win-sannsynlighet | Forklaring |
|----------------|---|-------------------|------------|
| > 80% | 0.3 | ~62% | Høy confidence → aggressivt spill |
| 70-80% | 0.5 | ~69% | Medium confidence → balansert |
| < 70% | 0.7 | ~76% | Lav confidence → konservativt |

**Dynamisk k-kalibrering (PRI 2):**
```python
# Fra _model_quality_tracker.py:236-245
historical_wr = _get_city_historical_winrate(city_name)
if historical_wr is not None and predicted_wp > 0:
    calibration_factor = historical_wr / predicted_wp
    k = k * (2.0 - calibration_factor)  # Juster k
    k = max(0.1, min(1.5, k))  # Clamp
```
- Hvis faktisk win-rate < predikert → overkonfident → øk k (mer konservativ)
- Hvis faktisk win-rate > predikert → underkonfident → senk k

### 🛡️ P5-Basert strategi

```
spill = ⌊P5⌋
P(win) ≈ 0.95
```

### 📊 Mean-Basert strategi

```
spill = ⌊μ⌋
P(win) ≈ 0.50
```

### Kelly Criterion

**Formel:**
```
f* = (b × p − q) / b

hvor:
  b  = odds − 1 (netto odds, typisk 0.39 for Polymarket ~1.39)
  p  = vår estimerte win-sannsynlighet
  q  = 1 − p
```

**Eksempel:** p=0.69, odds=1.39:
```
b = 0.39
f* = (0.39 × 0.69 − 0.31) / 0.39 = −0.105 → 0% (negativ edge)

For positiv edge ved 1.39 odds: p > 1/1.39 = 0.719
→ Sigma-strategien sikter mot >72% win probability.
```

### Win Probability Formel

```python
def win_prob(t, mu, sigma):
    """P(temp ≥ T) = 1 − Φ((T−μ)/σ)"""
    return 0.5 * (1 + erf((mu - t) / (sigma * √2)))
```

Strategi-resultatet (WIN/LOSS) avgjøres ved:
```python
is_win = round(actual_peak) == spill   # Polymarket resolution rule
```

---

## 10. BMA ENSEMBLE — 8 MODELLER

### Modell-oversikt

| # | Modell | API-navn | Vekt | Oppløsning | Oppdatering | Notat |
|---|--------|----------|------|------------|-------------|-------|
| 1 | **ECMWF IFS** | `ecmwf_ifs025` | 0.30 | 9 km | 00, 12z | Beste globale modell |
| 2 | **GFS** | `gfs_seamless` | 0.20 | 13 km | 00, 06, 12, 18z | US global, 4× daglig |
| 3 | **ICON** | `dwd_icon` | 0.15 | 13 km | 00, 06, 12, 18z | Tysk DWD, skarp for Europa |
| 4 | **GEM** | `gem_global` | 0.10 | 15 km | 00, 12z | Kanadisk modell |
| 5 | **UKMO** | `ukmo_global_deterministic_10km` | 0.08 | 10 km | 00, 12z | UK Met Office |
| 6 | **JMA** | `jma_seamless` | 0.07 | 20 km | 00, 12z | Japan, god for Asia |
| 7 | **HRRR** | `ncep_hrrr_conus` | 0.05 | 3 km | Hver time | US-only, 48t range |
| 8 | **AIFS** | `ecmwf_aifs025_single` | 0.05 | 28 km | 00, 12z | ECMWF AI/ML, eksperimentell |

### API-kall

```
GET https://api.open-meteo.com/v1/forecast
  ?latitude={lat}&longitude={lon}
  &daily=temperature_2m_max
  &models=ecmwf_ifs025,gfs_seamless,dwd_icon,gem_global,...
  &forecast_days={lead_days+2}
```

### BMA Ensemble Pipeline

```
1. FETCH           Hent rå prognoser fra alle 8 modeller (parallelt, semaphore(5))
2. LEAD-TIME       Skaler usikkerhet: +0.3°C std per dag med lead time
3. EM ALGORITHM    Juster vekter via Expectation-Maximization (40-dagers vindu)
4. CRPS            CRPS-minimerende vekt-justering
5. SEASONAL BIAS   Korriger for sesong-bias per stasjon (30-dagers vindu)
6. BMA COMBINE     Vektet gjennomsnitt: μ = Σ(w_i × μ_i)
                   Vektet std: σ² = Σ(w_i × (σ²_i + (μ_i − μ)²))
7. OUTPUT          BMAEnsemble: mean, std, median, P5, P10, P90, P95, confidence
```

### Confidence-beregning

```python
model_agree_ratio = models_in_bucket / total_models
narrowness_bonus = 1.0 / (1.0 + max(0, (hi_c - lo_c) / 4.0))
bucket_confidence = (
    ens.confidence 
    * (0.4 + 0.6 * model_agree_ratio) 
    * min(1.0, 1.0 + narrowness_bonus * 0.3)
)
bucket_confidence = min(0.99, max(0.10, bucket_confidence))
```

### UHI (Urban Heat Island) Justering

Legges til BMA-prediksjonen: `adjusted_mean = bma_mean + uhi_adjustment`

Eksempler fra [`weather_monitor_defaults.json`](weather_monitor_defaults.json):
- Tokyo, JP: 1.5°C
- Mexico City, MX: 1.5°C
- Beijing, CN: 1.3°C
- Wellington, NZ: 0.0°C (kystby)

---

## 11. ALLE FILTRE

### A. BMA Ensemble Filtre

| Filter | Formel | Terskel |
|--------|--------|---------|
| EM Algorithm vekter | Iterativ EM over 40-dagers vindu | Konvergens: 1e-6 |
| Lead-time usikkerhet | `σ_lead = σ_base + 0.3 × lead_days` | +0.3°C/dag |
| Sesong-bias | Per-stasjon, per-måned, 30-dagers rullerende | Auto-korrigert |
| CRPS-minimering | Minimize CRPS over training window | Kontinuerlig |
| Minimum std | `max(σ_computed, 0.5)` | 0.5°C floor |

### B. Confidence Filtre

| Filter | Formel | Effekt |
|--------|--------|--------|
| Model agreement | `agree_ratio = models_in / total_models` | 40% baseline + 60% agreement |
| Narrowness bonus | `1 / (1 + (hi−lo)/4)` | Smalere = høyere |
| Confidence cap | `min(0.99, max(0.10, raw))` | Alltid 10-99% |
| Humidity (høy) | `if humidity > 80: adj −= 8%` | Fuktig luft = lavere |
| Humidity (lav) | `if humidity < 40: adj += 3%` | Tørr luft = høyere |
| Cloud (mye) | `if cloud > 70: adj −= 5%` | Skyet = lavere |
| Cloud (lite) | `if cloud < 20: adj += 3%` | Klart = høyere |
| UHI | `bma_adj = bma_mean + uhi` | Legges til prediksjon |

### C. Peak Detection Filtre

| Filter | Formel | Vekt i live confidence |
|--------|--------|------------------------|
| Time factor | `min(60, 60 × hours_since_peak_start / window_duration)` | 0-60% |
| Decline factor | `min(25, minutes_decline × 1.0)` | 0-25% |
| Staleness factor | `min(15, minutes_since_max × 0.25)` | 0-15% |
| Distance bonus | `+10 if temp < spill−1, +5 if temp < spill` | 0-10% |
| Total cap | `min(98, sum)` | Maks 98% |

### D. Strategi Filtre

| Filter | Formel | Beskrivelse |
|--------|--------|-------------|
| Dynamic k | `k = 0.3 if conf>0.8, 0.5 if conf>0.7, else 0.7` | Risikojustering |
| Sigma spill | `spill = round(μ − k×σ)` | Primærstrategi |
| P5 spill | `spill = ⌊P5⌋` | Ultra-konservativ |
| Mean spill | `spill = ⌊μ⌋` | 50/50 referanse |
| Win probability | `0.5 × (1 + erf((μ − T) / (σ × √2)))` | Normal CDF |
| Kelly | `(b×p − q) / b` | Posisjonsstørrelse |
| Korrelasjon | `r ≥ 0.55 → ⚠️ warning` | Kryss-by eksponering |

### E. Trading Filtre

| Filter | Betingelse | Handling |
|--------|-----------|----------|
| Edge threshold | `p × odds > 1.05` | Minimum 5% edge |
| Kelly fraction | quarter-Kelly (×0.25) | Konservativ sizing |
| Position limit | `max_position = bankroll × kelly × 0.25` | Maks eksponering |
| Correlation reduction | `if correlated: reduce by 50%` | Risikospredning |

### F. Rapid Peak Monitor — Alle 8 Filtre

Når `_rapid_peak_monitor()` er aktiv (byer i peak-vindu), kjøres ALLE disse per 3-minutts poll:

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

---

## 12. PEAK DETECTION

### 7 Tilstander

`detect_peak_state()` i [`weather_monitor_cli.py`](weather_monitor_cli.py:483):

| State | Label | Emoji | Farge | Betydning |
|-------|-------|-------|-------|-----------|
| `future_date` | Venter | ⏳ | #9E9E9E | Måldato er i fremtiden |
| `past_date` | Fullført | ✅ | #4CAF50 | Måldato har passert |
| `rising` | STIGER | 🔵 | #2196F3 | Temp øker, før peak-vindu |
| `peak_window` | PEAK-VINDU | 🟡 | #FFC107 | Nå i peak-vindu, temp kan fortsatt stige |
| `possible_peak` | MULIG PEAK | 🟠 | #FF9800 | Temp synkende <30 min ELLER ingen ny max på 60+ min |
| `confirmed` | PEAK BEKREFTET | 🔴 | #D32F2F | Peak bekreftet (declining 30+ min past peak_end) |
| `completed` | FULLFØRT | ✅ | #4CAF50 | 2+ timer etter peak_end |

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

---

## 13. LOGG-STRUKTUR — KOMPLETT JSON SCHEMA

### `_model_quality_log.json`

```json
{
  "runs": [
    {
      "run_date": "2026-08-10",
      "target_date": "2026-08-10",
      "phase": "hourly_active | daily_bma | hourly_check | rapid_peak_monitor | daily_close",
      "run_started": "2026-08-10T06:00:00+00:00",
      "last_updated": "2026-08-10T16:05:00+00:00",
      "utc_hour": 14,
      "active_regions": ["ASIA", "EUROPE"],
      "active_city_count": 28,
      "all_city_count": 51,
      "top_5_confidence": ["Madrid, ES", "Dallas, US", "Beijing, CN", "Dubai, AE", "Athens, GR"],
      "predictions": {
        "Madrid, ES": {
          "bma_mean": 35.4,
          "bma_std": 0.6,
          "p5": 34.4,
          "p95": 36.4,
          "confidence": 0.82,
          "models": 8,
          "bma_probs": {"30": 0.1, "31": 1.2, "32": 5.3, "33": 14.2, "34": 26.1, "35": 28.3, "36": 17.2, "37": 6.1, "38": 1.5},
          "strategies": {
            "sigma": {
              "spill": 35,
              "k": 0.3,
              "win_prob": 0.74,
              "result": null,
              "actual_peak": null
            },
            "p5": {
              "spill": 34,
              "k": null,
              "win_prob": 0.99,
              "result": null,
              "actual_peak": null
            },
            "mean": {
              "spill": 35,
              "k": 0.0,
              "win_prob": 0.74,
              "result": null,
              "actual_peak": null
            }
          },
          "peak_detected_at": null,
          "recommendation": null,
          "_lat": 40.4719,
          "_lon": -3.5626,
          "_tz": "Europe/Madrid",
          "_peak_hour_start": 15,
          "_peak_hour_end": 18,
          "_target_date": "2026-08-10",
          "_uhi_adjustment": 0.5,
          "_lead_days": 0,
          "_features": {
            "model_weighting": true,
            "dynamic_k": true,
            "spread_filter": "narrow",
            "uhi_adjusted": false
          }
        }
      },
      "predictions_active": { "...": "..." },
      "predictions_multi_day": {
        "day1": { "...": "..." },
        "day2": { "...": "..." }
      },
      "observations": {
        "Madrid, ES": [
          {
            "time": "2026-08-10T14:00:00+02:00",
            "temp_c": 32.1,
            "peak_state": "pre_peak"
          },
          {
            "time": "2026-08-10T15:30:00+02:00",
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
      }
    }
  ],
  "cumulative": {
    "total_days": 14,
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
| `phase` | enum | `hourly_active`, `daily_bma`, `hourly_check`, `rapid_peak_monitor`, `daily_close` |
| `utc_hour` | int | UTC time (0-23) for hourly_active |
| `active_regions` | list | Hvilke regioner var aktive |
| `active_city_count` | int | Antall byer i aktivt vindu |
| `top_5_confidence` | list | Topp 5 byer rangert etter confidence |
| `predictions.{city}.bma_mean` | float | BMA ensemble gjennomsnitt (°C) |
| `predictions.{city}.bma_std` | float | Estimert standardavvik |
| `predictions.{city}.bma_probs` | dict | BMA sannsynlighet per temperatur-bøtte (P5-P95 range) |
| `predictions.{city}.strategies.{name}.spill` | int | Anbefalt bet-nivå (°C) |
| `predictions.{city}.strategies.{name}.k` | float | k-faktor (kun sigma) |
| `predictions.{city}.strategies.{name}.win_prob` | float | Estimert win-sannsynlighet |
| `predictions.{city}.strategies.{name}.result` | string | `WIN`, `LOSS`, eller `null` |
| `predictions.{city}.strategies.{name}.actual_peak` | float | Faktisk maksimumstemperatur |
| `predictions.{city}.peak_detected_at` | ISO datetime | Når peak ble bekreftet |
| `predictions.{city}.recommendation` | string | HOLD/SELG/AVVENT anbefaling |
| `predictions.{city}._*` | various | Interne felt for API-kall (prefikset med `_`) |
| `predictions.{city}._features` | dict | Feature-flags for edge validation |
| `predictions_active` | dict | Kun byer som var aktive i DENNE hourly_active run |
| `predictions_multi_day` | dict | day1 (lead_days=0) + day2 (lead_days=1) |
| `observations.{city}` | list | Tidsserie med observasjoner (time, temp_c, peak_state) |
| `summary` | dict | Per-strategi resultater for denne dagen |
| `cumulative` | dict | Akkumulerte resultater over alle dager |

### `_rapid_peak_log.json`

```json
[
  {
    "timestamp": "2026-08-10T14:15:00+00:00",
    "city": "Madrid, ES",
    "temp_c": 35.2,
    "trend": "↑",
    "live_confidence": 45.0,
    "base_confidence": 0.82,
    "adjusted_confidence": 0.847,
    "filters_active": {
      "humidity_adj": 0,
      "cloud_adj": 3,
      "uhi_adj": 0.5,
      "kelly_pct": 12.3,
      "correlation_warning": false,
      "ensemble_spread": 2.0,
      "bma_adjusted": 35.9,
      "suggested_spill": 35
    },
    "peak_state": "STIGER",
    "alert_level": "none",
    "alert_message": "",
    "humidity": 45,
    "cloud_cover": 15,
    "wind_speed": 8.2,
    "poll_number": 5
  }
]
```

### `_market_prices.json`

```json
{
  "fetched_at": "2026-08-10T14:00:00+00:00",
  "markets": [
    {
      "question": "Will the highest temperature in Madrid be 35°C on 2026-08-10?",
      "outcomes": [{"label": "Yes", "price": 0.62}, {"label": "No", "price": 0.38}],
      "volume": 12500,
      "volume_display": "$12.5k"
    }
  ]
}
```

### `_sms_log.json`

```json
{
  "Madrid, ES": "2026-08-10",
  "Tokyo, JP": "2026-08-10"
}
```

---

## 14. TWILIO SMS

### Oppsett

1. **Opprett Twilio-konto** på [twilio.com](https://www.twilio.com)
2. **Skaff et Twilio-nummer** (gratis trial gir $15 kreditt)
3. **Sett GitHub Secrets:**

| Secret | Beskrivelse | Eksempel |
|--------|-------------|----------|
| `TWILIO_SID` | Twilio Account SID | `ACxxxxxxxxxxxxx` |
| `TWILIO_TOKEN` | Twilio Auth Token | `xxxxxxxxxxxxx` |
| `TWILIO_FROM` | Twilio "From" nummer | `+1234567890` |

4. **Sett miljøvariabel** `ALERT_PHONE` (default: `+4795419426`) til mottaker-nummer

### Trigger-betingelser

SMS sendes når ALLE disse er sanne:
- `confidence > 70%` (BMA er sikker)
- `sigma_win_prob < 50%` (strategien er i fare)
- Ingen SMS allerede sendt for denne byen i dag (dedup)

### Pipeline-integrasjon

I [`model_quality_pipeline.yml`](.github/workflows/model_quality_pipeline.yml:116-121):
```yaml
- name: Send SMS alerts
  env:
    TWILIO_SID: ${{ secrets.TWILIO_SID }}
    TWILIO_TOKEN: ${{ secrets.TWILIO_TOKEN }}
    TWILIO_FROM: ${{ secrets.TWILIO_FROM }}
  run: python _sms_alert.py --check-and-send
```

### CLI-bruk

```powershell
# Test at Twilio fungerer
python _sms_alert.py --test

# Sjekk siste logg og send alerts automatisk
python _sms_alert.py --check-and-send
```

---

## 15. API-OVERSIKT

### Open-Meteo — GRATIS, ingen API-nøkkel

| Endepunkt | Formål | Rate Limit |
|-----------|--------|------------|
| `https://api.open-meteo.com/v1/forecast` | Ensemble-værmelding (8 modeller) | 10,000/dag |
| `https://api.open-meteo.com/v1/forecast` | Current weather (temp, fuktighet, vind, skyer) | Samme |
| `https://archive-api.open-meteo.com/v1/archive` | Historiske data (daily max temp) | 10,000/dag |
| `https://geocoding-api.open-meteo.com/v1/search` | Geokoding (by → lat/lon) | 10,000/dag |

### Polymarket APIer

| API | URL | Formål |
|-----|-----|--------|
| Gamma API | `https://gamma-api.polymarket.com/markets` | Markedsdata (spørsmål, utfall, priser) |
| Gamma API | `https://gamma-api.polymarket.com/events` | Event-data |
| CLOB API | `https://clob.polymarket.com/markets` | Ordrebok, tokens, priser |

Ingen API-nøkkel nødvendig for read-only data.

---

## 16. HURTIGGUIDE FOR AI AGENT

### 🔧 Hvordan legge til en ny by

1. **Åpne** [`weather_monitor_defaults.json`](weather_monitor_defaults.json)

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

4. **Valgfritt:** Legg til i `REGION_TZ_MAP` i [`_model_quality_tracker.py`](_model_quality_tracker.py:304) for riktig region-gruppering

5. **Valgfritt:** Legg til korrelasjon i `city_correlations` hvis relevant

6. **Commit og push.** Pipeline vil inkludere den nye byen ved neste hourly_active.

### 🔧 Hvordan fikse en ødelagt pipeline

**Symptom:** Pipeline feiler, ingen data i dashboard.

**Steg-for-steg diagnose:**
1. **Sjekk GitHub Actions loggen** — gå til Actions → "Model Quality Pipeline" → siste failed run
2. **Vanligste feil og fiks:**

| Feil | Årsak | Løsning |
|------|-------|---------|
| `ModuleNotFoundError: No module named 'httpx'` | Manglende avhengighet | `pip install httpx` |
| `orjson is not installed` | Structlog trenger orjson | `pip install orjson` |
| `.env file not found` | Mangler miljøvariabler | Lag `.env` med minimumsinnhold |
| `403 Forbidden` fra GitHub | Push bruker feil credentials | Sjekk `git remote -v` |
| `No daily_bma entry for today` | Pipeline ikke kjørt i dag | Kjør `--mode daily_bma` manuelt |
| `write permissions error` | Workflow mangler `contents: write` | Sjekk workflow YAML + repo settings |
| Pipeline timeout (>6h) | hourly_active med rapid monitor | Normalt; neste run plukker opp |
| `json.JSONDecodeError` | Korrupt loggfil | Slett `_model_quality_log.json`, kjør daily_bma |
| Open-Meteo 429 | Rate limit (10k/dag) | Vent til neste dag; daily_bma bruker ~500 kall |
| `KeyError` på city lookup | By mangler i REGION_TZ_MAP | Legg til byen i mappen |

3. **Force re-run:**
   ```powershell
   cd "C:\Users\PC\Desktop\vær monitor"
   Remove-Item _model_quality_log.json -ErrorAction SilentlyContinue
   python _model_quality_tracker.py --mode daily_bma
   ```

### 🔧 Hvordan endre cron schedule

Rediger [`model_quality_pipeline.yml`](.github/workflows/model_quality_pipeline.yml):

```yaml
on:
  schedule:
    - cron: '0 * * * *'      # hourly_active — endre her
    - cron: '0 23 * * *'     # daily_close — endre her
```

**Cron format:** `minutt time dag måned ukedag` (UTC)

### 🔧 Hvordan verifisere at alt fungerer

1. **Lokal smoke test:**
   ```powershell
   cd "C:\Users\PC\Desktop\vær monitor"
   python -c "from weather_monitor_cli import WeatherAnalyzer, LocationManager; print('OK')"
   ```

2. **Kjør daily_bma lokalt:**
   ```powershell
   python _model_quality_tracker.py --mode daily_bma
   ```

3. **Sjekk generert logg:**
   ```powershell
   python -c "import json; d=json.load(open('_model_quality_log.json')); print(f'Runs: {len(d[\"runs\"])}')"
   ```

4. **Generer dashboard lokalt:**
   ```powershell
   python _generate_quality_report.py --html
   ```

5. **GitHub Actions:**
   - Gå til Actions → "Model Quality Pipeline" → "Run workflow" → velg `daily_bma`

---

## 17. FEILSØKING

### `orjson is not installed`
```powershell
pip install orjson
```

### `.env` mangler
```powershell
# Minimum .env:
@"
ENV=production
WEATHER_BMA_ENABLED=true
WEATHER_SATELLITE_ENABLED=false
WEATHER_ENSEMBLE_CONFIDENCE_FLOOR=0.5
"@ | Out-File -FilePath .env -Encoding UTF8
```

### Pipeline får ikke skrevet til repo
Sjekk:
1. Workflow YAML har `permissions: contents: write`
2. Repo Settings → Actions → General → "Read and write permissions"

### `_model_quality_log.json` er korrupt
```powershell
Remove-Item _model_quality_log.json -ErrorAction SilentlyContinue
python _model_quality_tracker.py --mode daily_bma
```

### Dashboard viser gammel data
1. Sjekk siste pipeline run → fullført OK?
2. Sjekk at `_quality_report.html` ble generert og pushet
3. Vent 1-2 min (GitHub Pages cache)
4. Force refresh: `Ctrl+F5`

### GUI starter ikke
```powershell
cd "C:\Users\PC\Desktop\vær monitor"
pip install httpx structlog tenacity pydantic pydantic-settings python-dotenv colorama orjson
python weather_monitor_gui.py
```

---

## 📎 APPENDIKS

### Nyttige kommandoer

```powershell
# Pipeline modes
python _model_quality_tracker.py --mode hourly_active    # Tidssone-aware (anbefalt)
python _model_quality_tracker.py --mode daily_bma         # ALLE 51 byer, lead_days=0+1
python _model_quality_tracker.py --mode hourly_check      # Kun top 5 (gammel)
python _model_quality_tracker.py --mode daily_close       # 23:00 UTC
python _model_quality_tracker.py --mode full_report       # Kumulativ rapport

# Dashboard-generering
python _generate_quality_report.py --html                 # _quality_report.html
python _generate_quality_report.py --all-cities           # _all_cities.html
python _generate_quality_report.py --index                # index.html
python _generate_quality_report.py --peak                 # _peak_detection.html

# Edge-analyse
python _fetch_market_prices.py                            # Hent Polymarket priser
python _compute_market_edge.py                            # BMA vs marked edge
python _compute_market_edge.py --resolution-arb            # Post-peak scanner
python _compute_market_edge.py --json                      # JSON output

# Backtest
python _backtest_30days.py                                # 30 dager, alle 51 byer

# SMS
python _sms_alert.py --test                               # Test Twilio
python _sms_alert.py --check-and-send                     # Send alerts
```

---

> **Dokumentet er skrevet for å gi en AI agent FULL kontekst for å operere, feilsøke, og utvide VærMonitor-systemet. Ved tvil, les kildekoden — all logikk er dokumentert i filene referert ovenfor.**
