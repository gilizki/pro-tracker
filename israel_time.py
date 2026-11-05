"""Israel time and the tracker day, on the server.

This is the server-side copy of the rule in frontend/day.js: a day runs
02:00–01:59 Israel time (Asia/Jerusalem), daylight saving time included.
tests/test_notifications.py checks that both implementations agree.

Requires the ``tzdata`` package on systems without a time-zone database (Windows).
"""
import datetime as dt
from zoneinfo import ZoneInfo

TIME_ZONE = ZoneInfo("Asia/Jerusalem")
DAY_START_HOUR = 2  # same as DAY_START_HOUR in frontend/day.js


def israel_now() -> dt.datetime:
    """The current time in Israel (timezone-aware). Used as a FastAPI dependency."""
    return dt.datetime.now(TIME_ZONE)


def tracker_day(moment: dt.datetime) -> dt.date:
    """The tracker day of a timezone-aware moment: Israel's wall-clock date, minus
    one day before 02:00 (reads the wall clock, so DST nights are handled)."""
    local = moment.astimezone(TIME_ZONE)
    day = local.date()
    if local.hour < DAY_START_HOUR:
        day -= dt.timedelta(days=1)
    return day
