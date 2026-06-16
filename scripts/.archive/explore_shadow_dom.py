"""One-shot exploration: figure out which Playwright selector strategies pierce
shreddit's shadow DOM for upvote / save / Join buttons.

Tests four strategies side-by-side on the same page so we can compare:
  1. CSS selector on aria-label (the one that previously matched nothing)
  2. Playwright role-based locator (default: pierces open shadow roots)
  3. `>>>` combinator (explicit shadow piercing)
  4. Manual JS traversal via shadowRoot chains (always works for open roots)

Also dumps the outerHTML of one `shreddit-post` so we can see the real structure.

Run on hephix:
    python3 scripts/explore_shadow_dom.py smug_pickle72
"""
import json
import os
import re
import sys
import time
from pathlib import Path

# In-container path (baked by Dockerfile.agent) takes precedence; falls back to host path.
for _p in ("/root/skills/user/working-with-multilogin/scripts",
           str(Path.home() / "skills/user/working-with-multilogin/scripts")):
    if Path(_p).exists():
        sys.path.insert(0, _p)
        break

# Resolve repo root from the script location so this works in container (/app)
# and on host (/root/reddit-automation).
REPO = Path(__file__).resolve().parent.parent


def load_env(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(username: str) -> int:
    load_env(REPO / ".env")
    load_env(REPO / "accounts" / username / ".env")

    from mlx_client import Client
    from playwright.sync_api import sync_playwright

    config = json.loads((REPO / "accounts" / username / "config.json").read_text())
    pid = config["multilogin"]["profile_id"]
    folder = config["multilogin"]["folder_id"]

    c = Client(os.environ["MULTILOGIN_EMAIL"], os.environ["MULTILOGIN_PASSWORD"])
    c.signin()
    port = c.start(folder, pid)
    print(f"[setup] profile started on port {port}")
    time.sleep(8)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            ctx = browser.contexts[0]
            page = ctx.pages[0] if ctx.pages else ctx.new_page()

            print(f"[nav] -> https://www.reddit.com/")
            page.goto("https://www.reddit.com/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            print(f"[nav] settled at {page.url}")

            def safe(label, fn):
                try:
                    print(f"[{label}] {fn()}")
                except Exception as e:
                    print(f"[{label}] raised: {type(e).__name__}: {e}")

            # Strategy 1: original failing selector (sanity check on home feed)
            safe("S1 CSS aria-label",
                 lambda: f"button[aria-label*='upvote' i] → {page.locator('button[aria-label*=\"upvote\" i]').count()} matches")

            # Strategy 2: role-based (Playwright pierces OPEN shadow roots by default)
            safe("S2 get_by_role",
                 lambda: f"get_by_role(button, name=~upvote) → {page.get_by_role('button', name=re.compile('upvote', re.I)).count()} matches")

            # Strategy 3: explicit shadow-pierce combinator
            safe("S3 >>>",
                 lambda: f"shreddit-post >>> button[aria-label*='upvote' i] → {page.locator('shreddit-post >>> button[aria-label*=\"upvote\" i]').count()} matches")

            # Strategy 3b: alt — search across all aria-labels on the page (lighter shape)
            safe("S3b get_by_label",
                 lambda: f"get_by_label(re.compile('upvote', re.I)) → {page.get_by_label(re.compile('upvote', re.I)).count()} matches")

            # Strategy 4: manual JS traversal of shadowRoot
            js = """
              () => {
                const out = {posts: 0, buttons_with_upvote_aria: 0, shadow_roots_found: 0};
                const posts = document.querySelectorAll('shreddit-post');
                out.posts = posts.length;
                for (const p of posts) {
                  if (p.shadowRoot) {
                    out.shadow_roots_found++;
                    const btns = p.shadowRoot.querySelectorAll('button');
                    for (const b of btns) {
                      const al = (b.getAttribute('aria-label') || '').toLowerCase();
                      if (al.includes('upvote')) out.buttons_with_upvote_aria++;
                    }
                  }
                }
                return out;
              }
            """
            s4 = page.evaluate(js)
            print(f"[S4] manual JS traversal → {json.dumps(s4)}")

            # Dump the first shreddit-post outerHTML (truncated) so we can SEE the structure
            html = page.evaluate("""
              () => {
                const p = document.querySelector('shreddit-post');
                if (!p) return '<no shreddit-post on page>';
                // Get light DOM
                const light = p.outerHTML.slice(0, 1500);
                // Get shadow DOM if accessible
                let shadow = '<no open shadow root>';
                if (p.shadowRoot) shadow = p.shadowRoot.innerHTML.slice(0, 2000);
                return JSON.stringify({light_dom: light, shadow_dom: shadow});
              }
            """)
            parsed = json.loads(html) if html.startswith("{") else {"err": html}
            print(f"\n[dump] first shreddit-post light DOM:")
            print(parsed.get("light_dom", "")[:1200])
            print(f"\n[dump] first shreddit-post shadow DOM (or notice):")
            print(parsed.get("shadow_dom", "")[:1500])

            # Also probe Join button on a subreddit page
            print(f"\n[nav] -> https://www.reddit.com/r/dotnet/")
            page.goto("https://www.reddit.com/r/dotnet/", wait_until="domcontentloaded", timeout=30000)
            time.sleep(5)
            safe("Join.S1",
                 lambda: f"button:has-text('Join') → {page.locator('button:has-text(\"Join\")').count()} matches")
            safe("Join.S2",
                 lambda: f"get_by_role(button, name=~join) → {page.get_by_role('button', name=re.compile('join', re.I)).count()} matches")
            safe("Join.S3",
                 lambda: f"get_by_text('Join', exact=True) → {page.get_by_text('Join', exact=True).count()} matches")

    finally:
        try:
            c.stop(pid)
            print("\n[cleanup] profile stopped")
        except Exception as e:
            print(f"[cleanup] stop raised: {e}")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "smug_pickle72"))
