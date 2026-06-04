"""One-shot non-interactive onboarder for vast_prawn (the replacement for smug).

Does:
  1. Create a fresh MLX profile bound to residential LV proxy (mobile is depleted)
  2. Create accounts/vast_prawn/ + config.json + per-account .env (with Reddit creds)
  3. Skip cron registration — first session is manual, then host cron picks up

Run ONCE inside the redditagent-image container:
    docker run --rm --network "container:multilogin" \
      -v /root/reddit-automation/accounts:/app/accounts \
      -v /root/reddit-automation/.env:/app/.env:ro \
      -v /root/reddit-automation/scripts:/app/scripts:ro \
      redditagent-image \
      python3 /app/scripts/onboard_vast_prawn.py
"""
import json
import os
import sys
import time
from pathlib import Path

# Paths set up for in-container execution
REPO = Path("/app") if Path("/app/accounts").exists() else Path(__file__).resolve().parent.parent
ACCOUNTS = REPO / "accounts"
sys.path.insert(0, str(REPO))
sys.path.insert(0, "/root/skills/user/working-with-multilogin/scripts")

# === Reddit account credentials (provided by user) ===
REDDIT_USERNAME = "vast_prawn"
REDDIT_EMAIL    = "jon.stewart43@janeholt.nl"
REDDIT_PASSWORD = "L{m\\o\\!7\"s#qRN~c"

ANCHOR_SUB = "r/AskReddit"  # per new strategy: Day 1-3 lurk in r/AskReddit

# Load workspace .env (proxy creds) + dailyanvil's .env (MLX login creds — shared)
for envp in (REPO / ".env", ACCOUNTS / "dailyanvil" / ".env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from mlx_client import Client, DEFAULT_FLAGS_DESKTOP

account_dir = ACCOUNTS / REDDIT_USERNAME
if account_dir.exists():
    print(f"[error] {account_dir} already exists. Refusing to overwrite.")
    sys.exit(1)

mlx = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
mlx.signin()
print(f"[mlx] signed in")

folders = mlx.folders()
folder = folders[0]
folder_id = folder["folder_id"]
print(f"[mlx] using folder: {folder['name']} ({folder_id})")

# Residential proxy (mobile is exhausted/burned right now)
proxy = {
    "host": os.environ.get("MULTILOGIN_PROXY_HOST", "gate.multilogin.com"),
    "port": int(os.environ.get("MULTILOGIN_PROXY_PORT", "1080")),
    "type": os.environ.get("MULTILOGIN_PROXY_TYPE", "socks5"),
    "username": os.environ["MULTILOGIN_PROXY_USERNAME"].replace("type-mobile", "type-residential"),
    "password": os.environ["MULTILOGIN_PROXY_PASSWORD"],
}
print(f"[proxy] {proxy['host']}:{proxy['port']} ({proxy['type']}) — country=lv type=residential")

# Per cli.py existing pattern: natural tz/locale derived from proxy IP, masked geo
flags = {**DEFAULT_FLAGS_DESKTOP, "geolocation_masking": "mask"}

profile_name = f"reddit-{REDDIT_USERNAME}-desktop"
print(f"[mlx] creating profile {profile_name!r}...")
profile_id = mlx.create_profile(
    folder_id=folder_id,
    name=profile_name,
    proxy=proxy,
    flags=flags,
    os_type="windows",
)
print(f"[mlx] ✅ profile_id = {profile_id}")

# Bootstrap account folder + files
account_dir.mkdir(parents=True)
(account_dir / "recordings").mkdir()
(account_dir / "screenshots").mkdir()

# Generate config.json from template
template_path = REPO / "config.template.json"
config_text = template_path.read_text()
replacements = {
    "{{REDDIT_USERNAME}}": REDDIT_USERNAME,
    "{{ACCOUNT_DIR}}": str(account_dir).replace("\\", "\\\\"),
    "{{MULTILOGIN_PROFILE_ID}}": profile_id,
    "{{MULTILOGIN_FOLDER_ID}}": folder_id,
    "{{MULTILOGIN_PROFILE_NAME}}": profile_name,
    "{{ANCHOR_SUB}}": ANCHOR_SUB,
}
for k, v in replacements.items():
    config_text = config_text.replace(k, v)

# Set start_date to today so Day 1 begins now
from datetime import date
config = json.loads(config_text)
config.setdefault("plan", {})["start_date"] = date.today().isoformat()
account_dir.joinpath("config.json").write_text(json.dumps(config, indent=2))

# Write per-account .env — MLX login (workspace-shared) + Reddit creds
env_lines = [
    f"MULTILOGIN_EMAIL={os.environ['MULTILOGIN_EMAIL']}",
    f"MULTILOGIN_PASSWORD={os.environ['MULTILOGIN_PASSWORD']}",
    f"REDDIT_USERNAME={REDDIT_USERNAME}",
    f"REDDIT_EMAIL={REDDIT_EMAIL}",
    f"REDDIT_PASSWORD={REDDIT_PASSWORD}",
]
account_dir.joinpath(".env").write_text("\n".join(env_lines) + "\n")
os.chmod(account_dir / ".env", 0o600)

# Copy plan template
import shutil
shutil.copy(REPO / "plan.md", account_dir / "plan.md")

print()
print(f"[OK] {REDDIT_USERNAME} bootstrapped at {account_dir}")
print(f"  MLX profile name: {profile_name}")
print(f"  MLX profile id:   {profile_id}")
print(f"  Anchor sub:       {ANCHOR_SUB}")
print(f"  start_date:       {config['plan']['start_date']} (Day 1)")
print(f"  proxy:            country=lv type=residential")
print()
print(f"[next steps]")
print(f"  1. Open profile in MLX desktop UI manually, log in to Reddit as {REDDIT_USERNAME}")
print(f"     OR run: python3 tests/login_to_reddit.py {REDDIT_USERNAME}")
print(f"  2. First session manual: docker run ... python3 cli.py run {REDDIT_USERNAME}")
print(f"  3. After that, host cron picks it up automatically from next_run.json")
