"""Check dailyanvil's actual Reddit-side comments via smug viewer."""
import json, os, sys, time
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p); break

REPO = Path(__file__).resolve().parent.parent
for envp in (REPO / ".env", REPO / "accounts/smug_pickle72/.env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

from mlx_client import Client
from playwright.sync_api import sync_playwright

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"]); c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"  # smug = viewer
try: c.stop(pid); time.sleep(2)
except: pass
port = c.start(folder, pid); time.sleep(6)
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    page = b.contexts[0].pages[0]
    page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000); time.sleep(3)

    r = page.request.get("https://www.reddit.com/user/dailyanvil/about.json", timeout=15000)
    if r.status == 200:
        ud = (r.json().get("data") or {})
        print(f"dailyanvil karma: comment={ud.get('comment_karma')} link={ud.get('link_karma')} total={ud.get('total_karma')}")
        print(f"is_suspended={ud.get('is_suspended')}")

    r = page.request.get("https://www.reddit.com/user/dailyanvil/comments.json", timeout=15000)
    if r.status == 200:
        kids = (r.json().get("data") or {}).get("children", [])
        print(f"\npublic comments visible: {len(kids)}")
        for c2 in kids:
            cd = c2.get("data", {})
            print(f"  - {cd.get('subreddit_name_prefixed','?')} id={cd.get('name','')}")
            print(f"    score={cd.get('score')} ups={cd.get('ups')} created={cd.get('created_utc')}")
            print(f"    permalink: https://www.reddit.com{cd.get('permalink','')}")
            print(f"    body: {(cd.get('body') or '')[:200]!r}")
            print()
c.stop(pid)
