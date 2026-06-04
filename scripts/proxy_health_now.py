"""Quick proxy + Reddit reachability check."""
import os, sys, time
sys.path.insert(0, "/root/skills/user/working-with-multilogin/scripts")
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
for f in (REPO / ".env", REPO / "accounts/smug_pickle72/.env"):
    if f.exists():
        for line in f.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
from mlx_client import Client
from playwright.sync_api import sync_playwright, TimeoutError as PWT

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"]); c.signin()
folder = "33b31a69-2819-43c6-811a-2bebf5c09999"
pid = "0129cd1d-acba-4c2f-bccd-16492a2881d7"
try:
    c.stop(pid); time.sleep(2)
except Exception: pass
port = c.start(folder, pid)
print(f"mlx start OK port={port}")
time.sleep(6)
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    page = b.contexts[0].pages[0]
    for url, label in [
        ("https://api.ipify.org?format=json", "ipify"),
        ("https://www.reddit.com/", "reddit-home"),
    ]:
        try:
            r = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            txt = page.evaluate("() => document.body && document.body.innerText ? document.body.innerText.slice(0, 200) : '<no body>'")
            print(f"{label}: HTTP {r.status} | {txt}")
        except PWT as e:
            print(f"{label}: TIMEOUT {str(e)[:120]}")
        except Exception as e:
            print(f"{label}: {type(e).__name__}: {str(e)[:120]}")
c.stop(pid)
