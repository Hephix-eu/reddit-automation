"""Phase-0 spike: drive Reddit's REAL comment composer via UI (mouse + human_type)
on a live account, optionally submit, and verify the comment persisted.

Two modes:
  dry-run (default): open a r/AskReddit thread, open the composer, locate the
      real Lexical editor (NOT the honeypot textarea), type via browse.human_type,
      screenshot, then ESC/cancel. NEVER submits.
  --submit --text "...": same, but clicks the Comment button and then verifies
      the comment is visible (re-fetch thread .json + the account's
      /user/<name>/comments.json, CDN-lag tolerant).

Usage (inside redditagent-image, sharing netns with multilogin):
  python3 scripts/spike_reddit_comment.py --user swift_viper14
  python3 scripts/spike_reddit_comment.py --user swift_viper14 --thread <url> --text "..." --submit
"""
import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import browse  # noqa: E402


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def _bbox(page, locator):
    """Visible bounding box for a locator, with a JS scrollIntoView+rect fallback
    for collapsed custom elements Playwright won't measure. None if 0-size."""
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


def find_editor(page):
    """Return (locator, box) for the REAL VISIBLE Lexical contenteditable inside
    shreddit-composer, never a honeypot <textarea>. None if not visible yet."""
    for sel in ['shreddit-composer [contenteditable="true"]',
                'div[contenteditable="true"][data-lexical-editor="true"]',
                '[contenteditable="true"]']:
        loc = page.locator(sel)
        for i in range(loc.count()):
            cand = loc.nth(i)
            b = _bbox(page, cand)
            if b and b["width"] > 60 and b["height"] > 10:
                return cand, b
    return None, None


_TRIGGER_JS = """() => {
  // Walk light + open shadow DOM for the VISIBLE composer trigger bar. The
  // faceplate-textarea-input host measures 0; its bordered bar is a shadow child.
  const cands = [];
  let best = null;
  const walk = (root) => {
    root.querySelectorAll('*').forEach(el => {
      const tid = el.getAttribute && el.getAttribute('data-testid');
      const ph = el.getAttribute && el.getAttribute('placeholder');
      if (tid === 'trigger-button' || ph === 'Join the conversation' || ph === 'Add a comment') {
        const b = el.getBoundingClientRect();
        cands.push({tag: el.tagName.toLowerCase(), tid: tid||'', w: Math.round(b.width), h: Math.round(b.height)});
        if (b.width > 50 && b.height > 8 && (!best || b.width*b.height > best._area)) {
          best = el; best._area = b.width*b.height;
        }
      }
      if (el.shadowRoot) walk(el.shadowRoot);
    });
  };
  walk(document);
  if (!best) return {cands, hit: null};
  best.scrollIntoView({block: 'center'});
  const b = best.getBoundingClientRect();
  return {cands, hit: {tag: best.tagName.toLowerCase(),
    cx: Math.round(b.x + b.width/2), cy: Math.round(b.y + b.height/2),
    w: Math.round(b.width), h: Math.round(b.height)}};
}"""


def open_composer(page):
    """Find the VISIBLE trigger bar (walking shadow DOM), real-mouse-click its
    center so the composer fully expands (isTrusted — a JS click opens the DOM
    but leaves the editor invisible), then poll for a visible editor."""
    info = page.evaluate(_TRIGGER_JS)
    print(f"  trigger candidates: {info.get('cands')}")
    hit = info.get("hit")
    if not hit:
        print("  no visible trigger bar found")
        return None, None
    print(f"  real mouse-click trigger {hit}")
    time.sleep(0.3)
    page.mouse.click(hit["cx"], hit["cy"])
    for attempt in range(16):
        ed, eb = find_editor(page)
        if ed is not None:
            print(f"  composer expanded after {attempt*0.5:.1f}s; editor box={ {k: round(v) for k, v in eb.items()} }")
            return ed, eb
        time.sleep(0.5)
    print("  composer did not expand to a visible editor")
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="swift_viper14")
    ap.add_argument("--sub", default="AskReddit")
    ap.add_argument("--thread", help="explicit thread URL; else auto-pick first non-promoted")
    ap.add_argument("--text", default="ui-composer-dryrun-please-ignore")
    ap.add_argument("--list", action="store_true", help="list current sub threads and exit")
    ap.add_argument("--submit", action="store_true", help="actually click Comment + verify")
    args = ap.parse_args()

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.user / ".env")
    reddit_user = os.environ.get("REDDIT_USERNAME", "")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    config = json.loads((REPO / "accounts" / args.user / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]
    shots = REPO / "accounts" / args.user / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    def shot(page, label):
        p = shots / f"reddit_comment_{stamp}_{label}.png"
        try:
            page.screenshot(path=str(p), timeout=5000)
            return str(p)
        except Exception as e:
            return f"<shot failed: {str(e)[:60]}>"

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[setup] {args.user} started on port {port}  mode={'SUBMIT' if args.submit else 'DRY-RUN'}")
    time.sleep(8)

    rc = 1
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=40000)
            time.sleep(3)
            try:
                browse.dismiss_cookie_popup(page)
            except Exception:
                pass

            # Auth check
            me = page.request.get("https://oauth.reddit.com/api/v1/me", timeout=15000)
            who = ""
            if me.status == 200:
                try:
                    who = me.json().get("name") or ""
                except Exception:
                    pass
            print(f"[auth] /me HTTP {me.status} as {who!r} (expected {reddit_user!r})")
            if me.status != 200:
                print("[fail] not logged in — aborting (login flow not exercised in this spike)")
                return 2

            # Thread
            thread_url = args.thread
            if not thread_url or args.list:
                page.goto(f"https://www.reddit.com/r/{args.sub}/",
                          wait_until="domcontentloaded", timeout=40000)
                time.sleep(4)
                browse.dismiss_cookie_popup(page)
                page.wait_for_selector("shreddit-post", timeout=15000)
                if args.list:
                    posts = page.evaluate("""() => [...document.querySelectorAll('shreddit-post:not([promoted])')].slice(0,20).map(p => ({
                      permalink: p.getAttribute('permalink'),
                      comments: p.getAttribute('comment-count'),
                      title: p.getAttribute('post-title') || ''
                    }))""")
                    print("\n=== current r/%s threads ===" % args.sub)
                    for p in posts:
                        print(f"  {str(p['comments']):>5}c  https://www.reddit.com{p['permalink']}  | {p['title'][:90]}")
                    return 0
                target = page.locator('shreddit-post:not([promoted])').first
                permalink = target.get_attribute("permalink", timeout=5000)
                thread_url = f"https://www.reddit.com{permalink}"
            print(f"[thread] {thread_url}")
            page.goto(thread_url, wait_until="domcontentloaded", timeout=40000)
            time.sleep(5)
            # The cookie-preferences modal re-appears per page and overlays the
            # composer — dismiss it on the thread page too, before interacting.
            for _ in range(3):
                if browse.dismiss_cookie_popup(page):
                    print("  [cookie] dismissed consent modal on thread page")
                    time.sleep(1)
                else:
                    break
            browse.human_scroll(page, duration_s=8)

            # Show OP for context
            try:
                tj = page.request.get(thread_url.rstrip("/") + ".json", timeout=15000).json()
                op = tj[0]["data"]["children"][0]["data"]
                print(f"[OP] {op.get('title','')[:120]!r}  score={op.get('score')} comments={op.get('num_comments')}")
            except Exception as e:
                print(f"[OP] fetch err: {e}")

            print("\n=== open composer (real click on trigger) ===")
            editor, ebox = open_composer(page)
            if editor is None:
                print("[fail] composer did not expand to a visible editor")
                print(f"  shot: {shot(page, 'no_composer')}")
                return 3
            print(f"  shot: {shot(page, 'composer_open')}")

            print("\n=== focus + type via browse.human_type ===")
            page.mouse.click(ebox["x"] + ebox["width"] / 2, ebox["y"] + ebox["height"] / 2)
            time.sleep(0.5)
            browse.human_type(page, args.text)
            time.sleep(0.6)
            typed = page.evaluate("""() => {
              const el = document.querySelector('shreddit-composer [contenteditable="true"]')
                || document.querySelector('div[contenteditable="true"][data-lexical-editor="true"]')
                || document.querySelector('[contenteditable="true"]');
              return el ? (el.innerText||el.textContent||'').trim() : null; }""")
            print(f"  editor text now: {typed!r}")
            print(f"  shot: {shot(page, 'typed')}")
            landed = bool(typed and args.text[:20] in typed)
            print(f"  -> typing {'LANDED' if landed else 'FAILED'}")

            if not args.submit:
                print("\n[dry-run] NOT submitting — pressing Escape / clearing.")
                try:
                    page.keyboard.press("Escape")
                except Exception:
                    pass
                rc = 0 if landed else 4
                print(f"\n=== SUMMARY (dry-run) ===\n  composer opened: yes\n  typing landed: {landed}")
                return rc

            # ===== SUBMIT =====
            print("\n=== SUBMIT (clicking Comment) ===")
            submitted = False
            for label, build in [
                ("role button Comment", lambda: page.get_by_role("button", name=re.compile(r"^comment$", re.I))),
                ("button[type=submit] in composer", lambda: page.locator('shreddit-composer button[type="submit"]')),
            ]:
                loc = build()
                n = loc.count()
                print(f"  submit candidate [{label}] -> {n}")
                for i in range(n):
                    b = loc.nth(i)
                    try:
                        if b.is_visible(timeout=1500) and b.is_enabled():
                            browse.click_element(page, b)
                            submitted = True
                            print(f"    clicked [{label}] #{i}")
                            break
                    except Exception:
                        continue
                if submitted:
                    break
            if not submitted:
                print("  [fail] no submit button clicked")
                print(f"  shot: {shot(page, 'no_submit')}")
                return 5
            time.sleep(4)
            print(f"  shot: {shot(page, 'after_submit')}")

            # ===== VERIFY PERSISTENCE =====
            print("\n=== verify persistence ===")
            found_thread = False
            for attempt in range(6):  # ~ up to 60s, CDN-lag tolerant
                try:
                    vj = page.request.get(thread_url.rstrip("/") + ".json", timeout=15000).json()
                    def walk(items):
                        for it in items:
                            d = it.get("data", {})
                            body = (d.get("body") or "")
                            if args.text[:25] in body and (d.get("author","").lower() == reddit_user.lower()):
                                return d
                            reps = d.get("replies")
                            if isinstance(reps, dict):
                                r = walk(reps.get("data", {}).get("children", []))
                                if r:
                                    return r
                        return None
                    hit = walk(vj[1]["data"]["children"])
                    if hit:
                        found_thread = True
                        print(f"  ✅ visible in thread JSON: id={hit.get('name')} "
                              f"score={hit.get('score')} permalink=https://www.reddit.com{hit.get('permalink','')}")
                        break
                except Exception as e:
                    print(f"  attempt {attempt+1} err: {str(e)[:60]}")
                print(f"  attempt {attempt+1}: not visible yet, waiting...")
                time.sleep(10)

            # Cross-check via the account's own comment history
            usr_visible = False
            if reddit_user:
                try:
                    uj = page.request.get(
                        f"https://www.reddit.com/user/{reddit_user}/comments/.json?limit=5",
                        timeout=15000).json()
                    for ch in uj.get("data", {}).get("children", []):
                        if args.text[:25] in (ch.get("data", {}).get("body") or ""):
                            usr_visible = True
                            print(f"  ✅ present in /user/{reddit_user}/comments")
                            break
                except Exception as e:
                    print(f"  user-history fetch err: {str(e)[:60]}")

            print(f"\n=== SUMMARY (submit) ===")
            print(f"  typed+submitted: yes")
            print(f"  visible in thread: {found_thread}")
            print(f"  visible in user history: {usr_visible}")
            print(f"  verdict: {'PERSISTED' if (found_thread or usr_visible) else 'NOT VISIBLE (shadow/removed?)'}")
            rc = 0 if (found_thread or usr_visible) else 6
    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
