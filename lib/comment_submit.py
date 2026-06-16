"""Chokepoint for ALL Reddit comment submission from the warmup agent.

Everything that wants to post a comment - the autonomous warmup session,
the manual `scripts/test_comment_submit.py`, anything else we add later -
MUST go through `submit_comment` here. That gives us a single place to:

  1. Run mechanical quality checks (lib.comment_quality.check_all) -
     dedupe + blacklist. If they reject, we log a skipped row and bail
     without touching Reddit.
  2. Submit via the real UI composer (mouse + human-paced keystrokes,
     lib.browse) - open the composer, type into the Lexical editor, click
     Comment. No API POST.
  3. Re-fetch the thread JSON and verify the comment is visible (i.e. not
     shadow-rejected).
  4. Write the Action row to state.db with the right status.

Background (2026-06-02 ban cluster): the autonomous agent submitted
duplicate comments (crispygopher_9) and off-topic generic filler
(steepsalmon_13) despite AGENT_PROMPT.md telling it not to. Prompt-only
rules are not enough; a mechanical floor was needed. That floor is the
quality gate below - it stays even though the submit mechanism changed.

Auth model: the caller already has a live Multilogin browser session
(Playwright `page` connected via CDP, logged into Reddit). We act through
that real session by driving the comment composer in the page - no tokens,
no separate API call. (The old oauth.reddit.com POST used the `token_v2`
web-session cookie as an OAuth bearer, which is invalid, so it never
actually posted - replaced 2026-06-15 by UI submission, validated live.)
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

from lib import browse, comment_quality, db  # noqa: E402


# ---- UI composer mechanics (validated 2026-06-15 on live r/AskReddit) ------
#
# Reddit's comment composer is a Lexical editor inside <reddit-rte>'s shadow
# root. The collapsed trigger (<faceplate-textarea-input data-testid=
# "trigger-button">) HOST measures 0x0 - its visible bar is a shadow child - so
# we walk the shadow DOM for the element with a real rect and REAL-mouse-click
# its center (isTrusted) to fully expand the composer (a JS .click() opens the
# DOM but leaves the editor invisible). We only ever type into the visible
# contenteditable, never a <textarea> (several are honeypots holding a
# CSRF-like token that flags bots on submit).

_TRIGGER_JS = """() => {
  let best = null;
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      const tid = el.getAttribute && el.getAttribute('data-testid');
      const ph = el.getAttribute && el.getAttribute('placeholder');
      if (tid === 'trigger-button' || ph === 'Join the conversation' || ph === 'Add a comment') {
        const b = el.getBoundingClientRect();
        if (b.width > 50 && b.height > 8 && (!best || b.width*b.height > best._area)) {
          best = el; best._area = b.width*b.height;
        }
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);
  if (!best) return null;
  best.scrollIntoView({block: 'center'});
  const b = best.getBoundingClientRect();
  return {cx: Math.round(b.x + b.width/2), cy: Math.round(b.y + b.height/2)};
}"""


def _bbox(page, locator):
    """Visible box for a locator, with a JS scrollIntoView+rect fallback for
    collapsed custom elements Playwright won't measure. None if 0-size."""
    try:
        b = locator.bounding_box(timeout=2500)
        if b and b["width"] > 0 and b["height"] > 0:
            return b
    except Exception:
        pass
    try:
        r = locator.evaluate("""el => { el.scrollIntoView({block:'center'});
            const b = el.getBoundingClientRect();
            return {x:b.x, y:b.y, width:b.width, height:b.height}; }""")
        if r and r["width"] > 0 and r["height"] > 0:
            return r
    except Exception:
        pass
    return None


def _find_editor(page):
    """(locator, box) for the VISIBLE Lexical contenteditable inside
    shreddit-composer, never a honeypot <textarea>. (None, None) until visible."""
    for sel in ('shreddit-composer [contenteditable="true"]',
                'div[contenteditable="true"][data-lexical-editor="true"]',
                '[contenteditable="true"]'):
        loc = page.locator(sel)
        for i in range(loc.count()):
            cand = loc.nth(i)
            b = _bbox(page, cand)
            if b and b["width"] > 60 and b["height"] > 10:
                return cand, b
    return None, None


def _open_composer(page):
    """Real-click the visible trigger bar so the composer fully expands (a JS
    click opens the DOM but leaves the editor invisible), then poll for a
    visible editor. Returns (editor_locator, box) or (None, None)."""
    hit = None
    for _ in range(12):  # the trigger bar can render a little after nav
        hit = page.evaluate(_TRIGGER_JS)
        if hit:
            break
        time.sleep(0.6)
    if not hit:
        return None, None
    time.sleep(0.3)
    page.mouse.click(hit["cx"], hit["cy"])
    for _ in range(16):
        ed, eb = _find_editor(page)
        if ed is not None:
            return ed, eb
        time.sleep(0.5)
    return None, None


def _click_submit(page) -> bool:
    """Click the composer's Comment button with a real mouse click."""
    candidates = [
        page.get_by_role("button", name=re.compile(r"^comment$", re.I)),
        page.locator('shreddit-composer button[type="submit"]'),
    ]
    for loc in candidates:
        for i in range(loc.count()):
            b = loc.nth(i)
            try:
                if b.is_visible(timeout=1500) and b.is_enabled():
                    box = b.bounding_box(timeout=2000)
                    if box:
                        page.mouse.click(box["x"] + box["width"] / 2,
                                         box["y"] + box["height"] / 2)
                    else:
                        b.click(timeout=4000)
                    return True
            except Exception:
                continue
    return False


def _ui_submit_and_verify(
    page,
    thread_url: str,
    text: str,
) -> Tuple[bool, Optional[str], Optional[str], str]:
    """Submit the comment through the real UI composer (mouse + browse.human_type)
    and verify it is visible by re-fetching the thread JSON.

    Returns (ok, comment_id, comment_url, verify_status). ok=True means the
    Comment button was clicked AFTER the text landed in the editor; it does NOT
    guarantee visibility (that's verify_status). verify_status is one of:
    "confirmed", "shadow_rejected", "unverified", "open_failed", "not_landed",
    "submit_failed". The first three mirror the old API path so submit_comment's
    logging stays unchanged.
    """
    json_url = thread_url.rstrip("/") + ".json"

    # Make sure we're on the thread page (composer present).
    if "/comments/" not in (page.url or ""):
        page.goto(thread_url, wait_until="domcontentloaded", timeout=40000)
        time.sleep(4)
    # The cookie-preferences modal re-appears per page and overlays the composer.
    for _ in range(3):
        if not browse.dismiss_cookie_popup(page):
            break
        time.sleep(1)
    # A short read-scroll lets the comments section + composer lazy-render.
    try:
        browse.human_scroll(page, duration_s=3)
    except Exception:
        pass

    editor, ebox = _open_composer(page)
    if editor is None:
        return False, None, None, "open_failed"

    # Focus the editor (real click at its center) + type with human cadence.
    page.mouse.click(ebox["x"] + ebox["width"] / 2, ebox["y"] + ebox["height"] / 2)
    time.sleep(0.4)
    browse.human_type(page, text)
    time.sleep(0.5)

    landed = page.evaluate(
        """() => { const el =
          document.querySelector('shreddit-composer [contenteditable="true"]')
          || document.querySelector('div[contenteditable="true"][data-lexical-editor="true"]')
          || document.querySelector('[contenteditable="true"]');
          return el ? (el.innerText || el.textContent || '').trim() : ''; }""")
    if text[:25] not in (landed or ""):
        return False, None, None, "not_landed"

    if not _click_submit(page):
        return False, None, None, "submit_failed"

    # Verify by re-fetching the thread JSON; retry for CDN lag. The UI path
    # returns no comment id, so we match our own text by a substring.
    needle = text[:40]
    time.sleep(3)
    fetched_ok = False
    comment_url = None
    for _ in range(5):
        try:
            vresp = page.request.get(json_url, timeout=15000)
            if vresp.status == 200:
                fetched_ok = True
                vd = vresp.json()

                def walk(items):
                    for it in items:
                        d = it.get("data", {})
                        if needle and needle in (d.get("body") or ""):
                            return d
                        replies = d.get("replies")
                        if isinstance(replies, dict):
                            r = walk(replies.get("data", {}).get("children", []))
                            if r:
                                return r
                    return None

                hit = walk(vd[1]["data"]["children"])
                if hit:
                    perm = hit.get("permalink", "")
                    comment_url = "https://www.reddit.com" + perm if perm else None
                    return True, hit.get("name"), comment_url, "confirmed"
        except Exception:
            pass
        time.sleep(7)

    return True, None, comment_url, ("shadow_rejected" if fetched_ok else "unverified")


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
      2. Otherwise, submit via _ui_submit_and_verify(page, ...) (real UI
         composer, lib.browse).
         - On verify_status="confirmed": insert Action status='done',
           return True.
         - On verify_status="shadow_rejected": insert Action
           status='shadow_rejected', return False.
         - On any failure path: insert Action status='failed' with the
           verify_status as reasoning, return False.

    The caller (autonomous agent or scripts/test_comment_submit.py) must have
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

    # ---- 2. UI submit + verify -----------------------------------------
    ok, comment_id, comment_url, verify_status = _ui_submit_and_verify(
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
            reasoning=f"ui submit failed: {verify_status}",
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
            reasoning=f"ui submit (verify={verify_status})",
            session_id=session_id,
            name=account,
            result=comment_url,
        )
        return True

    # Shadow-rejected (submitted but not visible to re-fetch) OR unverified
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
        reasoning=f"ui submit (verify={verify_status})",
        session_id=session_id,
        name=account,
        result=comment_url,
    )
    return False
