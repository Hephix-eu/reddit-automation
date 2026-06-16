"""For each active warmup account: start MLX, get current outbound IP via
ipify, fetch scamalytics.com/ip/{ip} (through the proxy itself for less-blocked
view), parse fraud score + risk classification. Stop cleanly.
"""
import json
import os
import re
import sys
import time
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p); break

REPO = Path("/app") if Path("/app/accounts").exists() else Path(__file__).resolve().parent.parent
ACCOUNTS = REPO / "accounts"

# Load workspace + an account .env for MLX creds
for envp in (REPO / ".env", ACCOUNTS / "dailyanvil" / ".env"):
    if envp.exists():
        for line in envp.read_text().splitlines():
            if "=" in line and not line.strip().startswith("#"):
                k, v = line.split("=", 1); os.environ.setdefault(k.strip(), v.strip())

from mlx_client import Client
from playwright.sync_api import sync_playwright, TimeoutError as PWT

PROFILES = [
    ("dailyanvil",  "91c021fe-b8c7-468a-a718-b69f39663fe9"),
    ("vast_prawn",  "479bb58e-7629-401b-a695-f338e7cce643"),
    ("smug_pickle72","0129cd1d-acba-4c2f-bccd-16492a2881d7"),  # banned but still has proxy
]
FOLDER = "33b31a69-2819-43c6-811a-2bebf5c09999"

c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
c.signin()

results = []
for label, pid in PROFILES:
    print(f"\n=== {label} ===")
    try: c.stop(pid); time.sleep(2)
    except Exception: pass
    try:
        port = c.start(FOLDER, pid)
    except Exception as e:
        print(f"  start FAILED: {e}")
        continue
    time.sleep(6)

    with sync_playwright() as pw:
        b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        page = b.contexts[0].pages[0]

        # Get outbound IP
        ip = None
        try:
            r = page.goto("https://api.ipify.org?format=json", wait_until="domcontentloaded", timeout=20000)
            txt = page.evaluate("() => document.body.innerText")
            ip = (json.loads(txt) or {}).get("ip")
            print(f"  outbound IP: {ip}")
        except Exception as e:
            print(f"  ipify failed: {str(e)[:120]}")

        if ip:
            # Fetch scamalytics through this proxy session — they're less likely to block
            try:
                r = page.goto(f"https://scamalytics.com/ip/{ip}", wait_until="domcontentloaded", timeout=25000)
                html = page.content()
                # Pull score + risk + ISP from HTML
                score_m = re.search(r"Fraud\s+Score:?\s*</[^>]+>\s*<[^>]+>\s*(\d+)", html, re.I) or \
                          re.search(r"score[\"']?\s*[:=]\s*[\"']?(\d+)", html, re.I) or \
                          re.search(r"\bFraud Score\b[^0-9]*(\d+)", html, re.I)
                risk_m = re.search(r"Risk:?\s*</[^>]+>\s*<[^>]+>\s*([A-Za-z][\w\s]+?)<", html) or \
                         re.search(r"\bRisk\b[^A-Za-z]*([A-Za-z]+(?:\s[A-Za-z]+)?)", html)
                # Common labels: "Low Risk", "Medium Risk", "High Risk", "Very High Risk"
                isp_m  = re.search(r"ISP[^<]*<[^>]+>\s*<[^>]+>([^<]+)<", html) or \
                         re.search(r"\bISP\b\s*[:\-]\s*([^\n<]{2,60})", html)
                proxy_m = re.search(r"is\s+(?:a|an)\s+([A-Za-z\s]+?)\s+(?:proxy|VPN|residential|datacenter|hosting)", html, re.I)
                # Coarse heuristic: look for textual flags
                flags = {
                    "vpn": bool(re.search(r"\bis a VPN\b", html, re.I)),
                    "proxy": bool(re.search(r"\bis a proxy\b", html, re.I) or re.search(r"\bproxy server\b", html, re.I)),
                    "tor": bool(re.search(r"\bis a tor exit node\b", html, re.I)),
                    "residential": bool(re.search(r"\bresidential\b", html, re.I)),
                    "mobile": bool(re.search(r"\bmobile\b", html, re.I)),
                    "datacenter": bool(re.search(r"\bdatacenter\b|\bdata center\b", html, re.I)),
                }
                result = {
                    "account": label,
                    "ip": ip,
                    "score": int(score_m.group(1)) if score_m else None,
                    "risk_text": risk_m.group(1).strip() if risk_m else None,
                    "isp": isp_m.group(1).strip() if isp_m else None,
                    "type_guess": proxy_m.group(1).strip() if proxy_m else None,
                    "flags": {k: v for k, v in flags.items() if v},
                }
                results.append(result)
                print(f"  scamalytics:")
                print(f"    score: {result['score']}")
                print(f"    risk:  {result['risk_text']}")
                print(f"    ISP:   {result['isp']}")
                print(f"    type:  {result['type_guess']}")
                print(f"    flags: {result['flags']}")
            except Exception as e:
                print(f"  scamalytics fetch failed: {type(e).__name__}: {str(e)[:200]}")

    try: c.stop(pid)
    except Exception: pass
    print(f"  stopped")

print("\n=== SUMMARY ===")
for r in results:
    print(f"  {r['account']:15} ip={r['ip']:18} score={r['score']!s:4} risk={r['risk_text']!s:18} isp={(r['isp'] or '')[:40]}")
