"""Dump MLX profile state for a given account — fingerprint, proxy, masking,
notes, current launcher status."""
import json, os, sys, time
from pathlib import Path
for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p); break
REPO = Path("/app") if Path("/app/accounts").exists() else Path(__file__).resolve().parent.parent
for envp in (REPO / ".env", REPO / "accounts/crispygopher_9/.env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

import requests
from mlx_client import Client

USER = sys.argv[1] if len(sys.argv) > 1 else "crispygopher_9"
cfg = json.load(open(f"/app/accounts/{USER}/config.json"))
pid = cfg["multilogin"]["profile_id"]
folder = cfg["multilogin"]["folder_id"]
print(f"=== profile {USER} (pid={pid}) ===\n")

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()

# 1. Get profile metadata from cloud API
p = next((x for x in c.search_profiles("", limit=100) if x["id"] == pid), None)
if not p:
    print("profile not found")
    sys.exit(1)

print("--- top-level fields ---")
for k in ["id", "name", "folder_id", "browser_type", "os_type", "created_at", "updated_at", "status"]:
    print(f"  {k}: {p.get(k)}")

print("\n--- proxy config ---")
proxy = p.get("proxy") or {}
safe = {**proxy, "password": "<REDACTED>"} if proxy else None
print(json.dumps(safe, indent=2))

print("\n--- notes (state) ---")
notes_raw = p.get("notes") or ""
if notes_raw.strip().startswith("{"):
    try:
        notes = json.loads(notes_raw)
        print(json.dumps(notes, indent=2))
    except Exception:
        print(notes_raw[:400])
else:
    print(repr(notes_raw)[:400])

print("\n--- launcher current status (running / stopped / locked) ---")
r = requests.get(f"https://launcher.mlx.yt:45001/api/v1/profile/statuses",
                 headers={"Authorization": f"Bearer {c.token}"}, timeout=15, verify=False)
print(f"  HTTP {r.status_code}")
if r.status_code == 200:
    statuses = r.json().get("data") or {}
    found = False
    for k, v in statuses.items() if isinstance(statuses, dict) else []:
        if pid in str(k) or (isinstance(v, dict) and pid in str(v)):
            print(f"  {k}: {v}")
            found = True
    if not found:
        print(f"  raw: {r.text[:300]}")
else:
    print(f"  body: {r.text[:200]}")

print("\n--- profile parameters (flags / fingerprint hints) ---")
params = p.get("parameters") or {}
flags = params.get("flags") or {}
print(f"  flags (relevant):")
for k in ["proxy_masking", "geolocation_masking", "timezone_masking", "webrtc_masking",
         "navigator_masking", "audio_masking", "fonts_masking"]:
    print(f"    {k}: {flags.get(k)}")
print(f"  navigator: {(params.get('navigator') or {}).get('user_agent','')[:100]}")
