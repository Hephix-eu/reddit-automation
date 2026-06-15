"""Validate the production chokepoint lib.comment_submit.submit_comment end to
end: opens a live MLX session, then routes a real comment through the SAME
function the warmup agent uses (quality gate -> UI submit -> verify -> DB log).

Usage:
  python3 scripts/test_comment_submit.py --user salty_crow33 \
      --thread <url> --text "..."
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import uuid
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break
REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import comment_submit  # noqa: E402


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--thread", required=True)
    ap.add_argument("--text", required=True)
    args = ap.parse_args()

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.user / ".env")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    account_dir = REPO / "accounts" / args.user
    state_db = account_dir / "state.db"
    config = json.loads((account_dir / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]
    m = __import__("re").search(r"/r/([^/]+)/", args.thread)
    subreddit = f"r/{m.group(1)}" if m else None
    sid = "test-" + uuid.uuid4().hex[:8]

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[setup] {args.user} started on port {port}; sid={sid}")
    time.sleep(8)

    ok = False
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[0]
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=40000)
            time.sleep(3)
            me = page.request.get("https://oauth.reddit.com/api/v1/me", timeout=15000)
            print(f"[auth] /me HTTP {me.status}")
            if me.status != 200:
                print("[fail] not logged in; aborting")
                return 2

            print("\n=== calling comment_submit.submit_comment (the chokepoint) ===")
            ok = comment_submit.submit_comment(
                args.text,
                args.thread,
                account=args.user,
                state_db=state_db,
                day=None,
                session_id=sid,
                subreddit=subreddit,
                page=page,
            )
            print(f"  submit_comment returned: {ok}")
    finally:
        try:
            c.stop(pid)
            print("[cleanup] profile stopped")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    # Inspect the Action row the chokepoint wrote.
    print("\n=== DB rows for this session ===")
    try:
        conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        for r in conn.execute(
            "SELECT type, action_type, status, reasoning, result, target_url "
            "FROM actions_log WHERE session_id=? ORDER BY executed_at", (sid,)):
            print(f"  [{r['type']}/{r['action_type']}] status={r['status']} "
                  f"reasoning={r['reasoning']!r} result={r['result']!r}")
        conn.close()
    except Exception as e:
        print(f"  db read err: {e}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
