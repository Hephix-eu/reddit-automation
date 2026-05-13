"""Dismiss Reddit's cookie consent dialog once so it persists in cookies."""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib.multilogin import session


def main():
    account = sys.argv[1] if len(sys.argv) > 1 else "HovercraftWeary8654"
    account_dir = ROOT / "accounts" / account
    config = json.loads((account_dir / "config.json").read_text())
    for line in (account_dir / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v

    from playwright.sync_api import sync_playwright

    with session(config) as (mlx, pid, port):
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = b.contexts[0].pages[0]
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded")
            time.sleep(3)
            # Reject minimizes data exposure
            for label in ("Reject non-essential", "Reject Optional Cookies", "Reject all"):
                try:
                    btn = page.get_by_role("button", name=label).first
                    if btn.is_visible(timeout=2000):
                        btn.click()
                        print(f"clicked: {label}")
                        time.sleep(2)
                        break
                except Exception:
                    pass
            page.screenshot(path=str(account_dir / "screenshots" / "after_cookie_dismiss.png"))


if __name__ == "__main__":
    main()
