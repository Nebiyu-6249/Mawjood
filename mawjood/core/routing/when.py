"""Turning "tomorrow evening" into a search window.

Consumers give relative, fuzzy times. Aggregators want timestamps. This is the
translation, and it is deliberately generous: a window that is too narrow finds
nothing and sends a consumer to a human for no reason, which is a worse failure
than offering 6:30 when they said 6.

All arithmetic is in the operator's timezone (Asia/Dubai by default) because
"tomorrow evening" is a wall-clock idea, not a UTC one. A booking an hour out
because the window was computed in UTC is exactly the bug the timezone-aware
columns exist to prevent.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DEFAULT_TZ = ZoneInfo("Asia/Dubai")

# Parts of the day, as a local person means them.
DAYPARTS: dict[str, tuple[time, time]] = {
    "morning": (time(8, 0), time(12, 0)),
    "midday": (time(11, 30), time(14, 0)),
    "afternoon": (time(12, 0), time(17, 0)),
    "evening": (time(17, 0), time(22, 0)),
    "night": (time(19, 0), time(23, 0)),
}

# When no time is given at all, search the working part of the day rather than
# midnight to midnight.
DEFAULT_DAY = (time(9, 0), time(21, 0))

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_CLOCK = re.compile(r"^(?P<hour>[01]?\d|2[0-3]):(?P<minute>[0-5]\d)$")

# Either side of a stated time, so "6pm" finds 5:30 and 6:30 too.
CLOCK_SPREAD = timedelta(minutes=90)


def resolve_window(
    date_text: str | None,
    time_text: str | None,
    *,
    tz: ZoneInfo = DEFAULT_TZ,
    now: datetime | None = None,
) -> tuple[datetime, datetime]:
    """Return a UTC window for a consumer's words."""
    local_now = (now or datetime.now(UTC)).astimezone(tz)
    day = _resolve_day(date_text, local_now)

    if time_text:
        clock = _CLOCK.match(time_text.strip())
        if clock:
            at = time(int(clock.group("hour")), int(clock.group("minute")))
            centre = datetime.combine(day, at, tzinfo=tz)
            return (
                (centre - CLOCK_SPREAD).astimezone(UTC),
                (centre + CLOCK_SPREAD).astimezone(UTC),
            )
        part = DAYPARTS.get(time_text.strip().lower())
        if part is not None:
            start, end = part
            return (
                datetime.combine(day, start, tzinfo=tz).astimezone(UTC),
                datetime.combine(day, end, tzinfo=tz).astimezone(UTC),
            )

    start, end = DEFAULT_DAY
    window_start = datetime.combine(day, start, tzinfo=tz)
    # Never propose a time that has already passed today.
    if window_start < local_now:
        window_start = local_now.replace(second=0, microsecond=0) + timedelta(minutes=30)
    window_end = datetime.combine(day, end, tzinfo=tz)
    if window_end <= window_start:
        window_end = window_start + timedelta(hours=4)
    return window_start.astimezone(UTC), window_end.astimezone(UTC)


def _resolve_day(date_text: str | None, local_now: datetime) -> date:
    today = local_now.date()
    if not date_text:
        return today

    text = date_text.strip().lower()
    if text in ("today", "tonight"):
        return today
    if text == "tomorrow":
        return today + timedelta(days=1)
    if text == "day after tomorrow":
        return today + timedelta(days=2)
    if text == "weekend":
        # The UAE weekend is Saturday and Sunday; Friday evening is its edge.
        days_ahead = (5 - today.weekday()) % 7
        return today + timedelta(days=days_ahead or 7)

    if text in _WEEKDAYS:
        target = _WEEKDAYS[text]
        # "Thursday" said on a Thursday means next Thursday, not five minutes ago.
        days_ahead = (target - today.weekday()) % 7 or 7
        return today + timedelta(days=days_ahead)

    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return today


__all__ = ["CLOCK_SPREAD", "DAYPARTS", "DEFAULT_DAY", "DEFAULT_TZ", "resolve_window"]
