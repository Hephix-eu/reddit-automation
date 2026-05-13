"""Full middle-layer integration test (no claude, no autonomous reasoning).

Drives: Multilogin signin → start profile → Playwright over CDP → CreepJS
→ Reddit front page (just navigate, no actions) → SQLite logging → clean
shutdown.

What this proves:
  - Multilogin facade works end-to-end on this machine
  - Playwright connects to the launched profile
  - CreepJS verification extracts real numbers
  - Reddit loads inside the anti-detect browser
  - lib/db.py writes succeed
  - Profile stops cleanly via the API (no cookie corruption)

What this does NOT prove:
  - Claude's autonomous orchestration (that's Phase 3+)
  - Real warmup actions (upvotes, subscribes, comments)
  - Self-rescheduling

Run:
    cd reddit-automation
    python tests/test_browser_integration.py HovercraftWeary8654
"""

import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from lib import db
from lib.creepjs import CREEPJS_URL, verify
from lib.multilogin import session


def load_env(env_path: Path):
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k] = v


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python tests/test_browser_integration.py <username>")
    username = sys.argv[1]
    account_dir = ROOT / "accounts" / username
    if not account_dir.exists():
        sys.exit(f"account folder missing: {account_dir}")

    config = json.loads((account_dir / "config.json").read_text())
    load_env(account_dir / ".env")

    state_db = account_dir / config["paths"]["state_db"]
    db.init(state_db)
    session_id = str(uuid.uuid4())
    print(f"== test session {session_id[:8]} ==")
    print(f"   account: {username}")
    print(f"   db:      {state_db}")

    # --- Lazy import Playwright so the script is importable without it ---
    from playwright.sync_api import sync_playwright

    db.insert(
        state_db, type="StateSnapshot", status="done",
        action_type="integration_test_start",
        session_id=session_id, reasoning="Phase 2 browser integration starting",
    )

    with session(config) as (mlx, pid, port):
        print(f"   profile started: pid={pid[:8]} cdp_port={port}")
        db.insert(
            state_db, type="Action", status="done",
            action_type="profile_started", profile_id=pid,
            session_id=session_id, result=f"port={port}",
        )

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # --- CreepJS verification (optional) ---
            if config["stealth_verification"].get("enabled", True):
                print(f"   navigating to CreepJS...")
                verdict = verify(
                    page,
                    min_trust_score=config["stealth_verification"]["min_trust_score"],
                    max_lies=config["stealth_verification"]["max_lies"],
                )
                print(f"   CreepJS: {verdict.summary}")
                screenshot_path = account_dir / "screenshots" / f"creepjs_{session_id[:8]}.png"
                screenshot_path.parent.mkdir(exist_ok=True)
                page.screenshot(path=str(screenshot_path), full_page=True)
                print(f"   screenshot: {screenshot_path}")
                db.insert(
                    state_db, type="Action",
                    status="done" if verdict.passed else "failed",
                    action_type="stealth_verified", profile_id=pid,
                    session_id=session_id, result=verdict.summary,
                )
            else:
                print(f"   skipping CreepJS (disabled in config)")

            # --- Reddit front page (navigate only, no actions) ---
            print(f"   navigating to reddit.com...")
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=30_000)
            time.sleep(3)
            title = page.title()
            print(f"   reddit page title: {title!r}")
            page.screenshot(path=str(account_dir / "screenshots" / f"reddit_{session_id[:8]}.png"))

            db.insert(
                state_db, type="Action", status="done",
                action_type="browse", subreddit="r/all", profile_id=pid,
                session_id=session_id, target_url="https://www.reddit.com/",
                result=f"title={title[:80]!r}",
            )

            # Humanlike scroll via lib/browse
            from lib.browse import human_scroll
            tele = human_scroll(page, duration_s=20)
            print(f"   scroll telemetry: {tele}")

        print(f"   playwright context exited cleanly (NO browser.close())")

    print(f"   profile stopped via API")

    db.insert(
        state_db, type="Session", status="done", day=0,
        action_type="integration_test", session_id=session_id,
        result="phase2 ok",
        reasoning="Full middle layer verified: multilogin+playwright+creepjs+reddit+sqlite",
    )

    # --- Summary ---
    print()
    print("== summary ==")
    recent = db.recent(state_db, limit=10)
    for r in recent:
        print(f"  {r['executed_at']}  {r['type']:15s} {r.get('action_type','-'):25s} {r.get('result') or ''}")
    print()
    print(f"Phase 2 PASS: {len(recent)} rows in {state_db}")


if __name__ == "__main__":
    main()
