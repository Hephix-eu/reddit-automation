"""Submit a Reddit comment via OAuth API using the warmed Multilogin session.
No DOM editor, no reCAPTCHA — same path third-party Reddit clients use.

Two modes:

  draft (review):
      python3 oauth_comment.py --user smug_pickle72 \
          --thread https://www.reddit.com/r/dotnet/comments/<id>/<slug>/ \
          --text "Comment body here..."
      → opens MLX, fetches thread context, saves Draft row to SQLite,
        prints OP + draft for human eyeball, exits WITHOUT submitting.

  submit (after eyeball):
      python3 oauth_comment.py --user smug_pickle72 --draft-id <id> --submit
      → reads the saved draft, POSTs via oauth.reddit.com/api/comment,
        verifies the comment is visible, updates DB rows.

Always stops the Multilogin profile cleanly.
"""
import argparse
import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def thing_id_from_thread(url: str) -> str:
    """Extract t3_<id> from a reddit thread URL like /r/sub/comments/<id>/slug/."""
    m = re.search(r"/comments/([a-z0-9]+)/", url)
    if not m:
        raise ValueError(f"can't parse thread id from {url!r}")
    return f"t3_{m.group(1)}"


def db_insert(state_db: Path, **fields) -> str:
    """Insert into actions_log; returns the new row's id."""
    import sqlite3
    row_id = uuid.uuid4().hex
    cols = ["id", "type", "status"] + [k for k in fields if k not in ("id", "type", "status")]
    vals = [row_id, fields["type"], fields["status"]] + [fields.get(k) for k in cols[3:]]
    placeholders = ",".join("?" for _ in cols)
    conn = sqlite3.connect(str(state_db))
    conn.execute(f"INSERT INTO actions_log ({','.join(cols)}) VALUES ({placeholders})", vals)
    conn.commit()
    conn.close()
    return row_id


def db_get_draft(state_db: Path, draft_id_prefix: str) -> dict:
    import sqlite3
    conn = sqlite3.connect(str(state_db))
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, target_url, draft_content, subreddit, day FROM actions_log "
        "WHERE type='Draft' AND id LIKE ?",
        (draft_id_prefix + "%",),
    ).fetchone()
    conn.close()
    if not row:
        raise KeyError(f"no Draft row with id prefix {draft_id_prefix!r}")
    return dict(row)


def db_update_status(state_db: Path, row_id: str, status: str, **extra) -> None:
    import sqlite3
    conn = sqlite3.connect(str(state_db))
    sets = ["status = ?"] + [f"{k} = ?" for k in extra]
    vals = [status] + list(extra.values()) + [row_id]
    conn.execute(f"UPDATE actions_log SET {','.join(sets)} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def current_day(account_dir: Path) -> int | None:
    from datetime import date
    cfg = account_dir / "config.json"
    if not cfg.exists():
        return None
    c = json.loads(cfg.read_text())
    sd = (c.get("plan") or {}).get("start_date")
    if not sd:
        return None
    try:
        return (date.today() - date.fromisoformat(sd)).days + 1
    except Exception:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", required=True)
    ap.add_argument("--thread", help="full reddit thread URL")
    ap.add_argument("--text", help="comment body (markdown allowed)")
    ap.add_argument("--draft-id", help="pre-saved draft id prefix to submit")
    ap.add_argument("--submit", action="store_true", help="actually POST to /api/comment")
    args = ap.parse_args()

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.user / ".env")

    account_dir = REPO / "accounts" / args.user
    state_db = account_dir / "state.db"
    config = json.loads((account_dir / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]

    # Resolve mode: either we have draft-id (read from DB) or we have thread+text (write draft)
    draft_id = None
    if args.draft_id:
        draft = db_get_draft(state_db, args.draft_id)
        thread_url = draft["target_url"]
        text = draft["draft_content"]
        subreddit = draft["subreddit"]
        draft_id = draft["id"]
        print(f"[draft] loaded {draft_id[:8]}... from DB")
    else:
        if not args.thread or not args.text:
            sys.exit("either --draft-id, or both --thread and --text are required")
        thread_url = args.thread.strip()
        text = args.text.strip()
        m = re.search(r"/r/([^/]+)/", thread_url)
        subreddit = f"r/{m.group(1)}" if m else None

    thing_id = thing_id_from_thread(thread_url)
    day = current_day(account_dir)

    # Save the draft FIRST so even if MLX dies we have a record
    if not draft_id:
        draft_id = db_insert(
            state_db,
            type="Draft", status="pending_review",
            action_type="comment",
            subreddit=subreddit, target_url=thread_url,
            draft_content=text,
            day=day,
        )
        print(f"\n[saved draft] id={draft_id[:8]}...")

    print()
    print(f"=" * 64)
    print(f"DRAFT for {args.user} (Day {day}) — thing_id={thing_id}")
    print(f"=" * 64)
    print(f"thread:    {thread_url}")
    print(f"subreddit: {subreddit}")
    print(f"draft_id:  {draft_id}  (use --draft-id {draft_id[:12]} --submit to send)")
    print()
    print(f"--- comment body ---")
    print(text)
    print(f"--------------------")
    print()

    # Always fetch thread context for sanity (regardless of submit/not)
    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[mlx] profile started on port {port}")
    time.sleep(6)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[0]

            # Prime cookies + verify auth
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(3)
            me_resp = page.request.get("https://oauth.reddit.com/api/v1/me", timeout=15000)
            print(f"[auth] /api/v1/me HTTP {me_resp.status}")
            if me_resp.status != 200:
                print("[fail] auth failed — comment cannot be submitted")
                if args.submit:
                    db_update_status(state_db, draft_id, "failed",
                                     result=f"auth pre-check HTTP {me_resp.status}")
                return 2
            me = me_resp.json()
            who = me.get("name") or me.get("data", {}).get("name")
            print(f"[auth] logged in as {who!r}")

            # Fetch the thread JSON so we show the OP context (even in draft mode)
            json_url = thread_url.rstrip("/") + ".json"
            print(f"\n[thread] fetching {json_url}")
            tresp = page.request.get(json_url, timeout=15000)
            if tresp.status == 200:
                try:
                    tdata = tresp.json()
                    op = tdata[0]["data"]["children"][0]["data"]
                    print(f"  OP TITLE: {op.get('title','')[:100]}")
                    body = (op.get("selftext") or "")[:400]
                    if body:
                        print(f"  OP BODY:  {body}")
                    print(f"  score={op.get('score')} comments={op.get('num_comments')}")
                except Exception as e:
                    print(f"  parse err: {e}")
            else:
                print(f"  HTTP {tresp.status}")

            if not args.submit:
                print()
                print("[done] draft saved. eyeball above; if good, run:")
                print(f"  python3 scripts/oauth_comment.py --user {args.user} --draft-id {draft_id[:12]} --submit")
                return 0

            # ===== Extract auth artifacts from session =====
            ctx_cookies = browser.contexts[0].cookies()
            token_v2 = next((c["value"] for c in ctx_cookies if c["name"] == "token_v2"), None)
            csrf_token = next((c["value"] for c in ctx_cookies if c["name"] == "csrf_token"), None)
            real_ua = page.evaluate("() => navigator.userAgent")
            print(f"\n[auth] token_v2: {(token_v2 or '')[:40]}...  ({len(token_v2 or '')} chars)")
            print(f"[auth] csrf:     {(csrf_token or '')[:40]}")
            print(f"[auth] UA:       {real_ua[:80]}")

            # ===== SUBMIT =====
            print(f"\n=== SUBMITTING via oauth.reddit.com/api/comment ===")
            submit_headers = {
                "User-Agent": real_ua,
                "Accept": "application/json",
            }
            if token_v2:
                submit_headers["Authorization"] = f"Bearer {token_v2}"
            sub_resp = page.request.post(
                "https://oauth.reddit.com/api/comment",
                form={"api_type": "json", "thing_id": thing_id, "text": text},
                headers=submit_headers,
                timeout=20000,
            )
            print(f"  HTTP {sub_resp.status}")
            sub_body = sub_resp.text()
            print(f"  body: {sub_body[:600]}")

            # Did the request even reach the API? HTML body = CDN block, JSON = API saw it.
            looks_like_json = sub_body.lstrip().startswith("{")
            if sub_resp.status >= 400 and not looks_like_json:
                print(f"\n[fail] CDN/proxy blocked the POST (returned HTML, not API JSON)")
                db_update_status(state_db, draft_id, "pending_review",
                                 result=f"submit blocked: HTTP {sub_resp.status} (HTML body)")
                return 4

            comment_url = None
            comment_id = None
            try:
                sd = sub_resp.json()
                errors = sd.get("json", {}).get("errors", [])
                if errors:
                    print(f"  [fail] errors: {errors}")
                    db_update_status(state_db, draft_id, "failed",
                                     result=f"reddit errors: {errors}")
                    return 3
                things = sd.get("json", {}).get("data", {}).get("things", [])
                if things:
                    cd = things[0].get("data", {})
                    comment_id = cd.get("name") or cd.get("id")
                    comment_url = "https://www.reddit.com" + (cd.get("permalink", ""))
                    print(f"  ✅ comment_id={comment_id}")
                    print(f"  ✅ permalink={comment_url}")
            except Exception as e:
                print(f"  response parse err: {e}")

            # Verify by re-fetching the thread and looking for the comment
            time.sleep(3)
            verify_status = "unverified"
            if comment_id:
                vresp = page.request.get(json_url, timeout=15000)
                if vresp.status == 200:
                    try:
                        vd = vresp.json()
                        def walk(items):
                            for it in items:
                                d = it.get("data", {})
                                if d.get("name") == comment_id or d.get("id") == comment_id.split("_")[-1]:
                                    return True
                                replies = d.get("replies")
                                if isinstance(replies, dict) and walk(replies.get("data", {}).get("children", [])):
                                    return True
                            return False
                        if walk(vd[1]["data"]["children"]):
                            verify_status = "confirmed"
                            print(f"  ✅ verified — comment visible in thread JSON")
                        else:
                            verify_status = "shadow_rejected"
                            print(f"  ⚠ comment_id not found in thread after 3s — shadow_rejected")
                    except Exception as e:
                        print(f"  verify parse err: {e}")

            # Update Draft → submitted, insert Action row
            db_update_status(state_db, draft_id, "submitted",
                             submitted_content=text,
                             result=comment_url or "submitted but no permalink returned")
            action_id = db_insert(
                state_db,
                type="Action", status=("done" if verify_status == "confirmed" else "shadow_rejected"),
                action_type="comment",
                day=day, subreddit=subreddit,
                target_url=comment_url or thread_url,
                submitted_content=text,
                reasoning=f"oauth_api submit (verify={verify_status})",
                session_id=draft_id,  # tie the Action to its originating draft
            )
            print(f"\n[db] Draft → submitted; Action {action_id[:8]}... → {verify_status}")

    finally:
        try:
            c.stop(pid)
            print("\n[mlx] profile stopped")
        except Exception as e:
            print(f"[mlx] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
