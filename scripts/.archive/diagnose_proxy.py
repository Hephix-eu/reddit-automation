"""One-shot diagnostic: inspect MLX proxy config for both accounts and capture
the exact response when starting dailyanvil's profile.

Run on hephix: python3 scripts/diagnose_proxy.py
"""
import os
import sys
import json
import requests

sys.path.insert(0, "/root/skills/user/working-with-multilogin/scripts")

# Load workspace .env (proxy creds) + smug_pickle72 .env (MLX login)
def load_env(path):
    if not os.path.exists(path):
        return
    for line in open(path).read().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

load_env("/root/reddit-automation/.env")
load_env("/root/reddit-automation/accounts/smug_pickle72/.env")

from mlx_client import Client

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()

profiles = c.search_profiles("", limit=100)
print(f"=== Found {len(profiles)} profiles ===\n")

targets = {
    "smug_pickle72": "0129cd1d-acba-4c2f-bccd-16492a2881d7",
    "dailyanvil":    "91c021fe-b8c7-468a-a718-b69f39663fe9",
}

for name, pid in targets.items():
    p = next((x for x in profiles if x["id"] == pid), None)
    if not p:
        print(f"--- {name} ({pid}): NOT FOUND ---\n")
        continue
    proxy = p.get("parameters", {}).get("proxy") or p.get("proxy")
    flags = p.get("parameters", {}).get("flags") or p.get("flags") or {}
    print(f"--- {name}: {p.get('name')} ({pid}) ---")
    print(f"  folder_id:           {p.get('folder_id')}")
    if proxy:
        # Mask password
        proxy_safe = {**proxy}
        if "password" in proxy_safe:
            proxy_safe["password"] = "<REDACTED>"
        print(f"  proxy:               {json.dumps(proxy_safe)}")
    else:
        print(f"  proxy:               <NONE — profile leaks real IP>")
    print(f"  proxy_masking:       {flags.get('proxy_masking', '?')}")
    print(f"  geolocation_masking: {flags.get('geolocation_masking', '?')}")
    print(f"  timezone_masking:    {flags.get('timezone_masking', '?')}")
    print(f"  webrtc_masking:      {flags.get('webrtc_masking', '?')}")
    print()

# Now hit the start endpoint for dailyanvil and capture exact response
da = next(x for x in profiles if x["id"] == targets["dailyanvil"])
folder = da["folder_id"]
url = (
    f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder}"
    f"/p/{targets['dailyanvil']}/start"
    f"?automation_type=playwright&headless_mode=false"
)
print(f"=== Attempting dailyanvil start (capture exact error) ===")
print(f"  URL: {url}")
try:
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {c.token}"},
        timeout=30,
        verify=False,
    )
    print(f"  HTTP {r.status_code}")
    print(f"  Body: {r.text[:600]}")
    # If it succeeded, stop the profile so we don't leave it running
    if r.status_code == 200:
        print("\n  Start succeeded! Stopping profile to clean up...")
        c.stop(targets["dailyanvil"])
        print("  Stopped.")
except Exception as e:
    print(f"  Exception: {type(e).__name__}: {e}")
