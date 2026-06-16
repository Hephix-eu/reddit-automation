"""Dump the actual scamalytics page text for vast_prawn's current IP, so we
can see what the 'datacenter' flag actually means vs the Low Risk score."""
import json, os, sys, time, re
from pathlib import Path
for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists(): sys.path.insert(0, _p); break
REPO = Path("/app") if Path("/app/accounts").exists() else Path(__file__).resolve().parent.parent
for envp in (REPO / ".env", REPO / "accounts/dailyanvil/.env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k,v=line.split("=",1); os.environ.setdefault(k.strip(), v.strip())
from mlx_client import Client
from playwright.sync_api import sync_playwright
c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"]); c.signin()
pid = "479bb58e-7629-401b-a695-f338e7cce643"  # vast_prawn
try: c.stop(pid); time.sleep(2)
except: pass
port = c.start("33b31a69-2819-43c6-811a-2bebf5c09999", pid); time.sleep(6)
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
    page = b.contexts[0].pages[0]
    page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=20000)
    ip = json.loads(page.evaluate("() => document.body.innerText"))["ip"]
    print(f"=== IP: {ip} ===\n")
    page.goto(f"https://scamalytics.com/ip/{ip}", wait_until="domcontentloaded", timeout=25000)
    time.sleep(2)
    # Pull the user-visible text content — the panel near the top has the classification
    text = page.evaluate("() => document.body.innerText")
    # Find the lines that matter — score, risk, ISP, classification table
    interesting_lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s: continue
        if re.search(r"score|risk|isp|asn|country|proxy|vpn|tor|datacenter|residential|mobile|hosting|cloud|server", s, re.I):
            interesting_lines.append(s)
    print("--- relevant lines from scamalytics.com page ---")
    for l in interesting_lines[:60]:
        print(f"  {l[:160]}")
c.stop(pid)
print("\n[done]")
