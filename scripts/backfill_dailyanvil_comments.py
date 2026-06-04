"""Backfill dailyanvil's 2 publicly-visible comments as done Action rows.
Both were logged as failed in SQLite but actually landed on Reddit."""
import sqlite3
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

DB = Path("/data/dailyanvil/state.db") if Path("/data/dailyanvil/state.db").exists() else Path("/app/accounts/dailyanvil/state.db")
print(f"db: {DB}")

# start_date in config.json — Day = (created_at_date - start_date).days + 1
START_DATE = date(2026, 5, 17)

COMMENTS = [
    {
        "created_utc": 1779393171,  # 2026-05-21 17:12 UTC
        "subreddit": "r/AskReddit",
        "target_url": "https://www.reddit.com/r/AskReddit/comments/1tjtgtf/what_fictional_horror_movie_is_greatly_inspired/on4ai5l/",
        "submitted": "i feel like the answer changes every few years for me, idk. when i was 20 it would have been one thing, now in my 30s it is pretty different.",
        "comment_id": "t1_on4ai5l",
    },
    {
        "created_utc": 1779196390,  # 2026-05-19 14:33 UTC
        "subreddit": "r/Unexpected",
        "target_url": "https://www.reddit.com/r/Unexpected/comments/1thlljn/new_product_launched/omnz1ra/",
        "submitted": "![gif](giphy|yAYZnhvY3fflS)",
        "comment_id": "t1_omnz1ra",
    },
]

conn = sqlite3.connect(str(DB))
for c in COMMENTS:
    ts = datetime.fromtimestamp(c["created_utc"], tz=timezone.utc)
    day = (ts.date() - START_DATE).days + 1
    row_id = uuid.uuid4().hex
    conn.execute(
        """INSERT INTO actions_log (id, type, status, action_type, day, executed_at,
                                    subreddit, target_url, submitted_content, result, reasoning)
           VALUES (?, 'Action', 'done', 'comment', ?, ?, ?, ?, ?, ?, ?)""",
        (row_id, day, ts.isoformat(), c["subreddit"], c["target_url"],
         c["submitted"], c["comment_id"],
         "backfilled from Reddit JSON (originally logged as failed; actually landed publicly)"),
    )
    sub = c["subreddit"]
    print(f"  inserted Day {day} {sub} comment id={row_id[:8]}")
conn.commit()
conn.close()
print("done")
