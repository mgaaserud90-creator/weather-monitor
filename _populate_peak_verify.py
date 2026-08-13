"""Populate _peak_verification_log.json by comparing our archive peaks to Polymarket resolved outcomes.

Unit-aware rebuild:
  * Reads resolved markets through _load_market_resolved_details() so threshold
    markets ("X°C or higher") are never treated as resolved point temperatures.
  * US °F markets are stored with unit "F" and their native °F value — nothing
    is silently converted to °C.
  * Iterates ALL runs (not only yesterday) and rebuilds the whole log, so stale
    entries (e.g. a threshold misread as a 26°C point) are dropped on re-run.
"""
import json, sys, os
from datetime import datetime, timezone
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _model_quality_tracker import (
    _load_market_resolved_details,
    _load_log,
    _log_peak_verification,
    PEAK_VERIFICATION_LOG,
)

# Rebuild from scratch FIRST so stale/threshold entries disappear from the
# loader's view before it merges the peak-verification log as a source.
PEAK_VERIFICATION_LOG.write_text(
    json.dumps({"last_updated": "", "verifications": {}}, indent=2, ensure_ascii=False),
    encoding="utf-8",
)

resolved = _load_market_resolved_details()
print(f"Polymarket resolved markets (unit-aware, threshold-excluded): {len(resolved)}")

log = _load_log()
runs = log.get("runs", [])
if not runs:
    print("No runs in quality log.")
    sys.exit(1)

verified = 0
for entry in runs:
    run_date = entry.get("run_date", "")
    preds = entry.get("predictions", {})
    for city, pdata in sorted(preds.items()):
        sigma = pdata.get("strategies", {}).get("sigma", {})
        actual = sigma.get("actual_peak")
        if actual is None:
            continue
        city_base = city.split(",")[0].strip()
        target = pdata.get("_target_date", run_date)
        info = resolved.get((city, target)) or resolved.get((city_base, target))
        if info is None:
            continue
        if info.get("type") == "threshold":
            continue
        value = info.get("value")
        if value is None:
            continue
        _log_peak_verification(
            city=city,
            date_str=target,
            our_peak=float(actual),
            our_lat=pdata.get("_lat", 0),
            our_lon=pdata.get("_lon", 0),
            market_resolved=float(value),
            unit=(info.get("unit") or "C").upper(),
            bucket=info.get("bucket"),
        )
        verified += 1
        print(f"  {city} ({target}): our={float(actual)} market={float(value)} unit={(info.get('unit') or 'C').upper()}")

print(f"\nVerified: {verified} city/date pairs written to {PEAK_VERIFICATION_LOG}")
