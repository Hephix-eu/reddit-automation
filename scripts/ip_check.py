"""Check actual outbound IP per MLX profile via page.goto (browser stack)."""
import os, sys, json
from pathlib import Path

sys.path.insert(0, "/app")
from lib import multilogin

def load_env(p):
    if not os.path.exists(p): return
    for line in open(p).read().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# Load workspace + acctfarm creds (mounted)
load_env("/app/.env")
load_env("/app/.env.acctfarm")

ACCOUNTS_DIR = Path("/app/accounts")
results = []

for d in sorted(ACCOUNTS_DIR.iterdir()):
    if not d.is_dir() or d.name.startswith("."): continue
    user = d.name
    # Also load per-account .env (it may override MLX creds)
    if (d / ".env").exists():
        load_env(str(d / ".env"))
    try:
        cfg = json.loads((d / "config.json").read_text())
    except Exception:
        results.append({"user": user, "error": "no config"})
        continue
    print(f"\n=== {user} ===", flush=True)
    try:
        with multilogin.session(cfg) as (mlx, profile_id, cdp_port):
            from playwright.sync_api import sync_playwright
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{cdp_port}")
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto("https://ipinfo.io/json", timeout=30000, wait_until="domcontentloaded")
                    body = page.evaluate("() => document.body.innerText")
                    info = json.loads(body)
                    results.append({
                        "user": user,
                        "ip": info.get("ip"),
                        "country": info.get("country"),
                        "org": (info.get("org") or "")[:80],
                    })
                    print(f"  ip={info.get('ip')}  country={info.get('country')}  org={(info.get('org') or '')[:60]}", flush=True)
                except Exception as e:
                    results.append({"user": user, "error": f"ipinfo: {type(e).__name__}: {str(e)[:120]}"})
                    print(f"  ERROR: {e}", flush=True)
    except Exception as e:
        results.append({"user": user, "error": f"session: {type(e).__name__}: {str(e)[:160]}"})
        print(f"  SESSION ERROR: {type(e).__name__}: {str(e)[:200]}", flush=True)

print("\n\n=== FINAL ===")
ips = {}
for r in results:
    if r.get("ip"):
        ips.setdefault(r["ip"], []).append(r["user"])
    print(json.dumps(r))

print("\n--- grouped by IP ---")
for ip, users in sorted(ips.items(), key=lambda x: -len(x[1])):
    print(f"  {ip}  ({len(users)} accts): {', '.join(users)}")
