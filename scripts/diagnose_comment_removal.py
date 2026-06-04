"""Diagnose what happened to smug's r/dotnet comment.

Three possible removals, each with different recovery:
  (A) AutoModerator: subreddit AutoMod removed it — most common for low-karma
      accounts in subs with karma/age requirements. Comment exists, smug sees it,
      logged-out users see nothing. Recovery: post in less-gated subs.
  (B) Mod manual removal: a human mod removed it. Same external symptoms as (A).
      Recovery: don't return to this sub for a while.
  (C) Admin shadowban: Reddit-side account-level ban. NOTHING smug does shows up.
      Profile unauth = empty / page not found.
  (D) Account suspended: profile shows "suspended" message.

Probes:
  1. unauth fetch of comment by id (via api/info)
  2. unauth fetch of smug's user profile
  3. unauth fetch of the thread to see if comment appears in tree
  4. smug's own session viewing the comment (still visible to her?)
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
from mlx_client import Client
from playwright.sync_api import sync_playwright

COMMENT_ID = "t1_on7xr86"
COMMENT_URL = "https://www.reddit.com/r/dotnet/comments/1tip8sp/does_anybody_do_the_addapplicationservices_thing/on7xr86/"
THREAD_JSON = "https://www.reddit.com/r/dotnet/comments/1tip8sp.json"
USER = "smug_pickle72"

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0 Safari/537.36"

print("=" * 70)
print("PHASE 1 — unauth probes (from hephix host directly, no proxy)")
print("=" * 70)

# Direct fetches from hephix (no auth, no proxy) — sees what the public sees
for label, url in [
    ("comment by id (api/info)", f"https://www.reddit.com/api/info.json?id={COMMENT_ID}"),
    (f"user profile {USER}",      f"https://www.reddit.com/user/{USER}/about.json"),
    (f"user's submitted",         f"https://www.reddit.com/user/{USER}/submitted.json"),
    (f"user's comments",          f"https://www.reddit.com/user/{USER}/comments.json"),
    ("thread tree",               THREAD_JSON),
]:
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=15)
        print(f"\n--- {label} ---")
        print(f"  URL: {url}")
        print(f"  HTTP {r.status_code}  content-type={r.headers.get('content-type','')}")
        body = r.text
        if not body.lstrip().startswith("{") and not body.lstrip().startswith("["):
            print(f"  body[:200]: {body[:200]}")
            continue
        try:
            d = json.loads(body)
            if label.startswith("comment by id"):
                kids = (d.get("data") or {}).get("children", [])
                if not kids:
                    print(f"  ⚠ NO children — comment not visible unauthenticated")
                for c in kids:
                    cd = c.get("data", {})
                    print(f"  found comment: author={cd.get('author')!r} body[:60]={(cd.get('body') or '')[:60]!r}")
                    print(f"    score={cd.get('score')} ups={cd.get('ups')} removed_by_category={cd.get('removed_by_category')!r}")
                    print(f"    banned_by={cd.get('banned_by')!r} mod_removed={cd.get('mod_removed')} approved={cd.get('approved_at_utc')}")
            elif "user profile" in label:
                ud = d.get("data") or {}
                print(f"  name={ud.get('name')} comment_karma={ud.get('comment_karma')} link_karma={ud.get('link_karma')} created_utc={ud.get('created_utc')} is_suspended={ud.get('is_suspended')}")
            elif "submitted" in label or "comments" in label:
                kids = (d.get("data") or {}).get("children", [])
                print(f"  {len(kids)} items publicly visible")
                for c in kids[:5]:
                    cd = c.get("data", {})
                    print(f"    - {cd.get('subreddit_name_prefixed','')}  score={cd.get('score')}  text[:50]={((cd.get('body') or cd.get('title') or '')[:50])!r}")
            elif "thread tree" in label:
                # Find smug's comment in tree
                def walk(items, depth=0):
                    for it in items:
                        cd = it.get("data", {})
                        if cd.get("author") == USER:
                            print(f"    FOUND smug's comment in thread tree: {cd.get('name')} score={cd.get('score')} body[:50]={(cd.get('body') or '')[:50]!r}")
                            return True
                        replies = cd.get("replies")
                        if isinstance(replies, dict) and walk(replies.get("data", {}).get("children", []), depth+1):
                            return True
                    return False
                found = walk(d[1]["data"]["children"]) if len(d) > 1 else False
                if not found:
                    print(f"  ⚠ smug's comment NOT in thread tree (unauthenticated view)")
        except Exception as e:
            print(f"  parse err: {e}")
    except Exception as e:
        print(f"  {label}: {type(e).__name__}: {e}")

print()
print("=" * 70)
print("PHASE 2 — smug's own logged-in view (via Mimic)")
print("=" * 70)

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"

try:
    c.stop(pid); time.sleep(2)
except Exception:
    pass
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
            ("smug's own comment (logged in)",     f"https://www.reddit.com/api/info.json?id={COMMENT_ID}"),
            ("smug's user/me/comments (logged in)", f"https://www.reddit.com/user/{USER}/comments.json"),
        ]:
            r = page.request.get(url, timeout=15000)
            print(f"\n--- {label} ---")
            print(f"  HTTP {r.status}")
            try:
                d = r.json()
                if "comments" in label and "submitted" not in label:
                    kids = (d.get("data") or {}).get("children", [])
                    print(f"  {len(kids)} comments visible to smug herself")
                    for c2 in kids[:5]:
                        cd = c2.get("data", {})
                        print(f"    - {cd.get('subreddit_name_prefixed','')} score={cd.get('score')} body[:60]={(cd.get('body') or '')[:60]!r}")
                else:
                    kids = (d.get("data") or {}).get("children", [])
                    if not kids:
                        print(f"  ⚠ comment not found by id even from smug's session")
                    for c2 in kids:
                        cd = c2.get("data", {})
                        print(f"  found: author={cd.get('author')} score={cd.get('score')} body[:60]={(cd.get('body') or '')[:60]!r}")
                        print(f"    removed_by_category={cd.get('removed_by_category')!r}  banned_by={cd.get('banned_by')!r}")
            except Exception as e:
                print(f"  parse err: {e}")
                print(f"  body[:200]: {r.text()[:200]}")

        # ===== Test posting to r/ShadowBan (the bot will reply if account is shadowbanned) =====
        # Actually, just check r/dotnet's automod sticky / sub rules — what karma/age requirement is set?
        rules_resp = page.request.get("https://www.reddit.com/r/dotnet/about/rules.json", timeout=15000)
        print(f"\n--- r/dotnet rules ---")
        if rules_resp.status == 200:
            rd = rules_resp.json()
            for rule in rd.get("rules", [])[:10]:
                desc = (rule.get('description') or rule.get('short_name') or '').replace("\n", " ")
                print(f"  - {rule.get('short_name','?')}: {desc[:160]}")
finally:
    try:
        c.stop(pid)
        print("\n[mlx] smug stopped")
    except Exception:
        pass
