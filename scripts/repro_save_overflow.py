"""Reproduction harness: save a post via shreddit's overflow menu.

Tests the two-click pattern (open overflow → click Save) with multiple strategies
side-by-side, screenshots at each phase, captures menu DOM for forensic inspection.

Usage (run inside redditagent-image container so Playwright + browsers are present):
    python3 scripts/repro_save_overflow.py
    python3 scripts/repro_save_overflow.py --user smug_pickle72 --sub AskReddit

Outputs to accounts/<user>/screenshots/repro_save_<timestamp>_<phase>.png.
Stops the Multilogin profile cleanly in a finally block.

Hard rules (mirrors AGENT_PROMPT.md):
- NEVER call browser.close()
- Always c.stop(profile_id) in finally
- Don't submit a real comment / post during repro
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
    ap.add_argument("--sub", default="dotnet", help="subreddit (without r/) to visit for the test")
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
        p = shots / f"repro_save_{stamp}_{label}.png"
        try:
            page.screenshot(path=str(p), full_page=False, timeout=5000)
            return str(p)
        except Exception as e:
            return f"<screenshot failed: {str(e)[:80]}>"

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    port = c.start(folder, pid)
    print(f"[setup] profile {pid} started on port {port}")
    time.sleep(8)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            url = f"https://www.reddit.com/r/{args.sub}/"
            print(f"\n[nav] -> {url} (home feed)")
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            page.wait_for_selector("shreddit-post", timeout=15000)
            print(f"[nav] settled at {page.url}")

            # Pick a non-promoted post and navigate INTO it (overflow only appears on expanded post)
            target = page.locator('shreddit-post:not([promoted])').first
            if target.count() == 0:
                print("[fail] no non-promoted posts on page; aborting")
                return 2
            permalink = target.get_attribute("permalink", timeout=5000) or ""
            thread_url = f"https://www.reddit.com{permalink}"
            print(f"[nav] -> {thread_url} (expanded post)")
            page.goto(thread_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)
            print(f"[shot] {shot(page, '01_thread_landed')}")

            # Dump every button on the thread page so we can SEE what exists
            buttons = page.evaluate("""() => {
              const collect = (root, depth=0) => {
                let out = [];
                if (depth > 5) return out;
                const all = root.querySelectorAll('button, [role="button"], rpl-overflow-button, faceplate-tracker[noun]');
                for (const b of all) {
                  const al = b.getAttribute('aria-label') || '';
                  const txt = (b.innerText || b.textContent || '').trim().slice(0, 40);
                  const id = b.id || '';
                  const test = b.getAttribute('data-testid') || '';
                  const noun = b.getAttribute('noun') || '';
                  const tag = b.tagName.toLowerCase();
                  if (al || txt || id || test || noun) {
                    out.push({tag, aria: al, text: txt, id, testid: test, noun});
                  }
                }
                // Walk into shadow roots
                const all2 = root.querySelectorAll('*');
                for (const el of all2) {
                  if (el.shadowRoot) out = out.concat(collect(el.shadowRoot, depth+1));
                }
                return out;
              };
              return collect(document);
            }""")
            print(f"\n=== ALL buttons/menuitems on thread page ({len(buttons)} total) ===")
            # Group by interesting names: Save, More, Overflow, Share, Award, Hide, Report
            interesting = [b for b in buttons if any(k in (b.get('aria','') + ' ' + b.get('text','') + ' ' + b.get('noun','') + ' ' + b.get('testid','')).lower()
                                                      for k in ['save', 'more', 'overflow', 'options', 'hide', 'report', 'award', 'share'])]
            print(f"--- {len(interesting)} matched save/more/overflow/options/hide/report/award/share ---")
            for b in interesting[:40]:
                print(f"  <{b['tag']}> aria='{b['aria'][:40]}' text='{b['text']}' testid='{b['testid']}' noun='{b['noun']}'")

            # Wait for page to settle before locator probes (some buttons hydrate late)
            time.sleep(4)

            # ===== Phase 1: find the overflow trigger empirically =====
            print(f"\n=== Phase 1: locate overflow trigger ===")
            ov_strategies = [
                ("S1 CSS button[aria-label='More options']",
                 lambda: page.locator("button[aria-label='More options']")),
                ("S2 get_by_label('More options')",
                 lambda: page.get_by_label("More options")),
                ("S3 get_by_label(/more options/i)",
                 lambda: page.get_by_label(re.compile("more options", re.I))),
                ("S4 role=button exact name='More options'",
                 lambda: page.get_by_role("button", name="More options")),
                ("S5 role=button name=~more options regex",
                 lambda: page.get_by_role("button", name=re.compile("more options", re.I))),
                ("S6 XPath //button[@aria-label='More options']",
                 lambda: page.locator("xpath=//button[@aria-label='More options']")),
            ]
            overflow_locator = None
            for label, build in ov_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and overflow_locator is None:
                        overflow_locator = loc.first
                        print(f"    -> selected this strategy")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if overflow_locator is None:
                print("[fail] no strategy found an overflow trigger — see button dump above for candidates")
                return 3

            # ===== Phase 2: open overflow menu (multiple click strategies) =====
            print(f"\n=== Phase 2: open overflow menu — testing click strategies ===")

            # Helper: did the menu open? Check aria-expanded on the button
            def menu_is_open() -> bool:
                try:
                    val = overflow_locator.get_attribute("aria-expanded", timeout=2000)
                    return val == "true"
                except Exception:
                    return False

            click_strategies = [
                ("C1 plain click()",
                 lambda: overflow_locator.click(timeout=5000)),
                ("C2 hover post first, then click()",
                 lambda: (
                     page.locator("shreddit-post").first.hover(timeout=5000),
                     time.sleep(0.5),
                     overflow_locator.hover(timeout=5000),
                     time.sleep(0.3),
                     overflow_locator.click(timeout=5000),
                 )),
                ("C3 JS-direct el.click()",
                 lambda: overflow_locator.evaluate("el => el.click()")),
                ("C4 dispatch_event click",
                 lambda: overflow_locator.dispatch_event("click")),
                ("C5 focus + Enter key",
                 lambda: (overflow_locator.focus(timeout=3000), page.keyboard.press("Enter"))),
            ]

            opened = False
            winner = None
            for label, build in click_strategies:
                if opened:
                    break
                print(f"  trying [{label}]")
                try:
                    build()
                    time.sleep(1.5)
                    if menu_is_open():
                        print(f"    ✅ {label}: aria-expanded=true — menu OPEN")
                        opened = True
                        winner = label
                    else:
                        print(f"    ✗ {label}: aria-expanded still false")
                except Exception as e:
                    print(f"    ✗ {label}: {type(e).__name__}: {str(e)[:140]}")

            if not opened:
                print(f"[fail] no click strategy opened the menu")
                try:
                    page.screenshot(path=str(shots / f"repro_save_{stamp}_03_overflow_fail.png"), full_page=False, timeout=5000)
                except Exception:
                    pass
                return 4
            print(f"  WINNER: {winner}")

            time.sleep(2)
            print(f"[shot] {shot(page, '04_menu_open')}")

            # ===== Phase 3: find Save inside the opened menu =====
            print(f"\n=== Phase 3: locate Save menu item ===")
            save_strategies = [
                ("S1 role=menuitem name=~save",
                 lambda: page.get_by_role("menuitem", name=re.compile(r"^save\b", re.I))),
                ("S2 role=button name=~save (menu may use button role)",
                 lambda: page.get_by_role("button", name=re.compile(r"^save\b", re.I))),
                ("S3 text=Save (exact)",
                 lambda: page.get_by_text("Save", exact=True)),
                ("S4 CSS [data-testid*=save]",
                 lambda: page.locator("[data-testid*='save' i]")),
            ]
            save_locator = None
            for label, build in save_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and save_locator is None:
                        save_locator = loc.first
                        print(f"    -> selected")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if save_locator is None:
                print("[fail] menu open but no Save item found; dumping body innerText snippet around 'Save'")
                hits = page.evaluate("""() => {
                  const all = document.body.innerText;
                  const idx = all.toLowerCase().indexOf('save');
                  return idx === -1 ? '<no Save text on page>' : all.slice(Math.max(0, idx-100), idx+200);
                }""")
                print(hits)
                return 5

            # ===== Phase 4: click Save (Playwright resolves shadow-DOM, JS walks up to clickable) =====
            print(f"\n=== Phase 4: click Save ===")
            # save_locator is the Playwright locator from Phase 3 (pierces shadow).
            # Use .evaluate so the DOM node is in scope and we can walk via parentNode
            # (which crosses shadow boundaries via root.host if needed).
            result = save_locator.evaluate("""el => {
              const isClickable = (e) => e && (e.tagName === 'BUTTON' || e.getAttribute('role') === 'menuitem');
              const climb = (e) => {
                if (e.parentElement) return e.parentElement;
                const root = e.getRootNode();
                return (root && root.host) ? root.host : null;
              };
              let cur = el;
              let depth = 0;
              while (cur && !isClickable(cur) && depth < 30) {
                cur = climb(cur);
                depth++;
              }
              if (isClickable(cur)) {
                cur.click();
                return {ok: true, clicked: cur.tagName + '@role=' + (cur.getAttribute('role') || 'none'), depth};
              }
              return {ok: false, err: 'no clickable ancestor within 30 levels', start_tag: el.tagName};
            }""")
            print(f"  result: {result}")
            if not result.get("ok"):
                return 6

            time.sleep(3.5)  # give Reddit's API time to respond
            print(f"[shot] {shot(page, '06_after_save')}")

            # ===== Phase 5: verify via Playwright locators (which pierce shadow DOM) =====
            print(f"\n=== Phase 5: verify ===")
            try:
                # Re-open the overflow menu (use the proven C3 strategy)
                overflow_locator.evaluate("el => el.click()")
                time.sleep(2)
                # Playwright locators pierce shadow roots — unlike our custom JS.
                unsave_count = page.get_by_text("Unsave", exact=True).count()
                save_count = page.get_by_text("Save", exact=True).count()
                expanded = overflow_locator.get_attribute("aria-expanded", timeout=2000)
                print(f"  menu aria-expanded={expanded} | Save={save_count} | Unsave={unsave_count}")
                if unsave_count > 0:
                    print(f"  ✅ VERIFIED: 'Unsave' present → Save took effect on Reddit")
                elif save_count > 0:
                    print(f"  ✗ Still shows 'Save' (click happened but Reddit didn't toggle)")
                else:
                    print(f"  ⚠ neither — dumping menuitem texts via Playwright:")
                    menuitems = page.get_by_role("menuitem")
                    try:
                        texts = menuitems.all_inner_texts()
                        print(f"    role=menuitem texts ({len(texts)}): {[t[:50] for t in texts]}")
                    except Exception as e:
                        print(f"    menuitem dump failed: {e}")
                    # Also try buttons inside dropdowns
                    try:
                        btns = page.locator("[role=menu] button, faceplate-menu button, faceplate-menu li")
                        print(f"    menu buttons ({btns.count()}): {btns.all_inner_texts()[:15]}")
                    except Exception as e:
                        print(f"    menu button dump failed: {e}")
                print(f"[shot] {shot(page, '07_verify_menu_open')}")
                page.keyboard.press("Escape")
            except Exception as e:
                print(f"  verify raised: {type(e).__name__}: {e}")

    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
