"""Multilogin facade for the Reddit warmup agent.

Thin wrapper around the working-with-multilogin skill's Client. We do NOT
duplicate API logic here — that's all in mlx_client.py inside the skill.
This module just:
  - Adds the skill's scripts dir to sys.path so `from mlx_client import Client` works.
  - Provides `make_client()` that reads creds from env.
  - Provides a `session(config)` context manager that opens the configured
    profile, yields (client, profile_id, cdp_port), and stops cleanly on exit.

The agent imports from here. If we need anything the Client doesn't expose,
we extend the SKILL, not this file.
"""

import os
import sys
from contextlib import contextmanager
from pathlib import Path

SKILL_SCRIPTS = Path(r"C:\Users\vilum\Documents\skills\user\working-with-multilogin\scripts")
SKILL_SCRIPTS_LINUX = Path.home() / "skills/user/working-with-multilogin/scripts"

for p in (SKILL_SCRIPTS, SKILL_SCRIPTS_LINUX):
    if p.exists():
        if str(p) not in sys.path:
            sys.path.insert(0, str(p))
        break
else:
    raise RuntimeError(
        f"working-with-multilogin skill not found. Tried: {SKILL_SCRIPTS}, {SKILL_SCRIPTS_LINUX}"
    )

from mlx_client import Client  # noqa: E402  (sys.path manipulation above)


def make_client() -> Client:
    """Create + signin a Multilogin Client using env vars."""
    email = os.environ["MULTILOGIN_EMAIL"]
    password = os.environ["MULTILOGIN_PASSWORD"]
    c = Client(email, password)
    c.signin()
    return c


@contextmanager
def session(config: dict):
    """Open the profile from agent config, yield (client, profile_id, cdp_port).

    Lifecycle:
      1. Signin to Multilogin cloud
      2. Start the profile in playwright automation mode → CDP port
      3. yield → caller drives Playwright over `http://127.0.0.1:{port}`
      4. On exit (success OR exception): stop the profile via launcher API
         (NEVER browser.close() — that corrupts cookies)

    Example:
        from lib.multilogin import session
        with session(config) as (mlx, profile_id, port):
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
                page = browser.contexts[0].pages[0]
                page.goto("https://www.reddit.com/r/dotnet/")
                # ... do warmup work ...
            # exit Playwright `with` naturally — DO NOT call browser.close()
            mlx.save_state(profile_id, day=3, total_karma=87)
        # profile stopped via API after this line
    """
    mlx = make_client()
    pid = config["multilogin"]["profile_id"]
    fid = config["multilogin"]["folder_id"]
    headless = config["multilogin"].get("headless_mode", False)
    automation = config["multilogin"].get("automation_type", "playwright")

    if pid.startswith("TODO_") or fid.startswith("TODO_"):
        raise RuntimeError(
            f"Multilogin profile_id/folder_id not configured (got pid={pid!r}, fid={fid!r}). "
            "Fill in accounts/<username>/config.json before running the agent."
        )

    port = mlx.start(fid, pid, automation_type=automation, headless=headless)
    try:
        yield mlx, pid, port
    finally:
        mlx.stop(pid)
