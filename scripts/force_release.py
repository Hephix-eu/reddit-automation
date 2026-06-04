"""Force-release a stuck Multilogin profile for an account.

Called by run-on-host.sh when a stale lock is detected. Two cleanup steps:
  1) Stop any running browser instance via launcher /api/v1/profile/stop/p/{pid}
  2) Release the cloud-side profile lock via DELETE /bpds/profile/lock

Why this exists: when the host wrapper detected a stale lock it used to just
`rm` the file. The cloud-side MLX lock and any zombie Mimic process stayed
behind, and the next session crashed on profile-start (silent rc=1, no SQLite).

Both calls are best-effort: 4xx from "not running / already unlocked" is a
clean state, not a failure. Exit non-zero only on hard auth/network errors
so the wrapper can decide whether to proceed.
"""
import json
import os
import sys
from pathlib import Path

import requests


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: force_release.py <username>", file=sys.stderr)
        return 2

    user = sys.argv[1]
    account_dir = Path(f"/app/accounts/{user}")
    config = json.loads((account_dir / "config.json").read_text())
    profile_id = config["multilogin"]["profile_id"]
    folder_id = config["multilogin"]["folder_id"]

    # Per-account .env carries MULTILOGIN_EMAIL / MULTILOGIN_PASSWORD
    _load_env(account_dir / ".env")
    _load_env(Path("/app/.env"))

    sys.path.insert(0, "/app/.docker-skill")
    from mlx_client import Client, LAUNCH

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()

    rc = 0

    try:
        r = requests.get(
            f"{LAUNCH}/api/v1/profile/stop/p/{profile_id}",
            headers=c._auth(),
            timeout=20,
        )
        print(f"[force_release] launcher stop pid={profile_id[:8]}: HTTP {r.status_code} {r.text[:120]}")
    except requests.RequestException as e:
        print(f"[force_release] launcher stop FAILED: {e}")
        rc = 1

    try:
        r = requests.delete(
            "https://api.multilogin.com/bpds/profile/lock",
            headers=c._auth(),
            json={"profile_id": profile_id, "folder_id": folder_id},
            timeout=15,
        )
        print(f"[force_release] cloud unlock pid={profile_id[:8]}: HTTP {r.status_code} {r.text[:120]}")
    except requests.RequestException as e:
        print(f"[force_release] cloud unlock FAILED: {e}")
        rc = 1

    return rc


if __name__ == "__main__":
    sys.exit(main())
