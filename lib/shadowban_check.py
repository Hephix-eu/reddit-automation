"""Detect shadow-ban by viewing a target account through ANOTHER logged-in
account's session. Cannot be done from a single account — Reddit shows the
account-holder its own content even when shadow-banned.

Returns dict with `status` in {'healthy', 'shadowbanned', 'suspended', 'unknown'}.
"""
import json
import os
import sys
import time
from pathlib import Path

FOLDER = "33b31a69-2819-43c6-811a-2bebf5c09999"


def check(target_username: str, viewer_pid: str, mlx_client) -> dict:
    """Run a shadow-ban probe.

    Args:
        target_username: the user we want to verify (e.g. "smug_pickle72")
        viewer_pid: MLX profile id of a DIFFERENT account used as the viewer
        mlx_client: already-signed-in mlx_client.Client

    Returns:
        {"status": "healthy"|"shadowbanned"|"suspended"|"unknown", "raw": {...}}

    Side effects: starts + stops viewer_pid's profile.
    """
    from playwright.sync_api import sync_playwright

    try:
        mlx_client.stop(viewer_pid); time.sleep(2)
    except Exception:
        pass

    port = mlx_client.start(FOLDER, viewer_pid)
    time.sleep(6)

    result = {"status": "unknown", "raw": {}}
    try:
        with sync_playwright() as pw:
            b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            page = b.contexts[0].pages[0]
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=20000)
            time.sleep(3)
            r = page.request.get(
                f"https://www.reddit.com/user/{target_username}/about.json",
                timeout=15000,
            )
            result["raw"]["http_status"] = r.status
            body = r.text()
            if r.status == 404:
                result["status"] = "shadowbanned"
                result["raw"]["body_preview"] = body[:200]
            elif r.status == 200 and body.lstrip().startswith("{"):
                d = json.loads(body)
                ud = d.get("data") or {}
                result["raw"]["name"] = ud.get("name")
                result["raw"]["is_suspended"] = ud.get("is_suspended")
                result["raw"]["comment_karma"] = ud.get("comment_karma")
                result["raw"]["link_karma"] = ud.get("link_karma")
                if ud.get("is_suspended"):
                    result["status"] = "suspended"
                elif ud.get("name"):
                    result["status"] = "healthy"
            else:
                result["raw"]["body_preview"] = body[:200]
                # 403/HTML page = CDN/cloudflare block, not account-level signal
                result["status"] = "unknown"

            # While the viewer session is already up, also capture the target's
            # PUBLICLY VISIBLE comments. This is the only authoritative way to
            # know whether a comment we posted is visible to OTHERS — the posting
            # account always sees its own comments, even shadow-rejected ones.
            # Additive + best-effort: a failure here never changes ban status.
            if result["status"] == "healthy":
                try:
                    cr = page.request.get(
                        f"https://www.reddit.com/user/{target_username}/comments.json?limit=100",
                        timeout=15000,
                    )
                    if cr.status == 200 and cr.text().lstrip().startswith("{"):
                        kids = (cr.json().get("data") or {}).get("children", [])
                        vis = []
                        for k in kids:
                            cd = k.get("data") or {}
                            vis.append({
                                "id": cd.get("name") or cd.get("id"),
                                "body": (cd.get("body") or "")[:300],
                                "permalink": cd.get("permalink"),
                                "subreddit": cd.get("subreddit"),
                                "score": cd.get("score"),
                                "created_utc": cd.get("created_utc"),
                            })
                        result["visible_comments"] = vis
                        result["raw"]["visible_comment_count"] = len(vis)
                    else:
                        result["raw"]["comments_http"] = cr.status
                except Exception as e:
                    result["raw"]["comments_error"] = f"{type(e).__name__}: {e}"
    except Exception as e:
        result["raw"]["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            mlx_client.stop(viewer_pid)
        except Exception:
            pass

    return result
