"""Switch both warmup profiles from `type=mobile` to `type=residential` proxy.
Verifies via start/stop after each switch. Reversible — just re-run with mobile.
"""
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

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"

for label, pid in [
    ("dailyanvil", "91c021fe-b8c7-468a-a718-b69f39663fe9"),
    ("smug_pickle72", "0129cd1d-acba-4c2f-bccd-16492a2881d7"),
]:
    profs = c.search_profiles("", limit=100)
    p = next(x for x in profs if x["id"] == pid)
    proxy = p.get("proxy")
    old_user = proxy["username"]
    new_user = old_user.replace("type-mobile", "type-residential")
    if "type-residential" not in new_user:
        print(f"[{label}] already residential or unexpected username; skipping")
        print(f"  current: {old_user}")
        continue
    c.partial_update(pid, proxy={**proxy, "username": new_user})
    print(f"[{label}] updated to residential")
    print(f"  old: {old_user}")
    print(f"  new: {new_user}")
    time.sleep(2)

    # Verify via start/stop
    r = requests.get(
        f"https://launcher.mlx.yt:45001/api/v2/profile/f/{folder}/p/{pid}/start"
        f"?automation_type=playwright&headless_mode=false",
        headers={"Authorization": f"Bearer {c.token}"},
        timeout=30,
        verify=False,
    )
    if r.status_code == 200:
        try:
            c.stop(pid)
        except Exception:
            pass
        print(f"  verify: HTTP 200 — residential proxy works for {label}")
    else:
        print(f"  verify: HTTP {r.status_code} — {r.text[:200]}")
    print()
