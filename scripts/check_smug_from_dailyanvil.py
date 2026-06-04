"""View smug's content through dailyanvil's logged-in session.
If dailyanvil sees smug's posts/comments → smug is NOT shadowbanned.
If dailyanvil sees smug's profile but NOT the r/dotnet comment → mod removal in r/dotnet.
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

from mlx_client import Client
from playwright.sync_api import sync_playwright

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "91c021fe-b8c7-468a-a718-b69f39663fe9"  # dailyanvil

try: c.stop(pid); time.sleep(2)
except: pass
port = c.start(folder, pid)
print(f"[mlx] dailyanvil started on port {port}")
time.sleep(6)

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(3)

        # Verify dailyanvil is logged in
        me = page.request.get("https://oauth.reddit.com/api/v1/me", timeout=15000)
        print(f"[auth] /api/v1/me HTTP {me.status}")
        if me.status == 200:
            md = me.json()
            print(f"[auth] logged in as: {md.get('name')!r} karma={md.get('total_karma')}")

        for label, url in [
            ("smug's about (via dailyanvil)",     "https://www.reddit.com/user/smug_pickle72/about.json"),
            ("smug's comments (via dailyanvil)",  "https://www.reddit.com/user/smug_pickle72/comments.json"),
            ("r/dotnet thread (via dailyanvil)",  "https://www.reddit.com/r/dotnet/comments/1tip8sp.json"),
        ]:
            r = page.request.get(url, timeout=15000)
            print(f"\n--- {label} ---")
            print(f"  HTTP {r.status}")
            body = r.text()
            if not body.lstrip().startswith(("{", "[")):
                print(f"  body[:160]: {body[:160]}")
                continue
            try:
                d = json.loads(body)
                if "about" in url:
                    ud = d.get("data") or {}
                    print(f"  found smug profile: name={ud.get('name')} comment_karma={ud.get('comment_karma')} link_karma={ud.get('link_karma')} created_utc={ud.get('created_utc')} is_suspended={ud.get('is_suspended')}")
                elif "/user/" in url and "comments" in url:
                    kids = (d.get("data") or {}).get("children", [])
                    print(f"  {len(kids)} smug comments visible to dailyanvil")
                    for c2 in kids:
                        cd = c2.get("data", {})
                        print(f"    - {cd.get('subreddit_name_prefixed','?')} score={cd.get('score')} ups={cd.get('ups')} body[:60]={(cd.get('body') or '')[:60]!r}")
                elif "r/dotnet" in url:
                    def walk(items, depth=0):
                        for it in items:
                            cd = it.get("data", {})
                            if cd.get("author") == "smug_pickle72":
                                print(f"    smug's r/dotnet comment VISIBLE to dailyanvil:")
                                print(f"      score={cd.get('score')} ups={cd.get('ups')} body[:80]={(cd.get('body') or '')[:80]!r}")
                                return True
                            replies = cd.get("replies")
                            if isinstance(replies, dict) and walk(replies.get("data", {}).get("children", []), depth+1):
                                return True
                        return False
                    found = walk(d[1]["data"]["children"]) if len(d) > 1 else False
                    if not found:
                        print(f"  ⚠ smug's r/dotnet comment NOT visible in public thread (dailyanvil's view)")
            except Exception as e:
                print(f"  parse err: {e}")
                print(f"  body[:200]: {body[:200]}")
finally:
    try: c.stop(pid)
    except: pass
    print("\n[mlx] dailyanvil stopped")
