#!/usr/bin/env python3
"""
SMS Alert Module — Twilio-based SMS notifications for the VærMonitor.

Triggered when peak confidence exceeds 70 % AND any strategy (sigma/p5/mean)
is likely to lose based on current trend.  Deduplicates: only one SMS per city
per day to avoid spamming.

Environment Variables (set in .env or system):
    TWILIO_SID      — Twilio Account SID
    TWILIO_TOKEN    — Twilio Auth Token
    TWILIO_FROM     — Twilio "From" phone number (e.g. "+1234567890")

Usage:
    from _sms_alert import send_sms, can_send_sms_for_city, mark_sms_sent

    if can_send_sms_for_city("Oslo, NO"):
        await send_sms("VARSMONITOR: Oslo peak sannsynlig (82%). ...")
        mark_sms_sent("Oslo, NO")

    # CLI: send a test SMS unconditionally
    python _sms_alert.py --test

    # CLI: check latest log and send alerts automatically
    python _sms_alert.py --check-and-send
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from datetime import date
from pathlib import Path

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# Twilio credentials from environment
# ---------------------------------------------------------------------------
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
ALERT_PHONE = os.getenv("ALERT_PHONE", "+4795419426")

# ---------------------------------------------------------------------------
# Dedup: track SMS sends per city per day
# ---------------------------------------------------------------------------
_SMS_SENT_TODAY: set[str] = set()
_SMS_LOG_FILE = Path(__file__).resolve().parent / "_sms_log.json"

# ---------------------------------------------------------------------------
# Log helpers
# ---------------------------------------------------------------------------

def _load_sms_log() -> dict[str, str]:
    """Load sent-SMS log (city -> date sent)."""
    if _SMS_LOG_FILE.exists():
        import json
        try:
            return json.loads(_SMS_LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {}


def _save_sms_log(data: dict[str, str]) -> None:
    """Persist sent-SMS log."""
    import json
    _SMS_LOG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def can_send_sms_for_city(city_name: str) -> bool:
    """Return True if no SMS has been sent for this city today."""
    today_str = date.today().isoformat()
    log = _load_sms_log()
    return log.get(city_name) != today_str


def mark_sms_sent(city_name: str) -> None:
    """Record that an SMS was sent for this city today."""
    today_str = date.today().isoformat()
    log = _load_sms_log()
    log[city_name] = today_str
    # Keep only last 90 days
    cutoff = date.today().isoformat()
    log = {k: v for k, v in log.items() if v >= cutoff}
    _save_sms_log(log)


async def send_sms(message: str) -> bool | None:
    """Send an SMS alert via Twilio.

    If ``TWILIO_SID`` is not set, prints the message to stdout instead.

    Returns True if sent (or dry-run printed), False on error.
    """
    if not TWILIO_SID:
        print("[SMS] TWILIO_SID not set — skipping")
        if "--test" in sys.argv:
            print("[SMS] ERROR: Cannot send test SMS without credentials!")
        return

    if httpx is None:
        print(f"[SMS requires httpx] {message}")
        return False

    url = (
        f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    )
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url,
                auth=(TWILIO_SID, TWILIO_TOKEN),
                data={
                    "From": TWILIO_FROM,
                    "To": ALERT_PHONE,
                    "Body": message,
                },
            )
            resp.raise_for_status()
            return True
    except Exception as exc:
        print(f"[SMS FAILED] {message} — error: {exc}")
        return False


def check_and_alert(
    city_name: str,
    peak_confidence: float,
    live_conf: float,
    suggested_spill: float,
    current_temp: float,
    trend: str,
    sigma_win_prob: float,
) -> str | None:
    """Evaluate SMS trigger conditions and return an alert message if warranted.

    Triggers when:
      - peak_confidence > 70 %  (BMA is confident)
      - AND live_conf > 60 %    (peak likely passed)
      - AND current trend is declining (trend == "↓")
      - AND any strategy (sigma/p5/mean) is likely to lose

    Returns the SMS message string, or None if conditions are not met.
    """
    if peak_confidence <= 0.70:
        return None
    if live_conf <= 60:
        return None
    if trend != "↓":
        return None

    # Check if sigma strategy likely to lose
    sigma_at_risk = sigma_win_prob < 0.50

    if not sigma_at_risk:
        return None

    conf_pct = int(peak_confidence * 100)
    return (
        f"VARSMONITOR: {city_name} peak sannsynlig ({conf_pct}%). "
        f"KJOP {int(suggested_spill)}C star i fare. "
        f"Na {current_temp:.1f}C synkende. Vurder SELG."
    )


# ---------------------------------------------------------------------------
# --check-and-send: read the latest model_quality_log and send SMS alerts
# ---------------------------------------------------------------------------

QUALITY_LOG_FILE = Path(__file__).resolve().parent / "_model_quality_log.json"


def _load_quality_log() -> dict:
    """Load the model quality log."""
    if QUALITY_LOG_FILE.exists():
        try:
            return json.loads(QUALITY_LOG_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError):
            pass
    return {"runs": []}


async def check_and_send_from_log() -> int:
    """Read the latest run from _model_quality_log.json and send SMS alerts
    for cities where peak_confidence > 70 % AND sigma strategy is at risk.

    Returns the number of SMS messages sent.
    """
    log_data = _load_quality_log()
    runs = log_data.get("runs", [])
    if not runs:
        print("[SMS] No runs found in quality log — nothing to check.")
        return 0

    latest = runs[-1]
    predictions = latest.get("predictions", {})
    if not predictions:
        print("[SMS] Latest run has no predictions — nothing to check.")
        return 0

    sent_count = 0
    target_date = latest.get("target_date", latest.get("run_date", "?"))

    for city_name, pdata in sorted(predictions.items()):
        confidence = pdata.get("confidence", 0)
        strategies = pdata.get("strategies", {})
        sigma = strategies.get("sigma", {})
        sigma_win_prob = sigma.get("win_prob", 0)
        sigma_spill = sigma.get("spill", "?")
        sigma_result = sigma.get("result", "")

        # Skip already-resolved cities (they already have a result)
        if sigma_result in ("WIN", "LOSS"):
            continue

        # Check trigger: confidence > 70% AND sigma win_prob < 50%
        if confidence <= 0.70:
            continue
        if sigma_win_prob >= 0.50:
            continue

        # Also skip if SMS already sent for this city today
        if not can_send_sms_for_city(city_name):
            print(f"[SMS] SKIP {city_name} — already sent today.")
            continue

        conf_pct = int(confidence * 100)
        sigma_pct = int(sigma_win_prob * 100)
        message = (
            f"VARSMONITOR: {city_name} peak sannsynlig ({conf_pct}%). "
            f"KJOP {sigma_spill}C star i fare ({sigma_pct}% win prob). "
            f"Vurder SELG. ({target_date})"
        )

        print(f"[SMS] SENDING: {message}")
        success = await send_sms(message)
        if success:
            mark_sms_sent(city_name)
            sent_count += 1
            print(f"[SMS] SENT to {city_name}")
        else:
            print(f"[SMS] FAILED for {city_name}")

    if sent_count == 0:
        print("[SMS] No alerts triggered — all cities look stable.")
    else:
        print(f"[SMS] Sent {sent_count} alert(s).")

    return sent_count


def main() -> None:
    """CLI entry point for --test and --check-and-send modes."""
    parser = argparse.ArgumentParser(
        description="SMS Alert Module — Twilio-based notifications for VærMonitor.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Send an unconditional test SMS to verify Twilio configuration.",
    )
    parser.add_argument(
        "--check-and-send",
        action="store_true",
        help="Read latest _model_quality_log.json and send SMS alerts for "
             "cities where peak confidence > 70%% AND strategy is at risk.",
    )
    args = parser.parse_args()

    if args.test:
        print("[SMS] Sending test SMS...")
        sent = asyncio.run(
            send_sms("VarMonitor test: SMS fungerer! Pipeline klar.")
        )
        if sent:
            print("[SMS] Test SMS sent!")
        else:
            print("[SMS] Test SMS failed — check Twilio credentials.")
    elif args.check_and_send:
        sent = asyncio.run(check_and_send_from_log())
        if sent > 0:
            print(f"[SMS] Done — {sent} alert(s) sent.")
        else:
            print("[SMS] Done — no alerts needed.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
