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
"""

from __future__ import annotations

import os
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


async def send_sms(message: str) -> bool:
    """Send an SMS alert via Twilio.

    If ``TWILIO_SID`` is not set, prints the message to stdout instead.

    Returns True if sent (or dry-run printed), False on error.
    """
    if not TWILIO_SID:
        print(f"[SMS would send] {message}")
        return True

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
