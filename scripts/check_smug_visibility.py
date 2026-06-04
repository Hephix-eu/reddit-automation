"""Open a FRESH no-cookies context against Reddit (via smug's residential proxy)
and check what an unauthenticated visitor sees of smug's content.

If smug's comments are missing from this view → shadow-ban (account-level)
If smug's r/dotnet comment specifically is missing but r/AskReddit ones show → mod/automod removal in r/dotnet only
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
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"

try:
    c.stop(pid); time.sleep(2)
except Exception: pass
port = c.start(folder, pid)
print(f"[mlx] smug started on port {port}")
time.sleep(6)

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        # Create a NEW context (fresh cookies — unauthenticated view from same residential IP)
        unauth_ctx = b.new_context()
        unauth_page = unauth_ctx.new_page()
        unauth_page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(2)

        for label, url in [
            ("smug's profile (unauth)",       "https://www.reddit.com/user/smug_pickle72/about.json"),
            ("smug's comments list (unauth)", "https://www.reddit.com/user/smug_pickle72/comments.json"),
            ("the r/dotnet thread (unauth)",  "https://www.reddit.com/r/dotnet/comments/1tip8sp.json"),
            ("the AskReddit comment thread",  "https://www.reddit.com/user/smug_pickle72.json"),
        ]:
            r = unauth_page.request.get(url, timeout=20000)
            print(f"\n--- {label} ---")
            print(f"  HTTP {r.status}  content-type={r.headers.get('content-type','')}")
            body = r.text()
            if not (body.lstrip().startswith("{") or body.lstrip().startswith("[")):
                print(f"  body[:200]: {body[:200]}")
                continue
            try:
                d = json.loads(body)
                if "about" in url:
                    ud = (d.get("data") or {})
                    print(f"  name={ud.get('name')} comment_karma={ud.get('comment_karma')} link_karma={ud.get('link_karma')}")
                    print(f"  is_suspended={ud.get('is_suspended')} is_blocked={ud.get('is_blocked')}")
                elif "comments" in url or "smug_pickle72.json" in url:
                    kids = (d.get("data") or {}).get("children", [])
                    print(f"  {len(kids)} comments visible to LOGGED-OUT viewer")
                    for c2 in kids[:8]:
                        cd = c2.get("data", {})
                        print(f"    - {cd.get('subreddit_name_prefixed','')} score={cd.get('score')} ups={cd.get('ups')} body[:60]={(cd.get('body') or '')[:60]!r}")
                elif "r/dotnet" in url:
                    # Find smug's comment in thread tree
                    def walk(items, depth=0):
                        for it in items:
                            cd = it.get("data", {})
                            if cd.get("author") == "smug_pickle72":
                                print(f"    FOUND smug's r/dotnet comment in PUBLIC tree: score={cd.get('score')} body[:60]={(cd.get('body') or '')[:60]!r}")
                                return True
                            replies = cd.get("replies")
                            if isinstance(replies, dict) and walk(replies.get("data", {}).get("children", []), depth+1):
                                return True
                        return False
                    found = walk(d[1]["data"]["children"]) if len(d) > 1 else False
                    if not found:
                        print(f"  ⚠ smug's r/dotnet comment NOT visible to public — confirms it was removed/filtered")
            except Exception as e:
                print(f"  parse err: {e}")
finally:
    try: c.stop(pid)
    except: pass
    print("\n[mlx] smug stopped")
