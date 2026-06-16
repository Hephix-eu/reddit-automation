"""Targeted experiment: send real keyboard events to Reddit's comment composer,
diagnosing focus + visibility at every step.

Theory: previous attempts failed because focus was on the wrong element when
keys went out (focus on body, not on the editable). Lexical-based editors
silently ignore keystrokes when their tracked focus target isn't current.

Strategy:
  1. Navigate to a thread.
  2. JS-click the composer trigger (known to work).
  3. Wait for editor's contenteditable to have width > 0 AND
     `document.activeElement` to be inside the composer.
  4. Mouse-click directly on the visible editor (not the trigger) so the
     browser's own focus machinery records this as "user clicked editor".
  5. Type one character at a time with humanlike delays.
  6. After EACH key, log:
       - document.activeElement
       - editor.innerText
       - editor.getBoundingClientRect()
  7. Cancel/Escape before submission — this is a probe, not a real comment.
"""
import argparse
import json
import os
import random
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

TEST_TEXT = "test"  # Tiny — we'll cancel before submit, but keep it minimal.


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
    from playwright.sync_api import sync_playwright

    config = json.loads((REPO / "accounts" / args.user / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[setup] profile started on port {port}")
    time.sleep(8)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # Nav to sub, drill into first non-promoted post
            page.goto(f"https://www.reddit.com/r/{args.sub}/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            page.wait_for_selector("shreddit-post", timeout=15000)
            target = page.locator('shreddit-post:not([promoted])').first
            permalink = target.get_attribute("permalink", timeout=5000)
            thread_url = f"https://www.reddit.com{permalink}"
            print(f"[nav] -> {thread_url}")
            page.goto(thread_url, wait_until="domcontentloaded", timeout=30000)
            time.sleep(6)

            # ===== Step 1: dump all editable surfaces and their state =====
            print(f"\n=== editable surfaces in DOM (light + open shadow) ===")
            surfaces = page.evaluate("""() => {
              const out = [];
              const walk = (root, path='') => {
                const all = root.querySelectorAll('[contenteditable], textarea, input[type=text]');
                for (const el of all) {
                  const r = el.getBoundingClientRect();
                  out.push({
                    tag: el.tagName.toLowerCase(),
                    ce: el.getAttribute('contenteditable') || '',
                    name: el.getAttribute('name') || '',
                    placeholder: el.getAttribute('placeholder') || el.getAttribute('aria-label') || '',
                    rect_w: Math.round(r.width),
                    rect_h: Math.round(r.height),
                    visible_value: (el.value || el.innerText || '').slice(0, 60),
                    aria_haspopup: el.getAttribute('aria-haspopup') || '',
                    path,
                  });
                }
                root.querySelectorAll('*').forEach(el => {
                  if (el.shadowRoot) walk(el.shadowRoot, path + ' > ' + el.tagName.toLowerCase());
                });
              };
              walk(document);
              return out;
            }""")
            print(f"  found {len(surfaces)} editable surfaces (pre-open):")
            for s in surfaces:
                print(f"    <{s['tag']}> ce={s['ce']!r:10} name={s['name']!r:8} ph={s['placeholder'][:30]!r:32} rect={s['rect_w']}x{s['rect_h']} value={s['visible_value'][:30]!r}")

            # ===== Step 2: click composer trigger =====
            print(f"\n=== open composer (JS-direct click on trigger) ===")
            trigger = page.locator("[placeholder='Join the conversation']").first
            if trigger.count() == 0:
                print("[fail] no composer trigger")
                return 2
            trigger.evaluate("el => el.click()")
            time.sleep(3)

            # ===== Step 3: re-dump editable surfaces after open =====
            print(f"\n=== editable surfaces AFTER trigger click ===")
            surfaces = page.evaluate("""() => {
              const out = [];
              const walk = (root, path='') => {
                const all = root.querySelectorAll('[contenteditable], textarea');
                for (const el of all) {
                  const r = el.getBoundingClientRect();
                  out.push({
                    tag: el.tagName.toLowerCase(),
                    ce: el.getAttribute('contenteditable') || '',
                    name: el.getAttribute('name') || '',
                    placeholder: el.getAttribute('placeholder') || el.getAttribute('aria-label') || '',
                    rect_w: Math.round(r.width),
                    rect_h: Math.round(r.height),
                    visible_value: (el.value || el.innerText || '').slice(0, 60),
                    path,
                  });
                }
                root.querySelectorAll('*').forEach(el => {
                  if (el.shadowRoot) walk(el.shadowRoot, path + ' > ' + el.tagName.toLowerCase());
                });
              };
              walk(document);
              return out;
            }""")
            for s in surfaces:
                print(f"    <{s['tag']}> ce={s['ce']!r:10} name={s['name']!r:8} ph={s['placeholder'][:30]!r:32} rect={s['rect_w']}x{s['rect_h']} value={s['visible_value'][:30]!r}")

            # ===== Step 4: pick the VISIBLE editable target =====
            print(f"\n=== picking the visible editable target ===")
            visible_editors = [s for s in surfaces if s['rect_w'] > 50 and (s['ce'] == 'true' or s['tag'] == 'textarea')]
            # Exclude honeypot textareas (those with a long preset value that looks like a CSRF token)
            visible_editors = [s for s in visible_editors if not re.fullmatch(r"[A-Za-z0-9_\-]{30,}", s['visible_value'])]
            print(f"  candidates: {len(visible_editors)}")
            for s in visible_editors:
                print(f"    -> <{s['tag']}> ce={s['ce']} name={s['name']} rect={s['rect_w']}x{s['rect_h']}")

            if not visible_editors:
                print("[fail] no visible editable found after open")
                return 3

            # ===== Step 5: locate the target via Playwright and mouse-click it =====
            # Prefer contenteditable=true with visible rect
            target_locator = None
            for sel_label, build in [
                ("[contenteditable='true']:visible", lambda: page.locator("[contenteditable='true']").filter(visible=True)),
                ("textarea[name='text']",            lambda: page.locator("textarea[name='text']")),
                ("[contenteditable='true']",         lambda: page.locator("[contenteditable='true']")),
            ]:
                try:
                    loc = build()
                    n = loc.count()
                    print(f"  Playwright {sel_label} → {n}")
                    if n > 0 and target_locator is None:
                        target_locator = loc.first
                        print(f"    -> selected {sel_label}")
                except Exception as e:
                    print(f"  {sel_label}: raised {type(e).__name__}: {e}")

            if target_locator is None:
                print("[fail] no Playwright locator for editor")
                return 4

            # Mouse-click on the visible editor's bbox center so focus is recorded by browser
            print(f"\n=== mouse-click editor for focus ===")
            try:
                target_locator.scroll_into_view_if_needed(timeout=5000)
            except Exception as e:
                print(f"  scroll_into_view raised: {e}")
            try:
                box = target_locator.bounding_box(timeout=5000)
                print(f"  bbox: {box}")
                if box:
                    # Click at center of the editor
                    page.mouse.click(box["x"] + box["width"]/2, box["y"] + box["height"]/2)
                    time.sleep(0.5)
                else:
                    print("  no bbox — falling back to locator.click(force=True)")
                    target_locator.click(force=True, timeout=3000)
                    time.sleep(0.5)
            except Exception as e:
                print(f"  click raised: {e}")

            # ===== Step 6: verify focus is now on the editor =====
            active_info = page.evaluate("""() => {
              const a = document.activeElement;
              if (!a) return null;
              return {
                tag: a.tagName.toLowerCase(),
                ce: a.getAttribute('contenteditable') || '',
                name: a.getAttribute('name') || '',
                placeholder: a.getAttribute('placeholder') || a.getAttribute('aria-label') || '',
                inner_len: (a.value || a.innerText || '').length,
              };
            }""")
            print(f"\n=== activeElement after click ===")
            print(f"  {active_info}")

            # ===== Step 7: type one character at a time, log per-char effect =====
            print(f"\n=== typing '{TEST_TEXT}' one key at a time ===")
            for i, ch in enumerate(TEST_TEXT):
                page.keyboard.press(ch)
                time.sleep(random.uniform(0.10, 0.22))  # humanlike
                state = page.evaluate("""() => {
                  const ce = document.querySelector('[contenteditable=\"true\"]');
                  const ta = document.querySelector('textarea[name=\"text\"]') || document.querySelector('textarea');
                  const a = document.activeElement;
                  return {
                    ce_text: ce ? (ce.innerText || ce.textContent || '').trim() : null,
                    ta_value: ta ? (ta.value || '').trim() : null,
                    active_tag: a ? a.tagName.toLowerCase() : null,
                    active_ce: a ? (a.getAttribute('contenteditable') || '') : null,
                  };
                }""")
                print(f"  key {i+1} {ch!r}: active=<{state['active_tag']}> ce={state['active_ce']!r} | ce_text={state['ce_text']!r} | ta_value={state['ta_value']!r}")

            # ===== Step 8: cancel without submitting =====
            print(f"\n=== escape (no submission) ===")
            page.keyboard.press("Escape")
            time.sleep(1)

    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
