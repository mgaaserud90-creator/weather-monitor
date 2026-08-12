"""Resolve yesterday's predictions using archive API."""
import asyncio
import json
import sys
sys.path.insert(0, ".")

import httpx
from _model_quality_tracker import _load_log, _save_log, _now_utc, _update_recommendation

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


async def resolve_yesterday():
    log = _load_log()
    runs = log.get("runs", [])
    yesterday = "2026-08-11"

    entry = None
    for r in runs:
        if r.get("run_date") == yesterday:
            entry = r
            break

    if not entry:
        print(f"No entry for {yesterday}")
        return

    preds = entry.get("predictions", {})
    print(f"Resolving {len(preds)} cities for {yesterday}...")

    sigma_wins = sigma_losses = 0
    p5_wins = p5_losses = 0
    mean_wins = mean_losses = 0

    async with httpx.AsyncClient(timeout=30.0) as client:
        for city, pdata in sorted(preds.items()):
            strategies = pdata.get("strategies", {})
            sigma = strategies.get("sigma", {})
            if sigma.get("result") in ("WIN", "LOSS"):
                sigma_r = sigma["result"]
                if sigma_r == "WIN":
                    sigma_wins += 1
                else:
                    sigma_losses += 1
                print(f"  {city}: already resolved sigma={sigma_r}")
                continue

            lat = pdata.get("_lat", 0)
            lon = pdata.get("_lon", 0)
            tz = pdata.get("_tz", "UTC")

            try:
                resp = await client.get(
                    ARCHIVE_URL,
                    params={
                        "latitude": lat, "longitude": lon,
                        "start_date": yesterday, "end_date": yesterday,
                        "daily": "temperature_2m_max", "timezone": tz,
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                daily = data.get("daily", {})
                temps = daily.get("temperature_2m_max", [])
                if temps and temps[0] is not None:
                    actual = round(float(temps[0]), 1)
                else:
                    print(f"  {city}: no archive data")
                    continue
            except Exception as e:
                print(f"  {city}: API error: {e}")
                continue

            pdata["peak_detected_at"] = _now_utc()

            for sn in ("sigma", "p5", "mean"):
                s = strategies.get(sn, {})
                spill = s.get("spill", 0)
                is_win = round(actual) == spill
                s["result"] = "WIN" if is_win else "LOSS"
                s["actual_peak"] = actual

            _update_recommendation(pdata)

            sigma_r = strategies["sigma"]["result"]
            if sigma_r == "WIN":
                sigma_wins += 1
            else:
                sigma_losses += 1

            p5_r = strategies.get("p5", {}).get("result")
            if p5_r == "WIN":
                p5_wins += 1
            elif p5_r == "LOSS":
                p5_losses += 1

            mean_r = strategies.get("mean", {}).get("result")
            if mean_r == "WIN":
                mean_wins += 1
            elif mean_r == "LOSS":
                mean_losses += 1

            print(f"  {city}: actual={actual}C sigma={sigma_r} spill={sigma.get('spill')}")

        # Small delay between requests to avoid rate limiting
        await asyncio.sleep(0.3)

    entry["summary"] = {
        "sigma_wins": sigma_wins, "sigma_losses": sigma_losses,
        "p5_wins": p5_wins, "p5_losses": p5_losses,
        "mean_wins": mean_wins, "mean_losses": mean_losses,
    }
    entry["phase"] = "daily_close"
    entry["last_updated"] = _now_utc()
    _save_log(log)

    total = sigma_wins + sigma_losses
    print(f"\nDone: sigma={sigma_wins}W/{sigma_losses}L ({round(sigma_wins/max(1,total)*100,1)}%)")
    print(f"p5={p5_wins}W/{p5_losses}L, mean={mean_wins}W/{mean_losses}L")


asyncio.run(resolve_yesterday())
