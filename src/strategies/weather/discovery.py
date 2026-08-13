"""
Dependency-light discovery helpers for Polymarket daily-temperature markets.

Kept stdlib-only so both the live monitor and the standalone verification
script share the exact same "today" computation and event-matching logic.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

# User's local timezone — used to decide what "today" means for display and
# date-token matching. The date-match token itself is built from the current
# date, so it stays deterministic.
DISPLAY_TIMEZONE: str = "Europe/Oslo"


def local_today(tz_name: str = DISPLAY_TIMEZONE) -> date:
    """Return today's date in the user's local timezone (Europe/Oslo).

    Falls back to the system date if the timezone database is unavailable.
    """
    try:
        return datetime.now(ZoneInfo(tz_name)).date()
    except Exception:
        return date.today()


def build_date_tokens(today: date | None = None) -> list[str]:
    """Build the date-match tokens for a given date.

    Covers both spellings found in the wild:

    * slug style: ``on-august-12-2026`` / ``on-aug-12-2026``
    * title style: ``August 12`` / ``Aug 12`` (with and without the year)
    """
    d = today or local_today()
    month_full = d.strftime("%B").lower()  # "august"
    month_abbr = d.strftime("%b").lower()  # "aug"
    day = d.day
    year = d.year

    return [
        f"on-{month_full}-{day}-{year}",
        f"on-{month_abbr}-{day}-{year}",
        f"on {month_full} {day}",
        f"on {month_abbr} {day}",
        f"{month_full} {day}, {year}",
        f"{month_abbr} {day}, {year}",
        f"{month_full} {day}",
        f"{month_abbr} {day}",
    ]


def is_today_highest_temperature_event(
    slug: str,
    title: str,
    today: date | None = None,
) -> bool:
    """Return True if an event is today's "Highest temperature" event.

    Matches ``highest-temperature`` (slug) or ``highest temperature`` (title)
    AND one of the date tokens for ``today``. ``lowest-temperature`` events
    never match.
    """
    text = f"{slug or ''} {title or ''}".lower()

    if "highest-temperature" not in text and "highest temperature" not in text:
        return False

    return any(token in text for token in build_date_tokens(today))
