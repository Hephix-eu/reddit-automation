"""Detect 'needs human attention' signals from per-account SQLite.

Returns a dict suitable for accounts/<user>/watch.json, or None if no signal.
Distinct from banned.json (which is account-killed): watch.json is yellow,
meaning the agent should keep running but you should look at it.

Signals (any one in the last N hours triggers a watch entry):
  - Reddit served a captcha (`Error/captcha`)
  - Authentication 403 (`Error/auth_warning`)
  - `USER_REQUIRED` comment-submit failure
  - 3+ consecutive failed sessions (no `done` Session row in last 48h)
"""
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOOKBACK_HOURS = 24


def _rows_since(state_db: Path, since_iso: str, sql: str, *args):
    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, (*args, since_iso)).fetchall()]
    finally:
        conn.close()


def detect(state_db: Path) -> dict | None:
    """Return a watch payload dict, or None if no concerning signals."""
    state_db = Path(state_db)
    if not state_db.exists():
        return None
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()

    signals = []

    # 1) Captcha
    captcha_rows = _rows_since(
        state_db, cutoff,
        "SELECT executed_at, reasoning FROM actions_log WHERE type='Error' "
        "AND action_type LIKE '%captcha%' AND executed_at > ?",
    )
    for r in captcha_rows:
        signals.append({
            "kind": "captcha",
            "at": r["executed_at"],
            "detail": (r["reasoning"] or "")[:120],
        })

    # 2) Auth 403 / USER_REQUIRED — only flag if 2+ in window (singletons are noise)
    auth_rows = _rows_since(
        state_db, cutoff,
        "SELECT executed_at, action_type, reasoning, result FROM actions_log "
        "WHERE type='Error' AND (action_type LIKE '%auth%' OR action_type LIKE '%login%' "
        "OR reasoning LIKE '%USER_REQUIRED%' OR reasoning LIKE '%403%') "
        "AND executed_at > ?",
    )
    if len(auth_rows) >= 2:
        for r in auth_rows:
            signals.append({
                "kind": "auth_failure",
                "at": r["executed_at"],
                "detail": ((r.get("reasoning") or r.get("result") or "") + " [" + (r["action_type"] or "") + "]")[:120],
            })

    # 3) Streak of failed sessions
    cutoff_48 = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    sess_rows = _rows_since(
        state_db, cutoff_48,
        "SELECT executed_at, status, result FROM actions_log "
        "WHERE type='Session' AND executed_at > ? ORDER BY executed_at DESC",
    )
    if sess_rows and len(sess_rows) >= 3 and all(s["status"] in ("failed", "partial") for s in sess_rows[:3]):
        signals.append({
            "kind": "failed_streak",
            "at": sess_rows[0]["executed_at"],
            "detail": f"last 3 sessions all status in (failed,partial) within 48h",
        })

    if not signals:
        return None

    # Pick worst severity by signal kind ranking
    severity_rank = {"captcha": 3, "failed_streak": 2, "auth_failure": 1}
    worst = max(signals, key=lambda s: severity_rank.get(s["kind"], 0))

    return {
        "status": "watch",
        "severity": worst["kind"],
        "first_observed": min(s["at"] for s in signals),
        "last_observed": max(s["at"] for s in signals),
        "signal_count": len(signals),
        "signals": signals[:10],  # cap to avoid huge files
        "auto_detected_at": datetime.now(timezone.utc).isoformat(),
    }
