"""Probe a single account's IP. Pass account name as argv[1]."""
import os, sys, json, time
from pathlib import Path

sys.path.insert(0, "/app")
from lib import multilogin

def load_env(p):
    if not os.path.exists(p): return
    for line in open(p).read().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_env("/app/.env")
load_env("/app/.env.acctfarm")

user = sys.argv[1]
d = Path("/app/accounts") / user
load_env(str(d / ".env"))
cfg = json.loads((d / "config.json").read_text())

with multilogin.session(cfg) as (mlx, profile_id, cdp_port):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://ipinfo.io/json", timeout=30000, wait_until="domcontentloaded")
        body = page.evaluate("() => document.body.innerText")
        info = json.loads(body)
        print(f"{user}: ip={info.get('ip')}  country={info.get('country')}  org={(info.get('org') or '')[:80]}")
