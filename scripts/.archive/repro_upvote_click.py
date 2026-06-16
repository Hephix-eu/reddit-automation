"""Reproduction harness: upvote a post on r/dotnet home feed.

Tests selector + click strategies side-by-side, verifies via aria-pressed
toggle, then unvotes to leave smug_pickle72's state clean.

Hypothesis: same root cause as `repro_save_overflow.py` — upvote button is
hover-revealed (`tabindex=-1`), so Playwright's `.click()` waits forever on
the visibility check. JS-direct `el.click()` should bypass.

Usage (run inside redditagent-image container):
    python3 scripts/repro_upvote_click.py
    python3 scripts/repro_upvote_click.py --user smug_pickle72 --sub dotnet
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


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="smug_pickle72")
    ap.add_argument("--sub", default="dotnet")
    ap.add_argument("--leave-voted", action="store_true",
                    help="Skip the cleanup unvote (default: unvote at end)")
    args = ap.parse_args()

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.user / ".env")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    config = json.loads((REPO / "accounts" / args.user / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]
    shots = REPO / "accounts" / args.user / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    def shot(page, label: str) -> str:
        p = shots / f"repro_upvote_{stamp}_{label}.png"
        try:
            page.screenshot(path=str(p), full_page=False, timeout=5000)
            return str(p)
        except Exception as e:
            return f"<screenshot failed: {str(e)[:80]}>"

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()

    # Defensive: stop the profile if it's already running (leftover from prior session)
    try:
        c.stop(pid)
        time.sleep(2)
    except Exception:
        pass

    port = c.start(folder, pid)
    print(f"[setup] profile {pid} started on port {port}")
    time.sleep(8)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            url = f"https://www.reddit.com/r/{args.sub}/"
            print(f"\n[nav] -> {url}")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            page.wait_for_selector("shreddit-post", timeout=15000)
            print(f"[nav] settled at {page.url}")
            print(f"[shot] {shot(page, '01_landed')}")

            # ===== Phase 1: locate upvote buttons (compare strategies) =====
            print(f"\n=== Phase 1: locate upvote buttons ===")
            sel_strategies = [
                ("S1 CSS button[upvote] (attribute selector)",
                 lambda: page.locator("button[upvote]")),
                ("S2 CSS button[aria-label*='upvote' i]",
                 lambda: page.locator("button[aria-label*='upvote' i]")),
                ("S3 role=button name=~upvote",
                 lambda: page.get_by_role("button", name=re.compile("upvote", re.I))),
                ("S4 shreddit-post button[upvote]",
                 lambda: page.locator("shreddit-post button[upvote]")),
                ("S5 [aria-pressed][upvote]",
                 lambda: page.locator("button[upvote][aria-pressed]")),
            ]
            chosen = None
            for label, build in sel_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and chosen is None:
                        chosen = (label, loc.first)
                        print(f"    -> selected this strategy")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if chosen is None:
                print("[fail] no selector found upvote buttons")
                return 2

            sel_label, upvote = chosen

            # Scope to the FIRST non-promoted post so we're testing on a specific known target
            target_post = page.locator('shreddit-post:not([promoted])').first
            permalink = target_post.get_attribute("permalink", timeout=5000) or "<unknown>"
            print(f"[target] first non-promoted post permalink={permalink}")
            # Re-resolve upvote scoped to that post (more deterministic for verify)
            upvote = target_post.locator("button[upvote]").first
            n_in_post = target_post.locator("button[upvote]").count()
            print(f"  upvote buttons inside target post: {n_in_post}")

            # Read initial aria-pressed (this is the truth signal)
            initial_pressed = upvote.get_attribute("aria-pressed", timeout=5000)
            print(f"  initial aria-pressed = {initial_pressed!r}")
            if initial_pressed == "true":
                print(f"  ⚠ post is ALREADY upvoted (probably from this morning's session)")
                print(f"  test would be a no-op + then unvote; bailing to avoid confusion")
                return 3

            # ===== Phase 2: click strategies =====
            print(f"\n=== Phase 2: click strategies (verify via aria-pressed) ===")
            click_strategies = [
                ("C1 plain click() (5s)",
                 lambda: upvote.click(timeout=5000)),
                ("C2 hover post → click()",
                 lambda: (target_post.hover(timeout=5000),
                          time.sleep(0.3),
                          upvote.click(timeout=5000))),
                ("C3 JS-direct el.click()",
                 lambda: upvote.evaluate("el => el.click()")),
                ("C4 dispatch_event click",
                 lambda: upvote.dispatch_event("click")),
                ("C5 scroll into view + JS click",
                 lambda: (upvote.scroll_into_view_if_needed(timeout=5000),
                          time.sleep(0.3),
                          upvote.evaluate("el => el.click()"))),
            ]

            winner = None
            for label, build in click_strategies:
                if winner:
                    break
                print(f"  trying [{label}]")
                try:
                    build()
                    time.sleep(1.5)
                    now = upvote.get_attribute("aria-pressed", timeout=3000)
                    print(f"    aria-pressed after = {now!r}")
                    if now == "true":
                        winner = label
                        print(f"    ✅ {label} flipped aria-pressed to true")
                    else:
                        print(f"    ✗ {label} did not toggle")
                except PWTimeout as e:
                    print(f"    ✗ {label}: TIMEOUT {str(e)[:120]}")
                except Exception as e:
                    print(f"    ✗ {label}: {type(e).__name__}: {str(e)[:120]}")

            if winner is None:
                print(f"[fail] no click strategy flipped aria-pressed; dumping button HTML")
                html = upvote.evaluate("el => el.outerHTML.slice(0, 600)")
                print(html)
                return 4

            print(f"\n  WINNER: {winner}")
            print(f"[shot] {shot(page, '02_upvoted')}")

            # ===== Phase 3: verify by reading aria-pressed fresh =====
            print(f"\n=== Phase 3: verify ===")
            # Re-resolve in case DOM rewrote
            v = target_post.locator("button[upvote]").first.get_attribute("aria-pressed", timeout=3000)
            print(f"  fresh aria-pressed read: {v!r}")
            if v == "true":
                print(f"  ✅ verified — upvote persisted on Reddit's side")
            else:
                print(f"  ⚠ aria-pressed reverted to {v!r} — Reddit may have rejected (rate limit? duplicate?)")

            # ===== Phase 4: cleanup home-feed upvote (unvote so we can test thread page from clean state) =====
            print(f"\n=== Phase 4: cleanup home-feed upvote ===")
            try:
                fresh = target_post.locator("button[upvote]").first
                fresh.evaluate("el => el.click()")
                time.sleep(1.5)
                final = fresh.get_attribute("aria-pressed", timeout=3000)
                print(f"  aria-pressed after unvote = {final!r}")
                if final == "false":
                    print(f"  ✅ unvoted cleanly")
                else:
                    print(f"  ⚠ unvote may not have taken (state={final}) — thread test may start dirty")
            except Exception as e:
                print(f"  cleanup raised: {type(e).__name__}: {e}")

            # ===== Phase 5: navigate to thread page, repeat upvote test =====
            thread_url = f"https://www.reddit.com{permalink}"
            print(f"\n=== Phase 5: thread page upvote test ===")
            print(f"[nav] -> {thread_url}")
            page.goto(thread_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            print(f"[shot] {shot(page, '03_thread_landed')}")

            # On the thread page, the upvote button is in rpl-action-bar — likely hover-hidden.
            # Confirm selector still works.
            thread_strategies = [
                ("S1 button[upvote] (page-wide)", lambda: page.locator("button[upvote]")),
                ("S2 rpl-action-bar button[upvote]", lambda: page.locator("rpl-action-bar button[upvote]")),
                ("S3 shreddit-post button[upvote]", lambda: page.locator("shreddit-post button[upvote]")),
                ("S4 button[aria-label='upvote' i]", lambda: page.locator("button[aria-label='upvote' i]")),
            ]
            t_chosen = None
            for label, build in thread_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and t_chosen is None:
                        t_chosen = (label, loc.first)
                        print(f"    -> selected")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if t_chosen is None:
                print(f"[fail] no upvote button found on thread page")
                return 5

            _, thread_upvote = t_chosen
            # Dump HTML so we can see tabindex/visibility
            html = thread_upvote.evaluate("el => el.outerHTML.slice(0, 300)")
            print(f"  thread upvote outerHTML: {html}")
            initial_t = thread_upvote.get_attribute("aria-pressed", timeout=5000)
            print(f"  thread initial aria-pressed = {initial_t!r}")
            if initial_t == "true":
                print(f"  ⚠ thread upvote is ALREADY pressed (home-feed unvote may have failed)")
                return 6

            # Same click ladder
            click_strategies = [
                ("C1 plain click() (5s)",
                 lambda: thread_upvote.click(timeout=5000)),
                ("C2 hover post → click()",
                 lambda: (page.locator("shreddit-post").first.hover(timeout=5000),
                          time.sleep(0.3),
                          thread_upvote.click(timeout=5000))),
                ("C3 JS-direct el.click()",
                 lambda: thread_upvote.evaluate("el => el.click()")),
                ("C4 dispatch_event click",
                 lambda: thread_upvote.dispatch_event("click")),
                ("C5 scroll into view + JS click",
                 lambda: (thread_upvote.scroll_into_view_if_needed(timeout=5000),
                          time.sleep(0.3),
                          thread_upvote.evaluate("el => el.click()"))),
            ]
            t_winner = None
            for label, build in click_strategies:
                if t_winner:
                    break
                print(f"  trying [{label}]")
                try:
                    build()
                    time.sleep(1.5)
                    now = thread_upvote.get_attribute("aria-pressed", timeout=3000)
                    print(f"    aria-pressed after = {now!r}")
                    if now == "true":
                        t_winner = label
                        print(f"    ✅ {label} flipped on thread page")
                    else:
                        print(f"    ✗ {label} did not toggle")
                except PWTimeout as e:
                    print(f"    ✗ {label}: TIMEOUT {str(e)[:120]}")
                except Exception as e:
                    print(f"    ✗ {label}: {type(e).__name__}: {str(e)[:120]}")

            if t_winner is None:
                print(f"[fail] no thread-page click strategy worked")
                return 7
            print(f"\n  THREAD WINNER: {t_winner}")
            print(f"[shot] {shot(page, '04_thread_upvoted')}")

            # ===== Phase 6: cleanup thread upvote =====
            if not args.leave_voted:
                print(f"\n=== Phase 6: cleanup thread upvote ===")
                try:
                    page.locator("button[upvote]").first.evaluate("el => el.click()")
                    time.sleep(1.5)
                    fp = page.locator("button[upvote]").first.get_attribute("aria-pressed", timeout=3000)
                    print(f"  aria-pressed after unvote = {fp!r}")
                    if fp == "false":
                        print(f"  ✅ thread unvoted cleanly — state fully restored")
                    else:
                        print(f"  ⚠ thread unvote state={fp}")
                except Exception as e:
                    print(f"  thread cleanup raised: {type(e).__name__}: {e}")
            else:
                print(f"\n[skip] --leave-voted — keeping thread upvote in place")

            # Summary
            print(f"\n=== SUMMARY ===")
            print(f"  Home feed: {winner}")
            print(f"  Thread page: {t_winner}")

    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
