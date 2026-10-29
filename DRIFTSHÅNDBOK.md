# Driftshåndbok — `vær monitor`

Skrevet for å kunne brukes kl. 07:00 uten å lese koden. Alt kjører på
**GitHub Actions**; det finnes ingen Windows Task Scheduler og har aldri gjort det.
Repo: `mgaaserud90-creator/weather-monitor`, branch `main`, alle kommandoer kjøres
fra prosjektroten.

Bakgrunn og begrunnelser: [`STRATEGI_OPTIMALISERING.md`](STRATEGI_OPTIMALISERING.md) ·
[`INSTRUKS_TIL_AI_AGENT.md`](INSTRUKS_TIL_AI_AGENT.md). Denne filen dupliserer dem ikke.

---

## 1. Hva som kjører når

| Workflow | Cron (UTC) | Produserer | Job timeout | Eier disse artifaktene |
|---|---|---|---|---|
| [`full_auto_pipeline.yml`](.github/workflows/full_auto_pipeline.yml:1) | `30 16` (asia/nå) · `30 0` (europa/i går) · `30 8` (amerika/i går) · `0 12` (backstop/i går) | `_model_quality_log.json`, `_recommended_bets.json`, `_quality_report.*`, `index.html`, `_all_cities.html`, `_anbefalt_spill.html`, `_daily_city_log.*`, `_pnl_log.json`, WU-filer, `_modified_*`, `_heartbeat.json` | pipeline **60 min** · peak-rebuild **25 min** · deploy **30 min** | det meste — se eierskapskartet i [`action.yml`](.github/actions/commit-generated/action.yml:29) |
| [`model_quality_pipeline.yml`](.github/workflows/model_quality_pipeline.yml:1) | `0 0-22` (timesvis) | `_model_quality_log.json` | 15 min | deler `_model_quality_log.json` med full_auto (begge er full-regeneratorer, «vår vinner») |
| [`fetch_market_prices.yml`](.github/workflows/fetch_market_prices.yml:1) | `0 * * * *` | `_market_prices.json`, `_price_history.jsonl` | 15 min | **eneeier** |
| [`collect_resolved_markets.yml`](.github/workflows/collect_resolved_markets.yml:1) | `*/30` | `_resolved_markets_log.json`, `_resolved_markets.csv` | 12 min | deler med full_auto |
| [`peak_verify_polymarket.yml`](.github/workflows/peak_verify_polymarket.yml:1) | `*/30` | `_peak_verification_log.json`, `_peak_deviation_log.json` | 20 min | deler med full_auto |
| [`live_peak_selector.yml`](.github/workflows/live_peak_selector.yml:1) | `*/5` | `_peak_detection.html` | 12 min | deler med full_auto |
| [`sla_watchdog.yml`](.github/workflows/sla_watchdog.yml:1) | `*/30` | `_healthcheck_report.json`, evt. `pipeline-alert`-issue | 15 min | **ingen** (skriver ikke til git) |
| [`deploy_dashboard.yml`](.github/workflows/deploy_dashboard.yml:1) | ingen cron (`push`/dispatch) | GitHub Pages | — | — |
| [`test_smoke.yml`](.github/workflows/test_smoke.yml:1) | ingen cron (`push`/dispatch) | guarder + importtest | 30 min | — |

**Målt forbehold:** GitHub leverer cron på denne public-repoen **3–7 timer for sent**,
og dropper av og til en kjøring helt. Derfor:

* regionen utledes **aldri** fra klokka (`github.event.schedule` er sannheten;
  `force_schedule` overstyrer den), og
* vaktbikkja teller *leverede* `schedule`-kjøringer siste 24 t framfor å tro på cron-spesifikasjonen.

`daily_bma` kjører bare når regionen er `backstop` (cron `0 12 * * *`) og tar målt ~2 m 42 s.

---

## 2. Hvordan starte en kjøring manuelt

```bat
gh workflow run full_auto_pipeline.yml -f "force_schedule=0 12 * * *"
```

De fire gyldige verdiene (alt annet feiler høylytt i workflowen):

| `force_schedule` | Region / lukkedag |
|---|---|
| `30 16 * * *` | asia / i dag |
| `30 0 * * *` | europa / i går |
| `30 8 * * *` | amerika / i går |
| `0 12 * * *` | backstop / i går (**kjører `daily_bma`**) |

**Sitatfellen i cmd.exe:** uten anførselstegn deler `cmd` verdien på mellomrom, og `gh`
får bare `0` mens `12`, `*`, `*`, `*` blir løse argumenter. Det gir
`Unrecognised schedule ''0'` (observert én gang). Skriv derfor alltid
`-f "force_schedule=0 12 * * *"`. Sitter du i PowerShell og anførselstegnene
spises opp av `;`-kjeding, bruk `--raw-field "force_schedule=0 12 * * *"` eller kjør
kommandoen alene.

Andre nyttige:

```bat
gh workflow run sla_watchdog.yml                                  :: grønn, stille (ekte sjekk)
gh workflow run sla_watchdog.yml -f simulate_stale_hours=48       :: RØD bevis-kjøring, varsler IKKE
gh workflow run fetch_market_prices.yml
gh workflow run collect_resolved_markets.yml
gh run list --limit 10
gh run watch <run-id>
```

En simulering skal bli rød (det er beviset på at deteksjonen virker), men den
skal **ikke** sende SMS og **ikke** åpne issue. Blir en simuleringskjøring grønn,
er det deteksjonen som er ødelagt; åpner den en issue, er varslingsvernet ødelagt.

---

## 3. Når alarmen går

Alarmteksten er nøkkelen. Alt kommer fra [`_healthcheck.py`](_healthcheck.py:1)
via [`send_alert()`](_sms_alert.py:168): SMS hvis `ALERT_PHONE` er satt, ellers en
deduplisert GitHub-issue med label `pipeline-alert`. **`ALERT_PHONE` er ikke satt i
dag, så den levende kanalen er GitHub-issues.**

| Alarmtekst | Betyr | Sjekk først | Kommando |
|---|---|---|---|
| `stale:_model_quality_log.json` (>6 t) | modellkvalitet er ikke skrevet | er `model_quality_pipeline` i det hele tatt levert? | `gh run list --workflow=model_quality_pipeline.yml --limit 10` |
| `stale:_market_prices.json` (>6 t) | prisfetch står | rate limiting fra Polymarket, eller degraded-snapshot-guarden som nekter å skrive ufullstendige priser | `gh run list --workflow=fetch_market_prices.yml --limit 10` → `gh run view <id> --log-failed` |
| `stale:_resolved_markets_log.json` (>12 t) | settlement står | kjører `collect_resolved_markets`? | `gh run list --workflow=collect_resolved_markets.yml --limit 10` |
| `heartbeat` / `heartbeat-stale` | en pipeline-kjøring nådde aldri slutten, eller `_heartbeat.json` mangler/er uleselig | siste `full_auto_pipeline`-kjøring | `gh run list --workflow=full_auto_pipeline.yml --limit 5` |
| `missing-dates:_wunderground_real.json` | WU-hull i de siste 3 fullførte UTC-dagene | API-feil, eller en kjøring som døde før WU-steget | `python _fetch_wunderground_real.py --days 7` |
| `dropped-slots:<workflow>` | for få *leverede* `schedule`-kjøringer siste 24 t | GitHub-throttling vs. ødelagt cron | se under |
| `inconclusive` i `_healthcheck_report.json` | **ikke en alarm** — API-et kunne ikke spørres (rate limit/5xx/nett) og ble retryet | ingen ting; neste kjøring om 30 min prøver igjen | `gh run view <id> --log` |

**Dropped slots — throttling eller ødelagt cron?**

```bat
gh run list --workflow=full_auto_pipeline.yml --limit 100 --json createdAt,event --jq "[.[]|select(.event==\"schedule\")]|length"
```

* Tall > 0 men under minimum (`full_auto_pipeline` ≥ 2, `model_quality_pipeline` ≥ 4
  per 24 t): **GitHub throttler**. Vent; ikke rør workflowen.
* Tallet er **0** over flere døgn: cronen er ødelagt (feil filnavn i `--workflow`,
  eller `schedule:`-blokken er borte). Da er det en kodefeil, ikke vær.

**Finne og lukke issue:**

```bat
gh issue list --label pipeline-alert --state open
gh issue view <nummer>
gh issue close <nummer> --comment "Pipeline grønn igjen; lukker."
```

Issuen er deduplisert på tittel, så en langvarig feil gir **én** issue, ikke én per
30 minutter. Lukk den når `sla_watchdog` er grønn igjen — ellers vil neste ekte
avvik bli dempet inn i den gamle.

---

## 4. Dataintegritet

**WU-backfill** (henter på nytt, overskriver ikke historikk uten videre):

```bat
python _fetch_wunderground_real.py              :: i går for alle byer
python _fetch_wunderground_real.py --days 7     :: siste 7 lokale dager
python _fetch_wunderground_real.py --date 2026-09-01
```

`--days` ble tidligere deklarert men aldri lest (dokumentert backfill var en stille
no-op); det er rettet.

**WU gyldighetsport:** en dato regnes bare som sannhet når
`mean|Δ| < 0,6 °C` **og** `sd < 0,9 °C` mot det systemet allerede vet. Målt
2026-10-29: **28 av 56 datoer er flagget som kontaminerte** (daglig middel −1,6 …
−3,4 °C, sd 3,2–3,7 °C, drevet av US °F-rader). De 28 kontaminerte datoene **skal
ekskluderes fra alt stasjonsbias-arbeid** — å bruke dem som fysisk sannhet korrumperer
bias-korreksjonen i det stille. Porten skrives ut i kjøreloggen; les den.

**`_wunderground_real.csv` er nå sitert** fordi bynøklene inneholder `", "`
(f.eks. `Amsterdam, NL`). Uten sitat ble ni felt skrevet under en åtte-felts header,
og hver nedstrøms CSV-leser forskjøv kolonnene. Kolonneorden og navn er uendret;
bare sitat er lagt til, og `lineterminator="\n"` beholder LF.

**`_peak_verification_log.json` beholder bevisst bare nyeste rad per by.** Det er
en tilstandslogg, ikke en historikk. «Fiks» den ikke — historikken ligger i
`_peak_deviation_log.json`.

---

## 5. Vaktbikkjer

| Skript | Håndhever | Når den feiler |
|---|---|---|
| [`_check_timeout_budget.py`](_check_timeout_budget.py:1) | sum av steg-`timeout-minutes` ≤ jobbens `timeout-minutes` (pipeline 60, peak-rebuild 25, deploy 30) | legg tilbake/øk et *steg*timeout slik at summen holder seg innenfor jobbtaket, eller hev jobbtaket **bevisst** og dokumenter hvorfor |
| [`_check_data_growth.py`](_check_data_growth.py:1) | budsjett per artifakt, inkl. **120 MB** for `_price_history.jsonl` | krymp dataene (retention komprimerer selv) eller hev budsjettet **bevisst** |

Aldri demp, hopp over eller omgå en gate for å få grønt. En gate som er skrudd av
er en gate som ikke finnes.

---

## 6. Strategi

Systemet har **ingen reell pengefordel**; det er den stående dommen i
[`STRATEGI_OPTIMALISERING.md`](STRATEGI_OPTIMALISERING.md). Residual-ECDF (P4) er
forkastet og holdouten brukt opp (R3). Stasjonsbias-korreksjonen er vedtatt og kan
slås av umiddelbart med kill switch `VMON_BIAS_CORRECTION=0`.

**R2 blokkerer spill** til `_price_history.jsonl` har **≥ 200 prisede spill**, og
ingen seleksjonsendring kan noen gang vedtas på hit-rate- eller Brier-bevis.
Tell prisede rader:

```bat
python -c "import json;rows=[json.loads(l) for l in open('_price_history.jsonl',encoding='utf-8') if l.strip()];print('prisede rader:',sum(1 for r in rows if r.get('yes_price') is not None))"
```

Er filen fraværende, er svaret 0 — og da er svaret også nei.

---

## 7. Hva du aldri skal gjøre

1. **Aldri `push --force`** (eller force-with-lease) mot `main`.
2. **Aldri gjeninnfør den gamle commit-transporten.** Alle sju skrivere går gjennom
   [`.github/actions/commit-generated/action.yml`](.github/actions/commit-generated/action.yml:1);
   de tre gamle mekanismene er bevisst ikke stavet ut noe sted, slik at grep-tripwiren forblir tom.
3. **Aldri gate på veggklokke-timer.** Cron leveres 3–7 t sent; bruk `github.event.schedule`.
4. **Aldri tune `k` mot hit rate.** Det er ikke bevis.
5. **Aldri blås opp `bma_std`** for å se mer selvsikker ut.
6. **Aldri vis til `_edge_enhancer_backtest.json` som bevis.**
7. **Aldri rør enhetshåndteringen i `_populate_peak_verify.py`.**
