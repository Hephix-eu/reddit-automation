"""Check if dailyanvil is also shadow-banned — view her from smug's session
(smug is banned, but Reddit's web still answers smug's requests for OTHER users'
profiles normally)."""
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
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"
try: c.stop(pid); time.sleep(2)
except: pass
port = c.start(folder, pid)
print(f"[mlx] smug started on port {port}")
time.sleep(6)
try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
        time.sleep(3)
        for label, url in [
            ("dailyanvil about (from smug)",    "https://www.reddit.com/user/dailyanvil/about.json"),
            ("dailyanvil comments (from smug)", "https://www.reddit.com/user/dailyanvil/comments.json"),
            ("dailyanvil submitted (from smug)","https://www.reddit.com/user/dailyanvil/submitted.json"),
        ]:
            r = page.request.get(url, timeout=15000)
            print(f"\n--- {label} ---")
            print(f"  HTTP {r.status}")
            if r.status == 200:
                try:
                    d = json.loads(r.text())
                    if "about" in url:
                        ud = d.get("data") or {}
                        print(f"  name={ud.get('name')} comment_karma={ud.get('comment_karma')} link_karma={ud.get('link_karma')} is_suspended={ud.get('is_suspended')}")
                    else:
                        kids = (d.get("data") or {}).get("children", [])
                        print(f"  {len(kids)} items visible")
                        for c2 in kids[:5]:
                            cd = c2.get("data", {})
                            print(f"    - {cd.get('subreddit_name_prefixed','?')} score={cd.get('score')} body/title[:60]={((cd.get('body') or cd.get('title') or ''))[:60]!r}")
                except Exception as e:
                    print(f"  parse err: {e}; body[:200]: {r.text()[:200]}")
            else:
                print(f"  body[:200]: {r.text()[:200]}")
finally:
    try: c.stop(pid)
    except: pass
    print("\n[mlx] smug stopped")
