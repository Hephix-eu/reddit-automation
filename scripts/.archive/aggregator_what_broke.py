"""Per-account 'what is not working' view — aggregates from SQLite + log + state."""
import os, sys, json, sqlite3, subprocess, re
from pathlib import Path
from collections import Counter, defaultdict
from datetime import datetime, timezone, timedelta

REPO = Path("/root/reddit-automation") if Path("/root/reddit-automation").exists() else Path(__file__).resolve().parent.parent
ACCOUNTS = REPO / "accounts"
AGENT_LOG = Path("/var/log/reddit-agent.log") if Path("/var/log/reddit-agent.log").exists() else None

# Per-account user list (skip dot-files)
accounts = sorted([d.name for d in ACCOUNTS.iterdir() if d.is_dir() and not d.name.startswith(".")])

print("=" * 78)
print("PER-ACCOUNT AGGREGATOR — what is broken right now")
print(f"as of {datetime.now(timezone.utc).isoformat()}")
print("=" * 78)

for u in accounts:
    d = ACCOUNTS / u
    state = d / "state.db"
    print(f"\n┌────────────── {u} ──────────────")

    # Flags
    flags = []
    for fname, label in [("banned.json", "BANNED"), ("watch.json", "WATCH-manual"),
                         ("manual.json", "MANUAL"), ("pause", "PAUSED")]:
        if (d / fname).exists():
            flags.append(label)
    if flags:
        print(f"│ flags: {', '.join(flags)}")

    if not state.exists():
        print(f"│ no state.db")
        continue

    conn = sqlite3.connect(f"file:{state}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    # Last session
    sess = conn.execute(
        "SELECT executed_at, day, status, substr(result, 1, 120) AS r "
        "FROM actions_log WHERE type='Session' "
        "ORDER BY executed_at DESC LIMIT 1"
    ).fetchone()
    if sess:
        print(f"│ last session: {sess['executed_at']}  day={sess['day']}  status={sess['status']}")
        print(f"│   → {sess['r']}")
    else:
        print(f"│ no sessions yet")

    # Action success rate last 7 days
    cutoff7 = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    rows = conn.execute(
        "SELECT action_type, status, COUNT(*) AS n FROM actions_log "
        "WHERE type='Action' AND executed_at > ? GROUP BY action_type, status",
        (cutoff7,),
    ).fetchall()
    by_action = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by_action[r['action_type']][r['status']] += r['n']
    if by_action:
        print(f"│ actions (last 7d) — done / failed / skipped:")
        for at in sorted(by_action.keys()):
            cnts = by_action[at]
            done = cnts.get('done', 0); failed = cnts.get('failed', 0); skipped = cnts.get('done', 0) if at == 'skipped' else 0
            shadow = cnts.get('shadow_rejected', 0); submitted = cnts.get('submitted', 0)
            parts = []
            if done and at != 'skipped': parts.append(f"{done} done")
            if submitted: parts.append(f"{submitted} submitted")
            if failed: parts.append(f"{failed} failed")
            if shadow: parts.append(f"{shadow} shadow_rejected")
            if at == 'skipped' and cnts.get('done'): parts.append(f"{cnts['done']} times (throttle/allowlist)")
            print(f"│   {at:14}: {', '.join(parts) or '—'}")

    # Errors last 7 days — grouped by type
    err_rows = conn.execute(
        "SELECT action_type, COUNT(*) AS n FROM actions_log "
        "WHERE type='Error' AND executed_at > ? GROUP BY action_type ORDER BY n DESC",
        (cutoff7,),
    ).fetchall()
    if err_rows:
        print(f"│ errors (last 7d):")
        for r in err_rows[:8]:
            print(f"│   {r['action_type']:25} ×{r['n']}")

    # Latest 3 distinct error reasons
    err_sample = conn.execute(
        "SELECT executed_at, action_type, substr(coalesce(reasoning, result, ''), 1, 100) AS detail "
        "FROM actions_log WHERE type='Error' AND executed_at > ? "
        "ORDER BY executed_at DESC LIMIT 5",
        (cutoff7,),
    ).fetchall()
    if err_sample:
        print(f"│ recent error samples:")
        for r in err_sample:
            print(f"│   [{r['executed_at'][:19]}] {r['action_type']}: {r['detail']}")

    # Last shadowban check
    sb = conn.execute(
        "SELECT executed_at, result FROM actions_log "
        "WHERE type='StateSnapshot' AND action_type='shadowban_check' "
        "ORDER BY executed_at DESC LIMIT 1"
    ).fetchone()
    if sb:
        print(f"│ shadowban check ({sb['executed_at']}): {sb['result']}")

    conn.close()

    # Agent-log lines for this user (last 5 firing/exit events)
    if AGENT_LOG:
        try:
            text = AGENT_LOG.read_text(errors="replace")
            lines = [l for l in text.splitlines() if u in l]
            tail = lines[-5:]
            if tail:
                print(f"│ recent agent-log lines:")
                for l in tail:
                    print(f"│   {l[:140]}")
        except Exception:
            pass

    print(f"└────────────────────────────────────────")
