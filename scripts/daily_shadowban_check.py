"""For each active (non-paused) warmup account, run a cross-account shadow-ban
check. Writes a StateSnapshot row with action_type='shadowban_check' to the
target's SQLite for each result. Auto-touches banned.json + pause if banned.

Resilience:
- Tries multiple viewer profiles if first one fails.
- On MLX LOCK_PROFILE_ERROR, auto-unlocks via DELETE /bpds/profile/lock and retries.
- Catches per-target exceptions and continues with remaining accounts.
- Skips already-banned accounts as targets (no point re-checking).
"""
import json
import os
import sqlite3
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, "/root/skills/user/working-with-multilogin/scripts")

# Load workspace .env + any account .env
env_paths = [REPO / ".env"]
acct_root = REPO / "accounts"
if acct_root.exists():
    for d in acct_root.iterdir():
        if d.is_dir() and not d.name.startswith(".") and (d / ".env").exists():
            env_paths.append(d / ".env")
            break
for envp in env_paths:
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from mlx_client import Client
from lib.shadowban_check import check

FOLDER = "33b31a69-2819-43c6-811a-2bebf5c09999"

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()


def force_unlock(profile_id):
    """Clear a stuck MLX cloud-side profile lock. Returns True on HTTP 200."""
    try:
        r = requests.delete(
            "https://api.multilogin.com/bpds/profile/lock",
            headers=c._auth(),
            json={"profile_id": profile_id, "folder_id": FOLDER},
            timeout=15,
        )
        return r.status_code == 200
    except Exception:
        return False


def check_with_fallback(target, viewers):
    """Try check() against `target` using each viewer until one works.
    On LOCK_PROFILE_ERROR, auto-unlock and retry once with same viewer first.

    Callers should pre-filter `viewers` to exclude banned + paused accounts —
    those profiles tend to be stopped/uninstalled in MLX, so trying them just
    produces `viewer_unavailable` noise. Defense-in-depth: we also skip them
    here in case a caller forgets.
    """
    last_err = None
    for viewer in viewers:
        if viewer["username"] == target["username"]:
            continue
        if viewer.get("banned") or viewer.get("paused"):
            continue
        for attempt in range(2):
            try:
                print(f"  → trying viewer={viewer['username']} (attempt {attempt+1})")
                result = check(target["username"], viewer["pid"], c)
                # check() returns dict with 'status' even on internal errors,
                # so detect failure-from-MLX shape and treat as exception
                err = (result.get("raw") or {}).get("error", "")
                if "LOCK_PROFILE_ERROR" in err or "can't lock profile" in err:
                    raise RuntimeError(err)
                return result, viewer
            except Exception as e:
                msg = str(e)
                last_err = (viewer["username"], msg[:200])
                if "LOCK_PROFILE_ERROR" in msg or "can't lock profile" in msg:
                    print(f"     LOCK_PROFILE_ERROR — auto-unlocking {viewer['pid'][:8]}...")
                    if force_unlock(viewer["pid"]):
                        time.sleep(2)
                        continue  # retry same viewer
                # Not a lock error or unlock failed — move to next viewer
                break
    return {"status": "viewer_unavailable",
            "raw": {"error": f"all viewers failed; last: {last_err}"}}, None


accounts = []
for d in (REPO / "accounts").iterdir():
    if not d.is_dir() or d.name.startswith("."):
        continue
    cfg_path = d / "config.json"
    if not cfg_path.exists():
        continue
    cfg = json.loads(cfg_path.read_text())
    paused = (d / "pause").exists()
    banned = (d / "banned.json").exists()
    accounts.append({
        "username": d.name,
        "pid": cfg["multilogin"]["profile_id"],
        "state_db": d / "state.db",
        "dir": d,
        "paused": paused,
        "banned": banned,
    })

active = [a for a in accounts if not a["paused"] and not a["banned"]]
# Viewers must be reachable MLX profiles: banned/paused accounts often have
# their profiles stopped or uninstalled, so trying to look at /u/<target> from
# them yields `viewer_unavailable` rather than a real shadowban signal. This
# was the root cause of the 2026-06-03 false viewer_unavailable runs against
# salty_crow33 — crispygopher_9 (banned) was being attempted as a viewer.
viewers = active
print(f"checking {len(active)} active accounts "
      f"(skipping {sum(1 for a in accounts if a['paused'])} paused, "
      f"{sum(1 for a in accounts if a['banned'])} already-banned); "
      f"viewer pool: {len(viewers)}")

for target in accounts:
    if target["paused"]:
        print(f"skip {target['username']} (paused)")
        continue
    if target["banned"]:
        print(f"skip {target['username']} (already banned)")
        continue

    print(f"checking {target['username']}...")
    try:
        result, used_viewer = check_with_fallback(target, viewers)
    except Exception as e:
        print(f"  ✗ {target['username']}: uncaught error: {type(e).__name__}: {e}")
        continue

    print(f"  → {result['status']}  raw={result['raw']}  viewer={used_viewer['username'] if used_viewer else 'none'}")

    if target["state_db"].exists():
        try:
            conn = sqlite3.connect(str(target["state_db"]))
            conn.execute(
                """INSERT INTO actions_log (id, type, status, action_type, executed_at, result, reasoning)
                   VALUES (?, 'StateSnapshot', 'done', 'shadowban_check', ?, ?, ?)""",
                (uuid.uuid4().hex, datetime.now(timezone.utc).isoformat(),
                 result["status"], json.dumps(result["raw"])),
            )
            conn.commit(); conn.close()
        except Exception as e:
            print(f"  ! sqlite write failed: {e}")

    if result["status"] in ("shadowbanned", "suspended"):
        bp = target["dir"] / "banned.json"
        if not bp.exists():
            if result["status"] == "shadowbanned":
                evidence = f"daily_shadowban_check viewer={used_viewer['username'] if used_viewer else '?'} got HTTP 404 on /u/{target['username']}/about.json"
            else:
                evidence = f"daily_shadowban_check viewer={used_viewer['username'] if used_viewer else '?'} got is_suspended=true on /u/{target['username']}/about.json"
            bp.write_text(json.dumps({
                "status": result["status"],
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
                "suspected_since_day": None,
                "evidence": [evidence],
                "appeal_status": "pending",
            }, indent=2))
            (target["dir"] / "pause").touch()
            print(f"  ⚠ NEW {result['status'].upper()} — wrote banned.json + paused {target['username']}")
