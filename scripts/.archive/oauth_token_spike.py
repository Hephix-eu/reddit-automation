"""Spike: can we extract a usable OAuth bearer token from smug_pickle72's
logged-in Reddit session? Probes localStorage, cookies, and request inspection.
"""
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
for envp in (REPO / ".env", REPO / "accounts/smug_pickle72/.env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from mlx_client import Client
from playwright.sync_api import sync_playwright

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"  # smug

try:
    c.stop(pid); time.sleep(2)
except Exception:
    pass
port = c.start(folder, pid)
print(f"[setup] smug profile started on port {port}")
time.sleep(6)

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]

        # Make sure we're on Reddit so localStorage scope matches
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(4)

        # ===== localStorage keys =====
        print("\n=== localStorage keys (Reddit origin) ===")
        keys = page.evaluate("() => Object.keys(localStorage)")
        for k in keys:
            v = page.evaluate(f"() => localStorage.getItem({json.dumps(k)}) || ''")
            v_short = v[:120] if isinstance(v, str) else str(v)[:120]
            print(f"  {k}  ({len(v) if v else 0} chars)  preview: {v_short!r}")

        # ===== sessionStorage keys =====
        print("\n=== sessionStorage keys ===")
        keys = page.evaluate("() => Object.keys(sessionStorage)")
        for k in keys:
            v = page.evaluate(f"() => sessionStorage.getItem({json.dumps(k)}) || ''")
            print(f"  {k}  ({len(v) if v else 0} chars)")

        # ===== cookies =====
        print("\n=== cookies (names + auth-related) ===")
        cookies = b.contexts[0].cookies()
        for ck in cookies:
            if any(t in ck["name"].lower() for t in ("session", "token", "auth", "loid", "edgebucket", "reddit", "csrf", "tt_")):
                v = ck["value"]
                v_short = v[:60] + "..." if len(v) > 60 else v
                print(f"  {ck['name']:30} = {v_short}  (domain={ck['domain']})")

        # ===== look for bearer-style strings in localStorage values =====
        print("\n=== bearer-token-shaped values (24+ char base64-ish) ===")
        all_ls = page.evaluate("""() => {
          const out = {};
          for (const k of Object.keys(localStorage)) out[k] = localStorage.getItem(k);
          return out;
        }""")
        for k, v in all_ls.items():
            if not isinstance(v, str):
                continue
            # Look for tokens
            for m in re.finditer(r'(?:"accessToken"|"access_token"|"token")\s*:\s*"([A-Za-z0-9._\-]{20,})"', v):
                print(f"  {k}: token-shaped value found: {m.group(1)[:40]}...")
            # Look for nested JSON
            try:
                obj = json.loads(v)
                def walk(d, path=""):
                    if isinstance(d, dict):
                        for kk, vv in d.items():
                            if isinstance(vv, str) and any(t in kk.lower() for t in ("token", "access")) and 20 < len(vv) < 500:
                                print(f"  {k}{path}/{kk} = {vv[:50]}...")
                            walk(vv, f"{path}/{kk}")
                    elif isinstance(d, list):
                        for i, vv in enumerate(d):
                            walk(vv, f"{path}[{i}]")
                walk(obj)
            except Exception:
                pass

        # ===== try fetching /api/v1/me via the page's own fetch (which uses cookies) =====
        print("\n=== /api/v1/me via page.request (uses session cookies) ===")
        for url in [
            "https://oauth.reddit.com/api/v1/me",
            "https://www.reddit.com/api/v1/me",
            "https://www.reddit.com/api/me.json",
        ]:
            try:
                r = page.request.get(url, timeout=15000)
                body = r.text()[:200]
                print(f"  {url}")
                print(f"    HTTP {r.status} body: {body}")
            except Exception as e:
                print(f"  {url}: {type(e).__name__}: {str(e)[:200]}")

        # ===== try observing what the page sends when it calls Reddit =====
        # Trigger a page action that makes an authenticated API call (refresh karma)
        print("\n=== capturing outbound request to oauth.reddit.com... ===")
        captured = {"req": None}
        def on_req(req):
            if "oauth.reddit.com" in req.url or "/api/" in req.url:
                if captured["req"] is None:
                    captured["req"] = {
                        "url": req.url,
                        "method": req.method,
                        "headers": dict(req.headers),
                    }
        page.on("request", on_req)

        # Force a karma refresh by reloading
        try:
            page.reload(wait_until="domcontentloaded", timeout=15000)
            time.sleep(5)
        except Exception:
            pass

        if captured["req"]:
            print(f"  captured: {captured['req']['method']} {captured['req']['url']}")
            auth_h = captured["req"]["headers"].get("authorization", "")
            if auth_h:
                print(f"  AUTHORIZATION HEADER: {auth_h[:80]}...")
            else:
                print(f"  no Authorization header. Other interesting headers:")
                for h in ("x-reddit-loid", "x-reddit-session", "cookie"):
                    v = captured["req"]["headers"].get(h, "")
                    print(f"    {h}: {(v[:80] + '...') if len(v) > 80 else v}")
        else:
            print("  no oauth.reddit.com request observed")

finally:
    try:
        c.stop(pid)
    except Exception:
        pass
    print("\n[cleanup] profile stopped")
