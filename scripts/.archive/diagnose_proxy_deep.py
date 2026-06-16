"""Deep proxy diagnostic: IP/ASN, Reddit reachability, /api/me.json status,
MLX quota/usage endpoints. One-shot — prints everything we know.
"""
import json
import os
import sys
import time
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break

REPO = Path(__file__).resolve().parent.parent
for envp in (REPO / ".env", REPO / "accounts/dailyanvil/.env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

import requests
from mlx_client import Client
from playwright.sync_api import sync_playwright, TimeoutError as PWT

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "91c021fe-b8c7-468a-a718-b69f39663fe9"

print("=== dailyanvil proxy config ===")
profs = c.search_profiles("", limit=100)
p = next(x for x in profs if x["id"] == pid)
proxy = p.get("proxy") or {}
safe = {**proxy, "password": "<REDACTED>"} if proxy else None
print(json.dumps(safe, indent=2))

print("\n=== MLX usage / billing endpoints ===")
for path in (
    "/proxy/quota", "/proxy/usage", "/proxy/balance", "/billing/usage",
    "/user/usage", "/workspace/usage", "/user/subscription", "/user/balance",
    "/subscription", "/workspace/me", "/proxy", "/proxy/pools",
):
    try:
        r = requests.get(
            f"https://api.multilogin.com{path}",
            headers={"Authorization": f"Bearer {c.token}", "Accept": "application/json"},
            timeout=10,
        )
        snippet = r.text[:200].replace("\n", " ")
        print(f"  {path}: HTTP {r.status_code} | {snippet}")
    except Exception as e:
        print(f"  {path}: {type(e).__name__}: {e}")

print("\n=== try mlx.start (proxy provisioning) ===")
try:
    c.stop(pid); time.sleep(2)
except Exception:
    pass

port = None
try:
    port = c.start(folder, pid)
    print(f"  mlx.start OK, port={port}")
except Exception as e:
    print(f"  mlx.start FAILED: {type(e).__name__}: {str(e)[:300]}")

if port:
    time.sleep(6)
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]

        for label, url in [
            ("ipify",  "https://api.ipify.org?format=json"),
            ("ipinfo", "https://ipinfo.io/json"),
        ]:
            print(f"\n=== {label} ({url}) ===")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                print("  " + page.evaluate("() => document.body.innerText")[:400])
            except PWT as e:
                print(f"  TIMEOUT: {str(e)[:120]}")
            except Exception as e:
                print(f"  {type(e).__name__}: {str(e)[:200]}")

        print("\n=== reddit.com (HTML) ===")
        try:
            r = page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
            print(f"  HTTP {r.status} {r.url}")
        except PWT as e:
            print(f"  TIMEOUT: {str(e)[:120]}")

        print("\n=== /api/me.json (where dailyanvil got 403) ===")
        try:
            resp = page.request.get("https://www.reddit.com/api/me.json", timeout=15000)
            print(f"  HTTP {resp.status}")
            print(f"  body[:400]: {resp.text()[:400]}")
        except Exception as e:
            print(f"  {type(e).__name__}: {str(e)[:200]}")
    try:
        c.stop(pid)
    except Exception:
        pass
