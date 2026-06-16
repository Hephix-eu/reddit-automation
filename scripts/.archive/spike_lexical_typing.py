"""Spike: does CDP keyboard typing reach a Lexical editor through Multilogin's
patched browser, AND can we reproduce a real human's flight-time distribution?
Tested OUTSIDE Reddit (no login, no shadow DOM, no honeypot).

Mechanism under test (the comment-UI design's bet):
  real page.mouse.click() at the editor bbox center for genuine focus, then
  type one char at a time over CDP, pacing the inter-key flight time by
  inverse-CDF sampling from fixtures/keystroke_human_trace_001.json (a real
  182-keystroke human capture). The browser emits the trusted
  keydown -> beforeinput -> input -> keyup chain itself.

Two surfaces, to localize any failure:
  A. CONTROL: a plain <div contenteditable> via page.set_content.
  B. LEXICAL: https://playground.lexical.dev/  (same framework as reddit-rte).

Also measures OUR replayed flight-time distribution and compares to the human
baseline (median 134ms, p90 293ms).

HARD RULE: never submits anything. Pure typing probe. Stops the MLX profile
cleanly (never browser.close()).

Usage (inside redditagent-image, sharing netns with multilogin):
    python3 scripts/spike_lexical_typing.py --user swift_viper14
"""
import argparse
import bisect
import json
import os
import random
import statistics as st
import sys
import time
from pathlib import Path

for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break

REPO = Path(__file__).resolve().parent.parent

CONTROL_TEXT = "the quick brown fox checks cdp keyboard input works fine"
LEXICAL_TEXT = "testing whether a lexical editor accepts paced cdp keystrokes"

# Empirical human flight-time CDF (inter-keydown ms) from the saved trace.
_CDF = json.loads(
    (REPO / "fixtures" / "keystroke_human_trace_001.json").read_text()
)["flight_model_ms"]["cdf"]
_P = [p for p, _ in _CDF]


def sample_flight_ms(rng) -> float:
    u = rng.random()
    i = max(0, min(bisect.bisect_right(_P, u) - 1, len(_CDF) - 2))
    (p0, m0), (p1, m1) = _CDF[i], _CDF[i + 1]
    return m0 + (m1 - m0) * ((u - p0) / (p1 - p0)) if p1 > p0 else m0


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


# Records performance.now() of every keydown into window.__kd so we can
# measure OUR replayed flight times, plus the first few events' isTrusted.
KD_PROBE = """() => {
  window.__kd = [];
  window.__trust = [];
  const onKd = (e) => { window.__kd.push(performance.now());
    if (window.__trust.length < 6) window.__trust.push(
      {type:e.type, trusted:e.isTrusted, data:e.key}); };
  const onIn = (e) => { if (window.__trust.length < 6) window.__trust.push(
      {type:e.type, trusted:e.isTrusted, it:e.inputType, data:e.data}); };
  document.addEventListener('keydown', onKd, true);
  document.addEventListener('beforeinput', onIn, true);
}"""


def replay_stats(page):
    kd = page.evaluate("() => window.__kd || []")
    gaps = sorted(kd[i + 1] - kd[i] for i in range(len(kd) - 1))
    if not gaps:
        return None
    def pct(p):
        k = (len(gaps) - 1) * p
        f = int(k)
        return gaps[f] if f + 1 >= len(gaps) else gaps[f] + (gaps[f + 1] - gaps[f]) * (k - f)
    return {"keys": len(kd), "median": round(st.median(gaps)),
            "p90": round(pct(0.90)), "min": round(min(gaps)), "max": round(max(gaps))}


def type_humanlike(page, text, rng) -> None:
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        if i < len(text) - 1:
            time.sleep(sample_flight_ms(rng) / 1000.0)


def click_focus(page, locator):
    locator.scroll_into_view_if_needed(timeout=5000)
    box = locator.bounding_box(timeout=5000)
    if not box:
        raise RuntimeError("no bounding box for editor")
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    time.sleep(0.4)
    return box


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", default="swift_viper14")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = random.Random(args.seed)

    load_env(REPO / ".env")
    load_env(REPO / "accounts" / args.user / ".env")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    config = json.loads((REPO / "accounts" / args.user / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]
    shots = REPO / "accounts" / args.user / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())

    def shot(page, label):
        p = shots / f"spike_lexical_{stamp}_{label}.png"
        try:
            page.screenshot(path=str(p), timeout=5000)
            return str(p)
        except Exception as e:
            return f"<shot failed: {str(e)[:60]}>"

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    try:
        c.stop(pid); time.sleep(2)
    except Exception:
        pass
    port = c.start(folder, pid)
    print(f"[setup] profile {args.user} started on port {port}")
    time.sleep(8)

    results = {"control": "not-run", "lexical": "not-run"}
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            # ===== A. CONTROL =====
            print("\n=== A. control: plain <div contenteditable> ===")
            page.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
            page.evaluate("""() => {
              document.body.innerHTML =
                '<div id=e contenteditable=true style="border:2px solid #888;'
                + 'padding:24px;font-size:22px;min-height:120px;margin:40px"></div>';
            }""")
            time.sleep(1)
            page.evaluate(KD_PROBE)
            click_focus(page, page.locator("#e"))
            type_humanlike(page, CONTROL_TEXT, rng)
            time.sleep(0.4)
            ctrl_text = page.evaluate("() => document.getElementById('e').innerText.trim()")
            print(f"  text: {ctrl_text!r}")
            print(f"  trust: {page.evaluate('() => window.__trust')}")
            print(f"  replay timing: {replay_stats(page)}")
            print(f"  shot: {shot(page, 'A_control')}")
            results["control"] = "PASS" if CONTROL_TEXT in ctrl_text else "FAIL"
            print(f"  -> CONTROL {results['control']}")

            # ===== B. LEXICAL =====
            print("\n=== B. lexical playground ===")
            navd = False
            for attempt in range(3):
                try:
                    page.goto("https://playground.lexical.dev/",
                              wait_until="commit", timeout=60000)
                    navd = True
                    break
                except Exception as e:
                    print(f"  goto attempt {attempt+1} failed: {str(e)[:80]}")
                    time.sleep(3)
            if not navd:
                print("  [fail] could not navigate to playground (proxy/network)")
                results["lexical"] = "NAV-FAIL"
                raise SystemExit  # falls through to finally cleanup
            time.sleep(10)
            print(f"  shot: {shot(page, 'B_landed')}")
            editor = None
            for label, sel in [
                ("data-lexical-editor", 'div[contenteditable="true"][data-lexical-editor="true"]'),
                (".editor-input", '.editor-input[contenteditable="true"]'),
                ("any contenteditable", '[contenteditable="true"]'),
            ]:
                loc = page.locator(sel)
                n = loc.count()
                print(f"  selector [{label}] -> count={n}")
                if n > 0 and editor is None:
                    editor = loc.first
                    print(f"    -> using [{label}]")
            if editor is None:
                print("  [fail] no Lexical editor")
                results["lexical"] = "NO-EDITOR"
            else:
                click_focus(page, editor)
                page.evaluate(KD_PROBE)
                active = page.evaluate("""() => { const a=document.activeElement;
                  return a ? {tag:a.tagName.toLowerCase(),
                    ce:a.getAttribute('contenteditable')||'',
                    lex:a.getAttribute('data-lexical-editor')||''} : null; }""")
                print(f"  activeElement: {active}")
                type_humanlike(page, LEXICAL_TEXT, rng)
                time.sleep(0.6)
                lex_text = page.evaluate("""() => { const el =
                  document.querySelector('div[contenteditable="true"][data-lexical-editor="true"]')
                  || document.querySelector('.editor-input')
                  || document.querySelector('[contenteditable="true"]');
                  return el ? (el.innerText||el.textContent||'').trim() : null; }""")
                print(f"  text: {lex_text!r}")
                print(f"  trust: {page.evaluate('() => window.__trust')}")
                print(f"  replay timing: {replay_stats(page)}  (human baseline median=134 p90=293)")
                print(f"  shot: {shot(page, 'B_typed')}")
                results["lexical"] = "PASS" if (lex_text and LEXICAL_TEXT in lex_text) else "FAIL"
                print(f"  -> LEXICAL {results['lexical']}")

            print("\n=== SUMMARY ===")
            for k, v in results.items():
                print(f"  {k:8} : {v}")
            print("  (no submission performed)")
    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped cleanly")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0 if results.get("lexical") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
