"""Standalone CreepJS diagnostic — capture console errors + text snapshots over time."""
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib.multilogin import session


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "HovercraftWeary8654"
    account_dir = ROOT / "accounts" / username
    config = json.loads((account_dir / "config.json").read_text())
    for line in (account_dir / ".env").read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ[k] = v

    from playwright.sync_api import sync_playwright

    console_msgs = []
    page_errors = []

    with session(config) as (mlx, pid, port):
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            page.on("console", lambda m: console_msgs.append(f"[{m.type}] {m.text[:200]}"))
            page.on("pageerror", lambda e: page_errors.append(str(e)[:300]))

            print("Navigating to CreepJS (networkidle wait, up to 120s)...")
            try:
                page.goto("https://abrahamjuliot.github.io/creepjs/",
                          wait_until="networkidle", timeout=120_000)
            except Exception as e:
                print(f"  goto timeout/error: {e}")

            # Sample text every 15s for 2 minutes
            for i in range(8):
                time.sleep(15)
                txt = page.evaluate("() => document.body.innerText") or ""
                # Extract first 600 chars after stripping repeated blanks
                lines = [l for l in txt.splitlines() if l.strip()]
                head = "\n  ".join(lines[:25])
                print(f"\n--- t+{(i+1)*15}s ({len(txt)} chars total) ---")
                print(f"  {head}")
                if "Trust Score" in txt and any(c.isdigit() for c in txt.split("Trust Score",1)[1][:50]):
                    print("\n>>> Trust Score number detected, stopping")
                    break

            print(f"\n--- console ({len(console_msgs)} msgs) ---")
            for m in console_msgs[:20]:
                print(f"  {m}")
            print(f"\n--- page errors ({len(page_errors)}) ---")
            for e in page_errors:
                print(f"  {e}")


if __name__ == "__main__":
    main()
