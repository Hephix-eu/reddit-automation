"""Chokepoint for ALL Reddit comment submission from the warmup agent.

Everything that wants to post a comment - the autonomous warmup session, the
manual `scripts/oauth_comment.py --submit`, anything else we add later -
MUST go through `submit_comment` here. That gives us a single place to:

  1. Run mechanical quality checks (lib.comment_quality.check_all) -
     dedupe + blacklist. If they reject, we log a skipped row and bail
     without touching Reddit.
  2. POST to oauth.reddit.com/api/comment with the right auth + UA.
  3. Re-fetch the thread JSON and verify the comment is visible (i.e. not
     shadow-rejected).
  4. Write the Action row to state.db with the right status.

Background (2026-06-02 ban cluster): the autonomous agent submitted
duplicate comments (crispygopher_9) and off-topic generic filler
(steepsalmon_13) despite AGENT_PROMPT.md telling it not to. Prompt-only
rules are not enough; a mechanical floor was needed.

Auth model: the caller already has a live Multilogin browser session
(Playwright `page` connected via CDP, with cookies set by being logged
into Reddit). We accept that `page` object and extract `token_v2` +
`navigator.userAgent` from it. No separate cookie/header plumbing.
"""
from __future__ import annotations

import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Optional, Tuple

# Make sibling modules importable when this file is run as part of `lib.*`.
_REPO = Path(__file__).resolve().parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from lib import comment_quality, db  # noqa: E402


# ---- URL / thing-id parsing ------------------------------------------------

_THREAD_RE = re.compile(r"/comments/([a-z0-9]+)/")


def _thing_id_from_thread(url: str) -> str:
    """Extract t3_<id> from a reddit thread URL like /r/sub/comments/<id>/slug/."""
    m = _THREAD_RE.search(url)
    if not m:
        raise ValueError(f"can't parse thread id from {url!r}")
    return f"t3_{m.group(1)}"


# ---- OAuth POST + verify (extracted from scripts/oauth_comment.py) ---------

def _oauth_post_and_verify(
    page,
    thread_url: str,
    text: str,
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """POST the comment via oauth.reddit.com and verify it's visible.

    Returns (ok, comment_id, comment_url, verify_status), where:
      - ok=True means the POST returned 2xx + parseable JSON with a `thing`.
        It does NOT mean the comment is visible to other users.
      - verify_status is one of: "confirmed", "shadow_rejected", "unverified",
        "post_failed", "cdn_blocked", "reddit_errors".

    This is the chunk that previously lived inline in
    `scripts/oauth_comment.py`. It is the only place in the codebase that
    talks to `oauth.reddit.com/api/comment`.
    """
    thing_id = _thing_id_from_thread(thread_url)
    json_url = thread_url.rstrip("/") + ".json"

    ctx_cookies = page.context.cookies()
    token_v2 = next((c["value"] for c in ctx_cookies if c["name"] == "token_v2"), None)
    real_ua = page.evaluate("() => navigator.userAgent")

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
    sub_body = sub_resp.text()

    looks_like_json = sub_body.lstrip().startswith("{")
    if sub_resp.status >= 400 and not looks_like_json:
        return False, None, None, "cdn_blocked"

    try:
        sd = sub_resp.json()
    except Exception:
        return False, None, None, "post_failed"

    errors = sd.get("json", {}).get("errors", [])
    if errors:
        return False, None, None, "reddit_errors"

    things = sd.get("json", {}).get("data", {}).get("things", [])
    if not things:
        return False, None, None, "post_failed"

    cd = things[0].get("data", {}) or {}
    comment_id = cd.get("name") or cd.get("id")
    permalink = cd.get("permalink", "")
    comment_url = "https://www.reddit.com" + permalink if permalink else None

    # Verify by re-fetching the thread JSON and looking for our comment_id.
    time.sleep(3)
    verify_status = "unverified"
    if comment_id:
        try:
            vresp = page.request.get(json_url, timeout=15000)
            if vresp.status == 200:
                vd = vresp.json()

                def walk(items):
                    for it in items:
                        d = it.get("data", {})
                        if (d.get("name") == comment_id
                                or d.get("id") == comment_id.split("_")[-1]):
                            return True
                        replies = d.get("replies")
                        if isinstance(replies, dict):
                            children = replies.get("data", {}).get("children", [])
                            if walk(children):
                                return True
                    return False

                if walk(vd[1]["data"]["children"]):
                    verify_status = "confirmed"
                else:
                    verify_status = "shadow_rejected"
        except Exception:
            verify_status = "unverified"

    return True, comment_id, comment_url, verify_status


# ---- Public chokepoint -----------------------------------------------------

def submit_comment(
    text: str,
    thread_url: str,
    *,
    account: str,
    state_db: Path,
    day: Optional[int],
    session_id: str,
    subreddit: Optional[str],
    page,
) -> bool:
    """Validate, submit, verify, log. Returns True iff the comment was
    submitted AND verified visible to a re-fetch.

    Behavior:
      1. Run comment_quality.check_all(text, state_db). If reject, insert
         a Type=Action action_type=skipped row with
         reasoning='quality:<reason>' and return False. Do NOT touch
         Reddit.
      2. Otherwise, POST via _oauth_post_and_verify(page, ...).
         - On verify_status="confirmed": insert Action status='done',
           return True.
         - On verify_status="shadow_rejected": insert Action
           status='shadow_rejected', return False.
         - On any failure path: insert Action status='failed' with the
           verify_status as reasoning, return False.

    The caller (autonomous agent or oauth_comment.py --submit) must have
    a live Playwright `page` already logged into reddit.com via the
    Multilogin browser. We don't open or close that session.
    """
    state_db = Path(state_db)

    # ---- 1. Mechanical quality floor -----------------------------------
    reject = comment_quality.check_all(text, state_db)
    if reject:
        db.insert(
            state_db,
            type="Action",
            status="done",
            action_type="skipped",
            day=day,
            subreddit=subreddit,
            target_url=thread_url,
            submitted_content=text,
            reasoning=f"quality:{reject}",
            session_id=session_id,
            name=account,
        )
        return False

    # ---- 2. OAuth POST + verify ----------------------------------------
    ok, comment_id, comment_url, verify_status = _oauth_post_and_verify(
        page, thread_url, text,
    )

    if not ok:
        db.insert(
            state_db,
            type="Action",
            status="failed",
            action_type="comment",
            day=day,
            subreddit=subreddit,
            target_url=thread_url,
            submitted_content=text,
            reasoning=f"oauth_api submit failed: {verify_status}",
            session_id=session_id,
            name=account,
        )
        return False

    if verify_status == "confirmed":
        db.insert(
            state_db,
            type="Action",
            status="done",
            action_type="comment",
            day=day,
            subreddit=subreddit,
            target_url=comment_url or thread_url,
            submitted_content=text,
            reasoning=f"oauth_api submit (verify={verify_status})",
            session_id=session_id,
            name=account,
            result=comment_url,
        )
        return True

    # Shadow-rejected (POSTed 2xx but not visible to re-fetch) OR unverified
    # (couldn't re-fetch). Treat both as not-visible-yet.
    db.insert(
        state_db,
        type="Action",
        status="shadow_rejected" if verify_status == "shadow_rejected" else "failed",
        action_type="comment",
        day=day,
        subreddit=subreddit,
        target_url=comment_url or thread_url,
        submitted_content=text,
        reasoning=f"oauth_api submit (verify={verify_status})",
        session_id=session_id,
        name=account,
        result=comment_url,
    )
    return False
