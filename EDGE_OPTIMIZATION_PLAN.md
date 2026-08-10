# VærMonitor — Edge-Optimaliseringsplan

## Nåværende Status

| Komponent | Status |
|-----------|--------|
| BMA Ensemble (8 modeller) | ✅ Fungerer, semaphore(5) rate limit |
| 51 byer med stasjonskoordinater | ✅ Verifisert, 2 fikset |
| Tidssone-aware pipeline | ✅ Aktivt vindu per region |
| 3 strategier (Sigma/P5/Mean) | ✅ Tracket per by |
| Markedspriser (Polymarket) | ✅ 73 matchede muligheter |
| Win-rate per konfidensnivå | ✅ 4 tiers |
| Per-by strategi-anbefaling | ✅ Resultant Monitor |
| GitHub Actions cron | ✅ Kjører hver time |
| GitHub Pages dashboard | ✅ Live |

## 10 Edge-Forbedringer (Prioritert)

### 🥇 PRI 1: Modell-Vekting (Ikke Lik Vekt)
**Problem:** Alle 8 modeller vektes likt. ECMWF er verifisert ~30% mer nøyaktig enn GFS globalt.

**Løsning:** 
- ECMWF: vekt 2.0
- UKMO: vekt 1.5  
- GFS/ICON: vekt 1.0
- GEM/JMA: vekt 0.8
- HRRR/AIFS: vekt 0.6 (regional/eksperimentell)
- Spor per-modell nøyaktighet per by over tid → auto-juster vekter

**Forventet Edge-økning:** +3-5% win-rate

### 🥈 PRI 2: Dynamisk k Basert På Historisk Kalibrering
**Problem:** k-verdien (0.3/0.5/0.7) er statisk basert på confidence.

**Løsning:** 
- Spor kalibreringskurve: "når modellen sier X% konfidens, hva er faktisk win-rate?"
- Hvis modellen er overkonfident (sier 80% men vinner 60%) → øk k (mer konservativ)
- Hvis modellen er underkonfident → senk k
- Per-by kalibrering for maksimal presisjon

**Forventet Edge-økning:** +5-10% win-rate

### 🥉 PRI 3: Ensemble Spread Som Signal
**Problem:** Ensemble spread (P5-P95 bredde) brukes ikke aktivt som trading-signal.

**Løsning:**
- Smal spread (<2°C) + høy edge → **stor posisjon** (høy conviction)
- Bred spread (>4°C) → **liten/ingen posisjon** (modellene uenige)
- Spread-endring over tid → trend-signal (blir modellene mer enige?)

**Forventet Edge-økning:** +2-4% win-rate + bedre risk management

### 4️⃣ PRI 4: Diurnal Kurve-Modellering
**Problem:** Vi predikerer kun dagsmaks, ikke NÅR på dagen den inntreffer.

**Løsning:**
- Hent time-data fra Open-Meteo for prediksjonsdagen
- Modeller når peak inntreffer → bedre timing for exit/flip
- "Peak forventet kl 15:00 → hvis temp fortsatt stiger kl 14:30, vent med å selge"

**Forventet Edge-økning:** Bedre timing, mindre tapte muligheter

### 5️⃣ PRI 5: Markeds-Mikrostruktur (Bid/Ask Spread)
**Problem:** Vi ser kun mid-market pris, ikke spread.

**Løsning:**
- Hent order book fra CLOB API
- Smal spread = likvid marked = lettere å trade
- Bred spread = unngå (kostbart å gå inn/ut)
- Filtrer bort markeder med spread > 5%

**Forventet Edge-økning:** Reduserer transaksjonskostnader

### 6️⃣ PRI 6: Værfront-Korrelasjon
**Problem:** Korrelasjon mellom byer er statisk hardkodet.

**Løsning:**
- Beregn løpende korrelasjon fra faktiske temperatur-data
- Grupper byer etter værfront-påvirkning
- Hvis Shanghai og Tokyo begge viser BUY → dobbeltsjekk (samme værfront)
- Unngå overeksponering mot korrelerte markeder

### 7️⃣ PRI 7: Auto-Trader (CLOB API)
**Problem:** Manuell trading er treg og utsatt for feil.

**Løsning:**
- Integrer Polymarket CLOB API for automatisk ordreplassering
- Sett opp regler: "hvis edge > 15% OG spread < 3% → BUY $50"
- Stop-loss: "hvis markedet beveger seg 10% mot deg → selg"
- Kun for byer med høyest historisk win-rate

### 8️⃣ PRI 8: Sesong-Justering
**Problem:** Modellen behandler alle dager likt. Sommer ≠ vinter.

**Løsning:**
- Track win-rate per måned/sesong
- August (høysommer) → lettere å predikere (stabile værmønstre)
- April/Oktober → vanskeligere (overgangsmåneder)
- Juster posisjonsstørrelse etter sesong

### 9️⃣ PRI 9: Multi-Source Verifisering
**Problem:** Kun Open-Meteo som datakilde.

**Løsning:**
- Legg til WeatherAPI (gratis tier: 1M kall/mnd)
- Legg til METAR/aviationweather.gov for flyplass-data
- Kryss-sjekk: hvis begge kilder er enige → høyere konfidens
- Hvis kildene spriker → flagg usikkerhet

### 🔟 PRI 10: Datasett-Utvidelse
**Problem:** 51 byer, men bare 22 har aktive Polymarket-markeder.

**Løsning:**
- Når nye byer dukker opp på Polymarket → auto-legg til i defaults
- Overvåk `/markets` for nye temperatur-markeder
- Auto-utvid datasettet

## Anbefalt Rekkefølge

1. **PRI 1 + PRI 2** (Modell-vekting + Kalibrering) → størst edge-økning
2. **PRI 3** (Spread-som-signal) → bedre risk management
3. **PRI 7** (Auto-trader) → eliminerer menneskelige feil
4. **PRI 4-6** → inkrementelle forbedringer
5. **PRI 8-10** → langsiktig robusthet

## Estimert Kumulativ Edge

| Etter | Forventet Win-Rate |
|-------|-------------------|
| Nåværende | ~39% (Mean, round-logikk) |
| +PRI 1 | ~44% |
| +PRI 2 | ~52% |
| +PRI 3 | ~55% (med bedre risk management) |
