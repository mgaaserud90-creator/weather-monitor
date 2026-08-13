# 🌤️ VærMonitor — Polymarket Temperatur Trading

**Prediksjonsmarkedsverktøy med statistisk edge gjennom BMA Ensemble-modellering.**

---

## Hva er VærMonitor?

VærMonitor bruker **8 værvarslingsmodeller** kombinert via Bayesian Model Averaging (BMA) for å predikere daglige maksimumstemperaturer i 51 byer globalt. Prediksjonene sammenlignes mot [Polymarket.com](https://polymarket.com) sine temperaturmarkeder for å identifisere trading-muligheter med statistisk signifikant edge.

## 🚀 Hurtigstart

Dobbeltklikk på **VærMonitor.bat** på skrivebordet, eller:

```bash
cd C:\Users\PC\Desktop\polymarket-arb-bot
python weather_monitor_gui.py
```

## 📊 Dashboard

Live dashboard tilgjengelig på:  
https://mgaaserud90-creator.github.io/weather-monitor/_quality_report.html

Dashboardet oppdateres automatisk hvert 5. minutt og viser:
- 📈 Kumulativ strategi-performance (Sigma vs P5 vs Mean)
- 📅 Topp 5 byer for i morgen og +2 dager
- 🔄 Flip-anbefalinger
- ⚡ Rapid peak monitoring filtre

## 🧠 Slik fungerer det

1. **BMA Ensemble** — 8 NWP-modeller (ECMWF, GFS, ICON, GEM, UKMO, JMA, HRRR, AIFS) hentes fra Open-Meteo og kombineres med Bayesiansk modell-gjennomsnitt
2. **3 Strategier** — Sigma (μ−kσ, dynamisk k), P5-Basert (ultra-konservativ), Mean-Basert (50/50 referanse)
3. **Peak Detection** — Sanntids deteksjon av daglig maksimumstemperatur med 8 edge-filtre
4. **Pipeline** — Automatisert via GitHub Actions: 06:00 BMA → timeovervåkning → 23:00 avslutning

## 📁 Filstruktur

| Fil | Beskrivelse |
|-----|-------------|
| `weather_monitor_gui.py` | Desktop GUI (Tkinter, 4 faner) |
| `weather_monitor_cli.py` | CLI + all backend-logikk |
| `weather_monitor_defaults.json` | 51 standardbyer |
| `_model_quality_tracker.py` | Pipeline (daily_bma, hourly_check, daily_close) |
| `_generate_quality_report.py` | Dashboard-generator (HTML + MD) |
| `_backtest_30days.py` | Historisk validering (30 dager) |
| `_fetch_market_prices.py` | Henter Polymarket markedspriser |

## 🔧 Krav

- Python 3.11+
- Avhengigheter: `pip install httpx structlog tenacity pydantic pydantic-settings python-dotenv colorama orjson`
- `.env` fil med minimum: `ENV=production`, `WEATHER_BMA_ENABLED=true`

## 📈 Forventet ytelse

| Strategi | Win Rate | Stil |
|----------|----------|------|
| 🎯 Sigma (μ−kσ) | 60-75% | Adaptiv, anbefalt |
| 🛡️ P5-Basert | ~95% | Nesten garantert, lav edge |
| 📊 Mean-Basert | ~50% | Referanse |

## 🤖 For AI Agenter

Se [`INSTRUKS_TIL_AI_AGENT.md`](INSTRUKS_TIL_AI_AGENT.md) for komplett teknisk dokumentasjon.

## 📦 GitHub

Repo: https://github.com/mgaaserud90-creator/weather-monitor

---

*Bygget med Open-Meteo (gratis vær-API) og Bayesian Model Averaging.*
