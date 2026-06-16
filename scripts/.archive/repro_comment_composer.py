"""Reproduction harness: open Reddit's comment composer, type test text, close.

Smug's morning session logged `composer_open_failed` × 5 followed by
`scroll_into_view_if_needed: Timeout` × 3 — but the error tokens don't tell us
which phase fails. This script breaks composer interaction into 4 phases and
identifies the exact failure.

**HARD RULE: never submits.** Types nonsense text then presses Escape / clicks
Cancel. If a phase fails, exits with diagnostic and the screenshots show state.

Phases:
  1. Find composer trigger (placeholder "Add a comment" button or similar)
  2. Open composer (click trigger → rich editor visible)
  3. Find text input (contenteditable / textarea / etc.)
  4. Type test text → verify it appears
  5. Cancel / Escape → verify composer closed

Usage (inside redditagent-image container):
    python3 scripts/repro_comment_composer.py
    python3 scripts/repro_comment_composer.py --user smug_pickle72 --sub dotnet
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

TEST_TEXT = "reddit-warmup-test-please-ignore-do-not-submit"


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
        p = shots / f"repro_comment_{stamp}_{label}.png"
        try:
            page.screenshot(path=str(p), full_page=False, timeout=5000)
            return str(p)
        except Exception as e:
            return f"<screenshot failed: {str(e)[:80]}>"

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
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

            # Land on sub home, pick first non-promoted post, drill into thread
            page.goto(f"https://www.reddit.com/r/{args.sub}/",
                      wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            page.wait_for_selector("shreddit-post", timeout=15000)
            target = page.locator('shreddit-post:not([promoted])').first
            permalink = target.get_attribute("permalink", timeout=5000)
            thread_url = f"https://www.reddit.com{permalink}"
            print(f"\n[nav] -> {thread_url}")
            page.goto(thread_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for hydration (lesson from upvote repro)
            time.sleep(6)
            print(f"[shot] {shot(page, '01_thread_landed')}")

            # ===== Diagnostic: dump all comment-related elements =====
            print(f"\n=== Diagnostic dump: comment / composer / reply elements ===")
            elements = page.evaluate("""() => {
              const walk = (root, depth=0, out=[]) => {
                if (depth > 6) return out;
                const all = root.querySelectorAll('*');
                for (const e of all) {
                  const aria = (e.getAttribute('aria-label') || '').toLowerCase();
                  const ph = (e.getAttribute('placeholder') || '').toLowerCase();
                  const id = (e.id || '').toLowerCase();
                  const test = (e.getAttribute('data-testid') || '').toLowerCase();
                  const tag = e.tagName.toLowerCase();
                  const text = ((e.innerText || '').slice(0, 40)).trim().toLowerCase();
                  const blob = [aria, ph, id, test, tag, text].join('|');
                  if (/comment|composer|reply|join the conversation|add a comment/.test(blob)) {
                    out.push({
                      tag, aria: e.getAttribute('aria-label') || '',
                      placeholder: e.getAttribute('placeholder') || '',
                      id: e.id || '', testid: e.getAttribute('data-testid') || '',
                      role: e.getAttribute('role') || '',
                      ce: e.getAttribute('contenteditable') || '',
                      text: (e.innerText || '').trim().slice(0, 60),
                    });
                  }
                  if (e.shadowRoot) walk(e.shadowRoot, depth+1, out);
                }
                return out;
              };
              return walk(document);
            }""")
            # Dedupe by signature
            seen = set(); uniq = []
            for el in elements:
                k = (el['tag'], el['aria'], el['placeholder'], el['testid'], el['role'], el['text'][:30])
                if k not in seen:
                    seen.add(k); uniq.append(el)
            print(f"  {len(uniq)} unique candidates:")
            for el in uniq[:25]:
                tag = el['tag']
                interesting = f"aria='{el['aria'][:40]}' placeholder='{el['placeholder'][:30]}' role='{el['role']}' ce='{el['ce']}' testid='{el['testid'][:30]}'"
                print(f"    <{tag}> {interesting} text='{el['text'][:40]}'")

            # ===== Phase 1: locate composer trigger =====
            print(f"\n=== Phase 1: locate composer trigger ===")
            trigger_strategies = [
                ("S1 placeholder=Add a comment",
                 lambda: page.locator("[placeholder='Add a comment']")),
                ("S2 placeholder=Join the conversation",
                 lambda: page.locator("[placeholder='Join the conversation']")),
                ("S3 button name=Add a comment",
                 lambda: page.get_by_role("button", name=re.compile("add a comment|join the conversation", re.I))),
                ("S4 shreddit-async-loader[bundlename*=comment]",
                 lambda: page.locator("shreddit-async-loader[bundlename*='comment' i]")),
                ("S5 shreddit-composer (custom element)",
                 lambda: page.locator("shreddit-composer")),
                ("S6 textarea[name=text]",
                 lambda: page.locator("textarea[name='text']")),
                ("S7 contenteditable=true",
                 lambda: page.locator("[contenteditable='true']")),
                ("S8 faceplate-tracker[noun=comments_textbox]",
                 lambda: page.locator("faceplate-tracker[noun*='comment' i][noun*='textbox' i]")),
            ]
            trigger = None
            trigger_label = None
            for label, build in trigger_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and trigger is None:
                        trigger = loc.first
                        trigger_label = label
                        print(f"    -> selected this strategy")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if trigger is None:
                print(f"[fail] no composer trigger located")
                print(f"[shot] {shot(page, '02_no_trigger')}")
                return 2

            # Read attributes for context
            try:
                outer = trigger.evaluate("el => el.outerHTML.slice(0, 400)")
                print(f"  trigger outerHTML: {outer}")
            except Exception as e:
                print(f"  outerHTML dump failed: {e}")

            # ===== Phase 2: open composer (try plain click first, JS-direct as fallback) =====
            print(f"\n=== Phase 2: open composer ===")
            opened = False
            open_strategies = [
                ("O1 plain click()", lambda: trigger.click(timeout=5000)),
                ("O2 scroll_into_view + click", lambda: (
                    trigger.scroll_into_view_if_needed(timeout=5000),
                    time.sleep(0.3),
                    trigger.click(timeout=5000),
                )),
                ("O3 JS-direct el.click()", lambda: trigger.evaluate("el => el.click()")),
                ("O4 focus → press Enter", lambda: (
                    trigger.focus(timeout=3000),
                    page.keyboard.press("Enter"),
                )),
            ]

            for label, build in open_strategies:
                if opened:
                    break
                print(f"  trying [{label}]")
                try:
                    build()
                    time.sleep(2)
                    # Check for an expanded composer: contenteditable=true visible somewhere new
                    editable_count = page.locator("[contenteditable='true']").count()
                    has_submit = page.get_by_role("button", name=re.compile(r"^comment$", re.I)).count()
                    print(f"    after: contenteditable[true] count={editable_count}, Comment button count={has_submit}")
                    if editable_count > 0 or has_submit > 0:
                        opened = True
                        print(f"    ✅ {label} opened the composer")
                except PWTimeout as e:
                    print(f"    ✗ {label}: TIMEOUT {str(e)[:120]}")
                except Exception as e:
                    print(f"    ✗ {label}: {type(e).__name__}: {str(e)[:120]}")

            if not opened:
                print(f"[fail] no open strategy opened the composer")
                print(f"[shot] {shot(page, '03_no_open')}")
                return 3

            print(f"[shot] {shot(page, '03_composer_open')}")

            # ===== Phase 3: find the text input =====
            print(f"\n=== Phase 3: locate text input ===")
            input_strategies = [
                ("I1 contenteditable=true (first)",
                 lambda: page.locator("[contenteditable='true']").first),
                ("I2 textarea[name=text]",
                 lambda: page.locator("textarea[name='text']")),
                ("I3 textarea (any)",
                 lambda: page.locator("textarea")),
                ("I4 role=textbox name=~comment",
                 lambda: page.get_by_role("textbox", name=re.compile("comment", re.I))),
            ]
            text_input = None
            for label, build in input_strategies:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  [{label}] count={n}")
                    if n > 0 and text_input is None:
                        text_input = loc.first
                        print(f"    -> selected")
                except Exception as e:
                    print(f"  [{label}] raised: {type(e).__name__}: {e}")

            if text_input is None:
                print(f"[fail] composer open but no text input found")
                return 4

            # ===== Phase 4: type test text & verify it appears =====
            print(f"\n=== Phase 4: type test text ===")

            # Diagnostic: what IS the actual contenteditable element? Multiple textareas exist (3),
            # and 1 contenteditable — confirm which holds the editable focus area.
            ce_info = page.evaluate("""() => {
              const els = Array.from(document.querySelectorAll('[contenteditable="true"]'));
              return els.map(e => ({
                tag: e.tagName.toLowerCase(),
                parent_tag: e.parentElement?.tagName.toLowerCase() || '',
                rect: e.getBoundingClientRect().width > 0,
                aria: e.getAttribute('aria-label') || '',
                placeholder: e.getAttribute('data-placeholder') || e.getAttribute('placeholder') || '',
                inner: (e.innerText || '').slice(0, 40),
              }));
            }""")
            print(f"  contenteditable=true elements: {ce_info}")

            typed_ok = False
            type_strategies = [
                ("T1 click editor → keyboard.type",
                 lambda: (text_input.click(timeout=5000, force=True),
                          time.sleep(0.3),
                          page.keyboard.type(TEST_TEXT, delay=50))),
                ("T2 .fill(text)",
                 lambda: text_input.fill(TEST_TEXT, timeout=5000)),
                ("T3 focus → execCommand insertText",
                 lambda: (text_input.evaluate("el => el.focus()"),
                          time.sleep(0.3),
                          page.evaluate(f"() => document.execCommand('insertText', false, {json.dumps(TEST_TEXT)})"))),
                ("T4 dispatch beforeinput + execCommand",
                 lambda: (text_input.evaluate("el => el.focus()"),
                          time.sleep(0.3),
                          text_input.evaluate(f"""el => {{
                              const data = {json.dumps(TEST_TEXT)};
                              const ev = new InputEvent('beforeinput', {{
                                inputType: 'insertText', data, bubbles: true, cancelable: true
                              }});
                              el.dispatchEvent(ev);
                              document.execCommand('insertText', false, data);
                          }}""") )),
                ("T5 press_sequentially (Playwright's recommended for editors)",
                 lambda: (text_input.click(timeout=5000, force=True),
                          time.sleep(0.3),
                          text_input.press_sequentially(TEST_TEXT, delay=40, timeout=15000))),
            ]
            for label, build in type_strategies:
                if typed_ok:
                    break
                print(f"  trying [{label}]")
                try:
                    build()
                    time.sleep(1)
                    # Re-resolve the editable in case DOM changed
                    val = page.evaluate("""() => {
                      const ce = document.querySelector('[contenteditable="true"]');
                      const ta = document.querySelector('textarea[name="text"]') || document.querySelector('textarea');
                      return {
                        ce_text: ce ? (ce.innerText || ce.textContent || '').trim().slice(0, 100) : null,
                        ta_value: ta ? (ta.value || '').trim().slice(0, 100) : null,
                        active: document.activeElement?.tagName?.toLowerCase() || '',
                      };
                    }""")
                    print(f"    after: {val}")
                    if val.get("ce_text") and TEST_TEXT[:30] in val["ce_text"]:
                        typed_ok = True
                        print(f"    ✅ {label} typed text (found in contenteditable)")
                    elif val.get("ta_value") and TEST_TEXT[:30] in val["ta_value"]:
                        typed_ok = True
                        print(f"    ✅ {label} typed text (found in textarea)")
                    else:
                        print(f"    ✗ {label} — text didn't appear")
                except PWTimeout as e:
                    print(f"    ✗ {label}: TIMEOUT {str(e)[:120]}")
                except Exception as e:
                    print(f"    ✗ {label}: {type(e).__name__}: {str(e)[:120]}")

            print(f"[shot] {shot(page, '04_text_typed')}")

            if not typed_ok:
                print(f"[fail] composer open but typing failed")
                return 5

            # ===== Phase 5: close composer WITHOUT submitting =====
            print(f"\n=== Phase 5: close without submitting ===")
            # Try Escape first (most composers respect it), then Cancel button, then nav away
            try:
                page.keyboard.press("Escape")
                time.sleep(1.5)
                still_open = page.locator("[contenteditable='true']").count()
                if still_open == 0:
                    print(f"  ✅ Escape closed the composer")
                else:
                    print(f"  Escape didn't close (still {still_open} editable); trying Cancel button")
                    cancel = page.get_by_role("button", name=re.compile(r"^cancel$", re.I))
                    if cancel.count() > 0:
                        try:
                            cancel.first.click(timeout=3000)
                            time.sleep(1.5)
                            print(f"  ✅ Cancel button clicked")
                        except Exception:
                            cancel.first.evaluate("el => el.click()")
                            time.sleep(1.5)
                            print(f"  ✅ Cancel JS-direct click")
                    else:
                        # Last resort: navigate away (Reddit's auto-draft might preserve, but at least we exit)
                        page.goto("about:blank", timeout=10000)
                        print(f"  ⚠ no Cancel button; navigated to about:blank")
            except Exception as e:
                print(f"  close raised: {type(e).__name__}: {e}")

            # ===== Summary =====
            print(f"\n=== SUMMARY ===")
            print(f"  trigger:    {trigger_label}")
            print(f"  open path:  (see Phase 2 winner above)")
            print(f"  no submission performed")

    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
