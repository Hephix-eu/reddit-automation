"""Cross-account visibility check: view a comment from a DIFFERENT logged-in
account (its own proxy/IP). A shadow-removed comment is invisible to other
users, so if the viewer sees it, it is genuinely public (not shadow-rejected).

Usage:
  python3 scripts/check_comment_visible.py --viewer salty_crow33 \
      --thread <thread_url> --comment-id oruk277
"""
import argparse
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
sys.path.insert(0, str(REPO))


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--viewer", required=True)
    ap.add_argument("--thread", required=True)
    ap.add_argument("--comment-id", required=True, help="e.g. oruk277 (no t1_)")
    args = ap.parse_args()
    cid = args.comment_id.replace("t1_", "")

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.viewer / ".env")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    config = json.loads((REPO / "accounts" / args.viewer / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[setup] viewer {args.viewer} started on port {port}")
    time.sleep(8)

    rc = 1
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = browser.contexts[0].pages[0]
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=40000)
            time.sleep(3)
            me = page.request.get("https://oauth.reddit.com/api/v1/me", timeout=15000)
            print(f"[auth] viewer /me HTTP {me.status}")

            json_url = args.thread.rstrip("/") + ".json?raw_json=1"
            found = None
            for attempt in range(5):
                try:
                    vj = page.request.get(json_url, timeout=15000).json()

                    def walk(items):
                        for it in items:
                            d = it.get("data", {})
                            if d.get("id") == cid or d.get("name") == f"t1_{cid}":
                                return d
                            reps = d.get("replies")
                            if isinstance(reps, dict):
                                r = walk(reps.get("data", {}).get("children", []))
                                if r:
                                    return r
                        return None
                    found = walk(vj[1]["data"]["children"])
                    if found:
                        break
                except Exception as e:
                    print(f"  attempt {attempt+1} err: {str(e)[:60]}")
                print(f"  attempt {attempt+1}: not seen yet...")
                time.sleep(8)

            if found:
                print(f"\n✅ VISIBLE to {args.viewer}: author={found.get('author')} "
                      f"score={found.get('score')} removed_by={found.get('removed_by_category')}")
                print(f"   body={ (found.get('body') or '')[:100]!r}")
                rc = 0
            else:
                print(f"\n❌ NOT visible to {args.viewer} — likely shadow-removed (or CDN lag).")
                rc = 2
    finally:
        try:
            c.stop(pid)
            print("[cleanup] viewer profile stopped")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
