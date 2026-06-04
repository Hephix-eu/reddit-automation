"""Browse r/dotnet hot via dailyanvil's session (read-only) and dump 5-10
candidate threads with their OP body, score, comment count, and age.
We pick one by hand to draft a comment for.
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

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"]); c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "91c021fe-b8c7-468a-a718-b69f39663fe9"  # dailyanvil

try: c.stop(pid); time.sleep(2)
except Exception: pass
port = c.start(folder, pid)
print(f"[mlx] dailyanvil started on port {port}")
time.sleep(6)

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]
        page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=25000)
        time.sleep(3)
        # Fetch r/dotnet hot via www (oauth.reddit.com without explicit Bearer + User-Agent returns HTML).
        r = page.request.get("https://www.reddit.com/r/dotnet/hot.json?limit=15", timeout=15000)
        print(f"[fetch] /r/dotnet/hot.json → HTTP {r.status} content-type={r.headers.get('content-type','')}")
        body = r.text()
        if r.status != 200 or not body.lstrip().startswith("{"):
            print(f"  body[:300]: {body[:300]}")
            sys.exit("not JSON — bail")
        d = json.loads(body)
        print()
        for i, child in enumerate(d.get("data", {}).get("children", [])[:15]):
            p = child.get("data", {})
            age_h = int((time.time() - p.get("created_utc", 0)) / 3600)
            body_preview = (p.get("selftext", "") or "").replace("\n", " ").strip()[:200]
            print(f"=== #{i+1} ===")
            print(f"  title:     {p.get('title','')[:120]}")
            print(f"  score:     {p.get('score'):>4}  comments: {p.get('num_comments'):>3}  age: {age_h}h  ratio: {p.get('upvote_ratio')}")
            print(f"  permalink: https://www.reddit.com{p.get('permalink','')}")
            print(f"  flair:     {p.get('link_flair_text','-')}  is_self: {p.get('is_self')}  archived: {p.get('archived')}")
            if body_preview:
                print(f"  body:      {body_preview}")
            print()
finally:
    try: c.stop(pid)
    except Exception: pass
    print("[mlx] dailyanvil stopped")
