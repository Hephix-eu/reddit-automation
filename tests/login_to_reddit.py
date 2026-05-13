"""One-time Reddit login via Multilogin profile + Playwright.

Drives the existing profile to reddit.com, clicks Log In, types credentials
at human pace, screenshots every state. Closes cleanly so cookies persist.

Usage:
    python tests/login_to_reddit.py <account-folder-name>
"""

import json
import os
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib.multilogin import session


def load_env(env_path: Path):
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v


def human_type(page, selector, text, *, char_delay=(80, 150)):
    """Type into a field at human-realistic pace."""
    page.click(selector)
    time.sleep(random.uniform(0.4, 0.9))
    for ch in text:
        page.keyboard.type(ch, delay=random.uniform(*char_delay))


def snap(page, account_dir, label):
    p = account_dir / "screenshots" / f"login_{label}_{int(time.time())}.png"
    page.screenshot(path=str(p), full_page=False)
    print(f"  screenshot: {p.name}")
    return p


def main():
    account_name = sys.argv[1] if len(sys.argv) > 1 else "HovercraftWeary8654"
    account_dir = ROOT / "accounts" / account_name
    config = json.loads((account_dir / "config.json").read_text())
    load_env(account_dir / ".env")

    reddit_user = os.environ["REDDIT_USERNAME"]
    reddit_pass = os.environ["REDDIT_PASSWORD"]
    print(f"Logging in as: {reddit_user}")

    from playwright.sync_api import sync_playwright

    with session(config) as (mlx, pid, port):
        print(f"  profile started: port={port}")
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_viewport_size({"width": 1366, "height": 850})

            # 1. Land on home (natural arrival, not /login directly)
            print("Step 1: navigate to reddit.com")
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(random.uniform(2.5, 4.0))
            snap(page, account_dir, "1_home")

            # 2. Click Log In
            print("Step 2: click Log In")
            try:
                page.get_by_role("button", name="Log In").first.click(timeout=10_000)
            except Exception:
                # Fallback: link by text
                page.get_by_text("Log In", exact=False).first.click(timeout=10_000)
            time.sleep(random.uniform(1.5, 2.5))
            snap(page, account_dir, "2_login_modal")

            # 3. Type username (field selector evolves; try several)
            print("Step 3: type username")
            user_field = None
            for sel in ['input[name="username"]', 'input#login-username',
                        'input[autocomplete="username"]', 'input[type="text"]']:
                try:
                    if page.locator(sel).first.is_visible(timeout=2000):
                        user_field = sel
                        break
                except Exception:
                    continue
            if not user_field:
                snap(page, account_dir, "3_fail_no_username_field")
                raise RuntimeError("could not find username field")
            print(f"  field: {user_field}")
            human_type(page, user_field, reddit_user)
            time.sleep(random.uniform(0.6, 1.2))

            # 4. Type password
            print("Step 4: type password")
            pass_field = None
            for sel in ['input[name="password"]', 'input#login-password',
                        'input[autocomplete="current-password"]', 'input[type="password"]']:
                try:
                    if page.locator(sel).first.is_visible(timeout=2000):
                        pass_field = sel
                        break
                except Exception:
                    continue
            if not pass_field:
                snap(page, account_dir, "4_fail_no_password_field")
                raise RuntimeError("could not find password field")
            human_type(page, pass_field, reddit_pass)
            time.sleep(random.uniform(0.8, 1.5))
            snap(page, account_dir, "4_filled")

            # 5. Submit (click button, not Enter — looks more human)
            print("Step 5: click submit")
            submit_clicked = False
            for sel in ['button[type="submit"]', 'button:has-text("Log In")',
                        'button:has-text("Continue")']:
                try:
                    btn = page.locator(sel).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        submit_clicked = True
                        break
                except Exception:
                    continue
            if not submit_clicked:
                page.keyboard.press("Enter")
                print("  fallback: Enter key")

            # 6. Wait for either redirect (success) or error message
            print("Step 6: wait for outcome (15s)")
            time.sleep(15)
            snap(page, account_dir, "6_outcome")

            # 7. Check logged-in state via /user/me redirect trick
            from mlx_client import Client
            who = Client.discover_reddit_user(page)
            if who:
                print(f"  [OK] logged in as: {who}")
                if who.lower() != reddit_user.lower():
                    print(f"  WARN: expected {reddit_user!r}, got {who!r}")
            else:
                print(f"  [FAIL] /user/me did not resolve — likely not logged in")
                snap(page, account_dir, "7_user_me_check")
                raise SystemExit(1)

            # 8. Brief dwell so session cookies set, then exit Playwright
            time.sleep(random.uniform(3, 6))
            print("  dwell complete, exiting Playwright cleanly")

        print(f"  profile stopping via API")
    print("DONE")


if __name__ == "__main__":
    main()
