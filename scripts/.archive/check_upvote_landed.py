"""Verify whether smug_pickle72's Day 9 upvotes registered server-side.
Re-attach to smug's profile, fetch each post's JSON, check `likes` (true=upvoted),
`ups`/`score`, and whether the post is visibly upvoted by us.
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

POSTS = [
    ("r/Blazor",       "https://www.reddit.com/r/Blazor/comments/1tif6i8.json"),
    ("r/ITManagers",   "https://www.reddit.com/r/ITManagers/comments/1tfw8zp.json"),
    ("r/neovim",       "https://www.reddit.com/r/neovim/comments/1tirzct.json"),
    ("r/angular",      "https://www.reddit.com/r/angular/comments/1tilcqo.json"),
    ("r/Passkeys",     "https://www.reddit.com/r/Passkeys/comments/1tff2hk.json"),
]

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"

try:
    c.stop(pid); time.sleep(2)
except Exception:
    pass
port = c.start(folder, pid)
time.sleep(6)

try:
    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]

        for sub, url in POSTS:
            resp = page.request.get(url, timeout=15000)
            print(f"--- {sub} ({url}) ---")
            print(f"  HTTP {resp.status}")
            if resp.status != 200:
                print(f"  body[:200]: {resp.text()[:200]}")
                continue
            try:
                data = json.loads(resp.text())
                p = data[0]["data"]["children"][0]["data"]
                print(f"  title:        {p.get('title','')[:80]}")
                print(f"  score:        {p.get('score')}")
                print(f"  ups:          {p.get('ups')}")
                print(f"  upvote_ratio: {p.get('upvote_ratio')}")
                print(f"  num_comments: {p.get('num_comments')}")
                print(f"  LIKES (us):   {p.get('likes')}   <-- True=we upvoted, None=we didn't, False=we downvoted")
            except Exception as e:
                print(f"  parse err: {e}")

        # Also check smug's own karma + recent history
        print("\n=== smug_pickle72 own karma ===")
        me = page.request.get("https://www.reddit.com/api/me.json", timeout=15000)
        print(f"  HTTP {me.status}")
        if me.status == 200:
            try:
                d = json.loads(me.text())
                u = d.get("data", {})
                print(f"  name={u.get('name')}  total_karma={u.get('total_karma')} comment={u.get('comment_karma')} link={u.get('link_karma')}")
            except Exception as e:
                print(f"  parse err: {e}")
finally:
    try:
        c.stop(pid)
    except Exception:
        pass
