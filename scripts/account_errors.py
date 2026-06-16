#!/usr/bin/env python3
"""Emit one line per error/failed row in every account's state.db.

Output format (read by the Errors tab as a shell-source log):
    <executed_at>  <severity>  account=<name>  type=<action_type>  msg=<reasoning|result>

severity = "error" when type='Error' or status='failed'; "warn" otherwise.
"""
import sqlite3
import sys
from pathlib import Path

ACCTS = Path("/root/reddit-automation/accounts")
LIMIT_PER_ACCOUNT = 50  # cap to avoid runaway tail on one chatty account

rows_out = []
for d in sorted(ACCTS.iterdir()):
    if not d.is_dir() or d.name.startswith("."):
        continue
    db = d / "state.db"
    if not db.exists():
        continue
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """SELECT executed_at, type, status, action_type, reasoning, result
               FROM actions_log
               WHERE status='failed' OR type='Error'
               ORDER BY executed_at DESC
               LIMIT ?""",
            (LIMIT_PER_ACCOUNT,),
        ).fetchall()
        for r in rows:
            ts = (r["executed_at"] or "").replace(" ", "T")
            msg = (r["reasoning"] or r["result"] or "").replace("\n", " ").strip()[:300]
            atype = r["action_type"] or r["type"]
            rows_out.append(
                (ts, f"{ts}  error  account={d.name}  type={atype}  msg={msg}")
            )
        conn.close()
    except Exception as e:
        rows_out.append(("", f"  error  account={d.name}  type=db_read  msg=could not read state.db: {type(e).__name__}: {e}"))

# Sort newest first so the Errors tab (which also sorts by ts) gets them in order
rows_out.sort(key=lambda t: t[0], reverse=True)
for _, line in rows_out:
    print(line)
