"""Print summary of peak verification results."""
import json

log_path = "_peak_verification_log.json"
try:
    pv = json.load(open(log_path))
except (FileNotFoundError, json.JSONDecodeError):
    print("No verification data found.")
    exit(0)

disc = []
verifications = pv.get("verifications", {})
for city_key, entry in verifications.items():
    gap = entry.get("gap", 0)
    if abs(gap) > 0.5:
        disc.append({
            "city": city_key,
            "verdict": entry.get("verdict", "?"),
            "gap": gap,
        })

if disc:
    print(f"WARNING: {len(disc)} discrepancy(s) found (>0.5C)!")
    for d in disc:
        print(f"  {d['verdict']} {d['city']}: gap={d['gap']:+.1f}C")
else:
    ok_count = len(verifications)
    print(f"All {ok_count} verified peaks match Polymarket within 0.5C tolerance.")
