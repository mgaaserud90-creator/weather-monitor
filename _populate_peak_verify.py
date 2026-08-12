"""Populate _peak_verification_log.json by comparing our archive peaks to Polymarket resolved outcomes."""
import json, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _model_quality_tracker import _load_market_resolved_temps, _load_log, _log_peak_verification

resolved = _load_market_resolved_temps()
print(f"Polymarket resolved markets (with dates): {len(resolved)}")
for (city, date), temp in sorted(resolved.items())[:10]:
    print(f"  {city} ({date}): {temp}°C")

log = _load_log()
runs = log.get("runs", [])
entry = None
for r in runs:
    if r.get("run_date") == "2026-08-11":
        entry = r
        break

if not entry:
    print("No entry for 2026-08-11")
    sys.exit(1)

preds = entry.get("predictions", {})
verified = 0
for city, pdata in sorted(preds.items()):
    sigma = pdata.get("strategies", {}).get("sigma", {})
    actual = sigma.get("actual_peak")
    if actual is None:
        continue
    city_base = city.split(",")[0].strip()
    target = pdata.get("_target_date", "2026-08-11")
    mt = resolved.get((city, target)) or resolved.get((city_base, target))
    if mt is None:
        continue
    _log_peak_verification(
        city=city, date_str=target,
        our_peak=float(actual),
        our_lat=pdata.get("_lat", 0),
        our_lon=pdata.get("_lon", 0),
        market_resolved=mt,
    )
    verified += 1
    gap = round(float(actual) - mt, 1)
    print(f"  {city}: our={actual}°C market={mt}°C gap={gap:+}°C")

print(f"\nVerified: {verified} cities matched by date")
