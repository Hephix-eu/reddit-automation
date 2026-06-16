"""Detect shadow-ban / trust state for both warmup accounts.

A shadow-banned Reddit account works perfectly from the user's POV — their UI
shows their posts, their votes "succeed" — but nothing they do is visible to
anyone else. Test: fetch the public user page WITHOUT auth. If logged-in
sees content but unauthenticated sees nothing, that's shadow-ban.
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

import requests

ACCOUNTS = ["smug_pickle72", "dailyanvil"]

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Shadowban-Diagnostic/1.0"

print("=== Unauthenticated fetch of each account's public profile ===")
print("(If shadow-banned, /user/<name>/about.json returns 404 or empty)\n")
for name in ACCOUNTS:
    for path in [f"/user/{name}/about.json", f"/user/{name}/submitted.json", f"/user/{name}/comments.json"]:
        url = f"https://www.reddit.com{path}"
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=15)
            print(f"  {path}")
            print(f"    HTTP {r.status_code}")
            if r.status_code == 200:
                try:
                    d = r.json()
                    if "/about.json" in path:
                        u = d.get("data", {})
                        print(f"    name={u.get('name')} comment_karma={u.get('comment_karma')} link_karma={u.get('link_karma')} created={u.get('created_utc')} verified={u.get('verified')} has_verified_email={u.get('has_verified_email')}")
                    else:
                        kids = (d.get("data") or {}).get("children", [])
                        print(f"    {len(kids)} items returned")
                        for c in kids[:3]:
                            pd = c.get("data", {})
                            print(f"      - {pd.get('subreddit_name_prefixed','')} | score={pd.get('score')} | ups={pd.get('ups')} | created={pd.get('created_utc')}")
                except Exception as e:
                    print(f"    parse err: {e}")
                    print(f"    body[:200]: {r.text[:200]}")
            else:
                print(f"    body[:200]: {r.text[:200]}")
        except Exception as e:
            print(f"  {path}: {type(e).__name__}: {e}")
    print()
