"""Rotate the sticky-session token (`sid-XXXXXXXX`) in a Multilogin profile's
proxy username, then verify by starting+stopping the profile via the launcher.

The sid binds the profile to a specific upstream mobile IP. When that IP rotates
out of the provider's pool, the proxy backend returns GET_PROXY_CONNECTION_IP_ERROR.
Rotating the sid asks the provider for a fresh sticky binding — same account,
same country, same filter, new IP.

Usage:
    python3 scripts/rotate_proxy_sid.py <profile_id>          # rotate + verify
    python3 scripts/rotate_proxy_sid.py <profile_id> --dry    # show what would change

Run on hephix (needs to hit launcher.mlx.yt:45001).
"""
import json
import os
import random
import re
import string
import sys
import time

import requests

sys.path.insert(0, "/root/skills/user/working-with-multilogin/scripts")


def load_env(path: str) -> None:
    if not os.path.exists(path):
        return
    for line in open(path).read().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_env("/root/reddit-automation/.env")
load_env("/root/reddit-automation/accounts/smug_pickle72/.env")

from mlx_client import Client


def gen_sid(n: int = 8) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(random.choices(alphabet, k=n))


def rotate(profile_id: str, dry: bool = False) -> int:
    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()

    # Find profile (search returns all when query is empty)
    profiles = c.search_profiles("", limit=100)
    p = next((x for x in profiles if x["id"] == profile_id), None)
    if not p:
        print(f"profile {profile_id} not found")
        return 2

    # Profiles from /profile/search return a flat dict — proxy at top level.
    # /profile/update wants {profile_id, parameters: {proxy, flags, ...}}.
    # We need to assemble the parameters block from the search result.
    proxy = p.get("proxy") or p.get("parameters", {}).get("proxy")
    if not proxy:
        print(f"profile {profile_id} has no proxy attached — nothing to rotate")
        return 3

    old_username = proxy.get("username", "")
    m = re.search(r"sid-([A-Za-z0-9]+)", old_username)
    if not m:
        print(f"proxy.username has no sid- segment; raw: {old_username[:80]}")
        return 4
    old_sid = m.group(1)
    new_sid = gen_sid(len(old_sid))
    new_username = old_username.replace(f"sid-{old_sid}", f"sid-{new_sid}")

    print(f"=== Rotating sid for profile {profile_id} ===")
    print(f"  name:    {p.get('name')}")
    print(f"  old sid: sid-{old_sid}")
    print(f"  new sid: sid-{new_sid}")
    if dry:
        print("  (dry run — not committing)")
        return 0

    new_proxy = {**proxy, "username": new_username}

    # partial_update preserves all unspecified fields — only `proxy` will be touched.
    # This avoids the /profile/update requirement to send the full parameters block
    # (which needs `fingerprint`, hard to reconstruct from the search response).
    body = {"profile_id": profile_id, "proxy": new_proxy}
    r = requests.post(
        "https://api.multilogin.com/profile/partial_update",
        json=body,
        headers={
            "Authorization": f"Bearer {c.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        timeout=30,
    )
    if r.status_code not in (200, 201):
        print(f"  partial_update FAILED: HTTP {r.status_code}: {r.text[:400]}")
        return 5
    print(f"  partial_update OK: HTTP {r.status_code}")

    # Verify by starting via the launcher
    print(f"=== Verifying by starting profile via launcher ===")
    folder_id = p.get("folder_id")
    time.sleep(2)
    start_url = (
        f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder_id}/p/{profile_id}/start"
        f"?automation_type=playwright&headless_mode=false"
    )
    sr = requests.get(
        start_url,
        headers={"Authorization": f"Bearer {c.token}"},
        timeout=60,
        verify=False,
    )
    print(f"  start HTTP {sr.status_code}")
    print(f"  body: {sr.text[:400]}")

    if sr.status_code == 200:
        print(f"  ✅ start succeeded — sid rotation worked. Stopping profile...")
        try:
            c.stop(profile_id)
            print(f"  stopped cleanly")
        except Exception as e:
            print(f"  stop call raised: {e}")
        return 0
    else:
        print(f"  ❌ start failed even with new sid. Either the proxy provider is "
              f"completely out of IPs for this filter, or another setting is wrong.")
        return 6


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: rotate_proxy_sid.py <profile_id> [--dry]")
    pid = sys.argv[1]
    dry = "--dry" in sys.argv
    sys.exit(rotate(pid, dry))
