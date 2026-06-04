"""Hard rate limits for warmup actions.

Agent must call `assert_under_limit(state_db, action_type)` before each
meaningful Reddit-side action. Raises ThrottleViolation if calling would
exceed the per-action limits derived from the karma growth strategy.

Limits per the strategy doc's Risk Model:
  - velocity signal trips at >5 like-actions/hour or >20/day
  - comments must be slow (real users don't burst-comment)
  - posts must be very rare (1-2/day max for warmed accounts)

These limits are conservative. Better to under-act than to trip the filter.
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path


class ThrottleViolation(Exception):
    """Raised when an action would exceed strategy-doc rate limits."""


LIMITS = {
    "upvote":    {"per_hour": 5, "per_day": 20, "min_gap_seconds": 30},
    "save":      {"per_hour": 5, "per_day": 20, "min_gap_seconds": 30},
    "subscribe": {"per_hour": 5, "per_day": 10, "min_gap_seconds": 60},
    "comment":   {"per_hour": 3, "per_day": 8,  "min_gap_seconds": 240},
    "post":      {"per_hour": 1, "per_day": 2,  "min_gap_seconds": 14400},
}

# Global all-actions cap (cumulative across types) per strategy doc
GLOBAL_PER_DAY = 25
GLOBAL_PER_HOUR = 10


def _count_in_window(state_db: Path, action_type: str | None, window_seconds: int) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=window_seconds)).isoformat()
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        if action_type is None:
            q = ("SELECT COUNT(*) FROM actions_log WHERE type='Action' AND "
                 "status IN ('done','submitted','confirmed') AND executed_at > ?")
            return conn.execute(q, (cutoff,)).fetchone()[0]
        return conn.execute(
            "SELECT COUNT(*) FROM actions_log WHERE type='Action' AND action_type=? "
            "AND status IN ('done','submitted','confirmed') AND executed_at > ?",
            (action_type, cutoff),
        ).fetchone()[0]
    finally:
        conn.close()


def _seconds_since_last(state_db: Path, action_type: str) -> float:
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT executed_at FROM actions_log WHERE type='Action' AND action_type=? "
            "AND status IN ('done','submitted','confirmed') "
            "ORDER BY executed_at DESC LIMIT 1",
            (action_type,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not row[0]:
        return float("inf")
    last = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - last).total_seconds()


def assert_under_limit(state_db: Path, action_type: str) -> None:
    """Raise ThrottleViolation if doing one more `action_type` would exceed limits.
    Also enforces the global cross-type caps."""
    state_db = Path(state_db)
    if not state_db.exists():
        return  # fresh account — no history to throttle on

    # Global caps first
    if _count_in_window(state_db, None, 3600) >= GLOBAL_PER_HOUR:
        raise ThrottleViolation(
            f"global: already {GLOBAL_PER_HOUR}+ total actions in last hour"
        )
    if _count_in_window(state_db, None, 86400) >= GLOBAL_PER_DAY:
        raise ThrottleViolation(
            f"global: already {GLOBAL_PER_DAY}+ total actions in last 24h"
        )

    if action_type not in LIMITS:
        return
    L = LIMITS[action_type]

    last_gap = _seconds_since_last(state_db, action_type)
    if last_gap < L["min_gap_seconds"]:
        raise ThrottleViolation(
            f"min_gap: last {action_type} was {last_gap:.0f}s ago; "
            f"need >= {L['min_gap_seconds']}s"
        )
    if _count_in_window(state_db, action_type, 3600) >= L["per_hour"]:
        raise ThrottleViolation(
            f"hourly: already {L['per_hour']}+ {action_type}s in last hour"
        )
    if _count_in_window(state_db, action_type, 86400) >= L["per_day"]:
        raise ThrottleViolation(
            f"daily: already {L['per_day']}+ {action_type}s in last 24h"
        )


def current_usage(state_db: Path) -> dict:
    """Return dict of current usage vs limits for dashboard rendering."""
    state_db = Path(state_db)
    out = {"global": {
        "hour": _count_in_window(state_db, None, 3600),
        "day": _count_in_window(state_db, None, 86400),
        "limit_hour": GLOBAL_PER_HOUR, "limit_day": GLOBAL_PER_DAY,
    }, "per_action": {}}
    if not state_db.exists():
        return out
    for at, L in LIMITS.items():
        out["per_action"][at] = {
            "hour": _count_in_window(state_db, at, 3600),
            "day": _count_in_window(state_db, at, 86400),
            "limit_hour": L["per_hour"], "limit_day": L["per_day"],
            "since_last": _seconds_since_last(state_db, at),
            "min_gap": L["min_gap_seconds"],
        }
    return out
