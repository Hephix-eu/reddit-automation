"""SQLite state store for one warmup account.

Per-account DB at `accounts/<username>/state.db`. One table, all action types
flow through it (Session, Draft, Action, StateSnapshot, Error) — same shape
covering Session, Draft, Action, StateSnapshot, Error rows.

Why one table: simple chronological view, easy WHERE filters, no joins.
Trade-off: rows mix concerns (drafts vs actions vs sessions). Worth it for
the simplicity at this scale (~50-200 rows per account over 14 days).
"""

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS actions_log (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,        -- Session | Draft | Action | StateSnapshot | Error
    status          TEXT NOT NULL,        -- scheduled | pending_review | approved | rejected | submitted | failed | done
    day             INTEGER,
    action_type     TEXT,                 -- browse | upvote | comment | post | reconnaissance | subscribe | save | ...
    subreddit       TEXT,
    target_url      TEXT,
    draft_content   TEXT,
    submitted_content TEXT,
    reasoning       TEXT,
    session_id      TEXT,                 -- groups rows from same browsing session
    scheduled_for   TEXT,                 -- ISO8601 UTC
    executed_at     TEXT,                 -- ISO8601 UTC
    result          TEXT,
    profile_id      TEXT,
    name            TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_session_id   ON actions_log(session_id);
CREATE INDEX IF NOT EXISTS idx_type_status  ON actions_log(type, status);
CREATE INDEX IF NOT EXISTS idx_executed_at  ON actions_log(executed_at DESC);
CREATE INDEX IF NOT EXISTS idx_day          ON actions_log(day);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return str(uuid.uuid4())


@contextmanager
def connect(db_path: Path):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init(db_path: Path) -> None:
    """Create DB + schema if missing. Idempotent."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect(db_path) as c:
        c.executescript(SCHEMA)


def insert(db_path: Path, **fields) -> str:
    """Insert one row. Returns generated id.

    Required: type, status. Everything else optional.
    `executed_at` auto-fills with `now_iso()` if missing.
    """
    fields.setdefault("id", new_id())
    fields.setdefault("executed_at", now_iso())
    cols = list(fields.keys())
    placeholders = ", ".join("?" for _ in cols)
    sql = f"INSERT INTO actions_log ({', '.join(cols)}) VALUES ({placeholders})"
    with connect(db_path) as c:
        c.execute(sql, [fields[k] for k in cols])
    return fields["id"]


def update_status(db_path: Path, row_id: str, status: str, **extras) -> None:
    sets = ["status = ?"] + [f"{k} = ?" for k in extras]
    values = [status, *extras.values(), row_id]
    with connect(db_path) as c:
        c.execute(f"UPDATE actions_log SET {', '.join(sets)} WHERE id = ?", values)


def recent(db_path: Path, limit: int = 20) -> list[dict]:
    with connect(db_path) as c:
        rows = c.execute(
            "SELECT * FROM actions_log ORDER BY executed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def latest_session(db_path: Path) -> Optional[dict]:
    """Most recent Type=Session row, or None."""
    with connect(db_path) as c:
        row = c.execute(
            "SELECT * FROM actions_log WHERE type = 'Session' "
            "ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def pending_drafts(db_path: Path, older_than_minutes: int = 30) -> list[dict]:
    """Drafts with Status=approved that have aged past the submission delay.

    These are the ones the agent should actually submit this session.
    """
    with connect(db_path) as c:
        rows = c.execute(
            "SELECT * FROM actions_log "
            "WHERE type = 'Draft' AND status = 'approved' "
            "AND datetime(executed_at) <= datetime('now', ?) "
            "ORDER BY executed_at ASC",
            (f"-{older_than_minutes} minutes",),
        ).fetchall()
    return [dict(r) for r in rows]


def count_sessions_today(db_path: Path) -> int:
    with connect(db_path) as c:
        row = c.execute(
            "SELECT COUNT(*) AS n FROM actions_log "
            "WHERE type = 'Session' AND date(executed_at) = date('now')"
        ).fetchone()
    return row["n"]


def last_post_at(db_path: Path) -> Optional[datetime]:
    """When did the agent last submit a post (Action_Type=post)? For 48hr-rule enforcement."""
    with connect(db_path) as c:
        row = c.execute(
            "SELECT executed_at FROM actions_log "
            "WHERE type = 'Action' AND action_type = 'post' AND status = 'submitted' "
            "ORDER BY executed_at DESC LIMIT 1"
        ).fetchone()
    if not row:
        return None
    return datetime.fromisoformat(row["executed_at"])


def total_karma(db_path: Path) -> int:
    """Sum karma deltas across all action rows where Result contains a number.

    Convention: `result` field may include "karma=+3" or similar. Best-effort parse.
    """
    karma = 0
    with connect(db_path) as c:
        rows = c.execute(
            "SELECT result FROM actions_log WHERE result IS NOT NULL"
        ).fetchall()
    for r in rows:
        # Parse simple "karma=+N" / "karma=-N" patterns
        for token in (r["result"] or "").split():
            if token.startswith("karma="):
                try:
                    karma += int(token.split("=", 1)[1])
                except ValueError:
                    pass
    return karma
