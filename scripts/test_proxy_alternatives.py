"""Test whether residential / datacenter / other-country proxy types work
via the MLX API. Determines whether 'switching to residential' is a real
escape hatch or requires a separate purchase.
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

import requests
from mlx_client import Client

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "91c021fe-b8c7-468a-a718-b69f39663fe9"

profs = c.search_profiles("", limit=100)
p = next(x for x in profs if x["id"] == pid)
orig_proxy = p.get("proxy")
orig_user = orig_proxy["username"]
print(f"original: {orig_user}\n")


def test_variant(label, new_user):
    new_proxy = {**orig_proxy, "username": new_user}
    c.partial_update(pid, proxy=new_proxy)
    time.sleep(1.5)
    r = requests.get(
        f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder}/p/{pid}/start"
        f"?automation_type=playwright&headless_mode=false",
        headers={"Authorization": f"Bearer {c.token}"},
        timeout=30,
        verify=False,
    )
    print(f"[{label}]")
    print(f"  username: {new_user}")
    print(f"  HTTP {r.status_code}: {r.text[:250]}")
    if r.status_code == 200:
        try:
            c.stop(pid)
        except Exception:
            pass
        print("  -> WORKS")
        return True
    return False


variants = [
    ("lv-residential",            orig_user.replace("type-mobile", "type-residential")),
    ("lv-residential-nofilter",   orig_user.replace("type-mobile", "type-residential").replace("-filter-medium", "")),
    ("lv-datacenter",             orig_user.replace("type-mobile", "type-datacenter")),
    ("us-mobile",                 orig_user.replace("country-lv", "country-us")),
    ("us-residential",            orig_user.replace("country-lv-type-mobile", "country-us-type-residential")),
    ("any-residential",           orig_user.replace("country-lv-type-mobile", "type-residential")),
]

found_working = False
for label, new_user in variants:
    if test_variant(label, new_user):
        found_working = True
        break

# Always restore
c.partial_update(pid, proxy={**orig_proxy, "username": orig_user})
print(f"\nrestored: {orig_user}")

if not found_working:
    print("\nNo variant worked. Either the workspace is fully blocked, or these")
    print("proxy types require a separate purchase from the MLX web UI.")
