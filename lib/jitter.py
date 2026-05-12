"""Next-session jitter logic.

Pure functions. Given current state, compute when the next session should fire.
Implementation matches the defaults in AGENT_PROMPT.md:
  - 1 session/day usually, 2 on Day 8 (push-past-100 day)
  - Active hours window 08:00-23:00 in account timezone
  - 3-7hr gap if multi-session day
  - ±20min jitter on the target time
  - 20% chance of skip-day (push +24-36hr)
  - Never round-minute schedules
"""

import random
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


SESSIONS_PER_DAY = {
    1: 1, 2: 1, 3: 1, 4: 1, 5: 1, 6: 1, 7: 1,
    8: 2,                              # push-past-100 day
    9: 1, 10: 1, 11: 1, 12: 1, 13: 1, 14: 1,
}


def next_run(
    now: datetime,
    day: int,
    sessions_today_already: int,
    tz: str = "Europe/Riga",
    active_hour_start: int = 8,
    active_hour_end: int = 23,
    rng: random.Random | None = None,
) -> datetime:
    """Return tz-aware datetime for next session start.

    `now` may be naive or tz-aware; assumed UTC if naive.
    Returned datetime is in `tz`.
    """
    r = rng or random
    tzinfo = ZoneInfo(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=ZoneInfo("UTC"))
    now_local = now.astimezone(tzinfo)

    target_sessions = SESSIONS_PER_DAY.get(day, 1)

    if sessions_today_already >= target_sessions:
        # Done for today — pick a random time tomorrow in active window
        candidate = _random_time_in_window(
            now_local.date() + timedelta(days=1),
            active_hour_start,
            active_hour_end,
            tzinfo,
            r,
        )
    else:
        # More sessions due today — gap 3-7 hours from now
        gap_hours = r.uniform(3.0, 7.0)
        candidate = now_local + timedelta(hours=gap_hours)
        candidate = _clamp_to_active_window(
            candidate, active_hour_start, active_hour_end, tzinfo, r
        )

    # ±20 min jitter
    candidate += timedelta(minutes=r.uniform(-20, 20))
    candidate = _clamp_to_active_window(
        candidate, active_hour_start, active_hour_end, tzinfo, r
    )

    # 20% skip-day chance — push forward 24-36 extra hours
    if r.random() < 0.20:
        candidate += timedelta(hours=r.uniform(24, 36))
        candidate = _clamp_to_active_window(
            candidate, active_hour_start, active_hour_end, tzinfo, r
        )

    # Never round-minute. Snap to a 1-59 minute that isn't 0/15/30/45.
    candidate = _avoid_round_minutes(candidate, r)

    return candidate


def _random_time_in_window(
    on_date,
    start_h: int,
    end_h: int,
    tzinfo: ZoneInfo,
    r: random.Random,
) -> datetime:
    """Pick a random time on `on_date` inside [start_h, end_h)."""
    hour = r.randint(start_h, end_h - 1)
    minute = r.randint(0, 59)
    return datetime.combine(on_date, time(hour, minute), tzinfo=tzinfo)


def _clamp_to_active_window(
    dt: datetime,
    start_h: int,
    end_h: int,
    tzinfo: ZoneInfo,
    r: random.Random,
) -> datetime:
    """If `dt` lands outside [start_h, end_h), push to next active window opening + small jitter."""
    local = dt.astimezone(tzinfo)
    if local.hour < start_h:
        # Same day, push to opening hour + 0-90 min jitter
        return local.replace(hour=start_h, minute=r.randint(0, 90 % 60), second=0, microsecond=0) + \
            timedelta(minutes=r.randint(0, 90))
    if local.hour >= end_h:
        # Tomorrow's opening
        tomorrow = (local.date() + timedelta(days=1))
        return datetime.combine(
            tomorrow,
            time(start_h, r.randint(0, 90)),
            tzinfo=tzinfo,
        )
    return local


def _avoid_round_minutes(dt: datetime, r: random.Random) -> datetime:
    if dt.minute in (0, 15, 30, 45):
        # Shift by ±3 min to a non-round minute
        offset_choices = [m for m in range(-7, 8) if m and (dt.minute + m) % 5 not in (0,)]
        dt = dt + timedelta(minutes=r.choice(offset_choices))
    return dt.replace(second=0, microsecond=0)
