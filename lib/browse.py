"""Humanlike browsing primitives.

Used by the test scripts AND by the agent during sessions. Keeps the
scroll/dwell rhythm consistent across both contexts.

Design notes (calibrated from real trackpad wheel-event capture):
  - Each gesture is a bell-curve momentum envelope: ramp → plateau → decay,
    matching how macOS/trackpad inertia actually fires WheelEvents.
  - Events fire at ~17 ms intervals (≈60 Hz), matching trackpad hardware.
  - Per-event deltaY: 1–200 px; peak per gesture 15–120 px forward,
    8–40 px reverse.
  - ~15% of gestures scroll backward (re-read); these are full bell-curve
    reverse gestures, not single events.
  - Between-gesture pauses: quick re-skim 0.08–0.35 s (35%), normal read
    0.6–1.8 s (40%), deep read 2–5 s (25%).
  - Total duration is approximate — humans don't time their browsing.

Anti-bot rationale: constant deltas + constant sleeps produce a periodic
scroll-velocity histogram that anti-bot systems fingerprint. Real trackpad
input has a bell-curve velocity envelope per gesture and ~17 ms inter-event
timing — both very different from the naive "random big delta every 200ms"
pattern.
"""

import bisect
import math
import random
import re
import time

# Persists last known cursor position across calls so moves don't teleport.
_last_mouse_pos: tuple[float, float] | None = None


def human_mouse_move(page, x: float, y: float, *,
                     rng: random.Random | None = None) -> None:
    """Move the cursor from the last known position to (x, y) along a curved path.

    Uses a quadratic Bézier with a randomised control point so the trajectory
    is never a straight line. Speed follows a smoothstep ease (slow → fast →
    slow), matching how trackpad/mouse deceleration actually looks.
    Updates the module-level position so the next call starts correctly.
    """
    global _last_mouse_pos
    r = rng or random

    if _last_mouse_pos is None:
        # First call: start from a plausible resting spot mid-viewport.
        sx = r.uniform(250, 700)
        sy = r.uniform(200, 500)
    else:
        sx, sy = _last_mouse_pos

    dist = math.hypot(x - sx, y - sy)
    if dist < 2:
        _last_mouse_pos = (x, y)
        return

    # Control point offset calibrated from recorded data: median curvature ratio
    # (max lateral deviation / chord) = 0.085, p95 = 0.55. Control offset =
    # 2 * curvature * chord, so ±25% of chord covers the typical range.
    cx = (sx + x) / 2 + r.uniform(-min(dist * 0.25, 80), min(dist * 0.25, 80))
    cy = (sy + y) / 2 + r.uniform(-min(dist * 0.20, 60), min(dist * 0.20, 60))

    # Real data: median speed 1.14 px/ms, 17ms per event → ~19 px/step.
    # dist/18 matches that; cap keeps extreme distances reasonable.
    n_steps = max(6, min(40, int(dist / 18)))

    for i in range(1, n_steps + 1):
        t = i / n_steps
        # Smoothstep: slow at start and end, fast in the middle.
        te = t * t * (3 - 2 * t)
        bx = (1 - te) ** 2 * sx + 2 * (1 - te) * te * cx + te ** 2 * x
        by = (1 - te) ** 2 * sy + 2 * (1 - te) * te * cy + te ** 2 * y
        # Per-step jitter: real recordings show ±2 px variance at this granularity.
        bx += r.uniform(-2.0, 2.0)
        by += r.uniform(-2.0, 2.0)
        page.mouse.move(bx, by)
        # Real inter-event median 17ms, p5–p95 range 10–21ms.
        time.sleep(r.uniform(0.013, 0.020))

    page.mouse.move(x, y)
    _last_mouse_pos = (x, y)


def human_click(page, x: float, y: float, *,
                rng: random.Random | None = None,
                button: str = "left") -> None:
    """Move to (x, y) humanly, pause briefly, then click.

    Prefer this over bare page.mouse.click() for any deliberate UI action.
    """
    global _last_mouse_pos
    r = rng or random
    human_mouse_move(page, x, y, rng=r)
    time.sleep(r.uniform(0.04, 0.14))   # micro-pause before pressing
    page.mouse.click(x, y, button=button)
    _last_mouse_pos = (x, y)


def get_cursor_pos() -> tuple[float, float] | None:
    """Return the last known cursor position in viewport CSS pixels, or None."""
    return _last_mouse_pos


def click_element(page, locator, *,
                  rng: random.Random | None = None,
                  button: str = "left") -> None:
    """Move to a Playwright locator's center humanly, then click.

    Falls back to locator.click() when bounding_box() returns None (element
    is off-screen or inside a closed shadow root where geometry isn't exposed).
    Use this for every deliberate UI click — upvotes, saves, subscribes, post
    links, login buttons, submit — instead of bare locator.click().
    """
    box = locator.bounding_box()
    if box:
        human_click(
            page,
            box["x"] + box["width"] / 2,
            box["y"] + box["height"] / 2,
            rng=rng,
            button=button,
        )
    else:
        locator.click()


def human_scroll(page, *, duration_s: float = 60.0,
                 rng: random.Random | None = None) -> dict:
    """Scroll the current page at humanlike rhythm for ~`duration_s` seconds.

    Returns a small dict with telemetry (counts, total distance) for logging.
    """
    r = rng or random
    deadline = time.time() + duration_s
    wheels = 0
    reverses = 0
    bursts = 0
    distance = 0

    while time.time() < deadline:
        # ~15% chance of a full reverse (upward re-read) gesture
        is_reverse = r.random() < 0.15
        if is_reverse:
            reverses += 1
            peak = r.randint(8, 40)
        else:
            peak = r.randint(15, 120)

        # Trackpad momentum produces ~20–55 events per gesture
        n_events = r.randint(20, 55)
        bursts += 1

        for i in range(n_events):
            if time.time() >= deadline:
                break
            # Bell-curve envelope: ramp (0–25%) → plateau (25–55%) → decay
            t = i / n_events
            if t < 0.25:
                env = t / 0.25
            elif t < 0.55:
                env = 1.0
            else:
                env = (1.0 - t) / 0.45
            delta = max(1, int(peak * env * r.uniform(0.75, 1.25)))
            if is_reverse:
                delta = -delta
            page.mouse.wheel(0, delta)
            distance += abs(delta)
            wheels += 1
            # ~17 ms between events, matching 60 Hz trackpad hardware
            time.sleep(r.uniform(0.013, 0.022))

        # Pause after gesture
        p = r.random()
        if p < 0.35:
            time.sleep(r.uniform(0.08, 0.35))   # quick re-skim
        elif p < 0.75:
            time.sleep(r.uniform(0.6, 1.8))     # normal reading pause
        else:
            time.sleep(r.uniform(2.0, 5.0))     # deep read

    return {
        "wheels": wheels,
        "bursts": bursts,
        "reverses": reverses,
        "distance_px": distance,
        "duration_s": round(duration_s, 1),
    }


def dwell(seconds_min: float = 0.4, seconds_max: float = 1.5,
          rng: random.Random | None = None) -> None:
    """A short randomized pause. Use between deliberate actions (clicks, navigations)."""
    r = rng or random
    time.sleep(r.uniform(seconds_min, seconds_max))


# Inter-keydown flight-time distribution (ms), empirically derived from a real
# 182-keystroke human capture (fixtures/keystroke_human_trace_001.json):
# median 134, mean 167, heavy right tail to ~1.2 s for word/sentence pauses,
# ~4% sub-70ms rollover bursts. Sampled via inverse-CDF so generated typing
# matches human rhythm rather than a constant delay — the #1 tell of scripted
# input. Validated end-to-end against a live Lexical editor through Multilogin
# (scripts/.archive/spike_lexical_typing.py): replay median 164 / p90 302 vs 134 / 293.
_FLIGHT_CDF: list[tuple[float, float]] = [
    (0.00, 40), (0.05, 72), (0.10, 84), (0.25, 103), (0.50, 134),
    (0.75, 182), (0.90, 293), (0.95, 384), (0.99, 543), (1.00, 1211),
]
_FLIGHT_PS = [p for p, _ in _FLIGHT_CDF]


def _sample_flight_ms(r: random.Random) -> float:
    """Draw one inter-key flight time (ms) from the empirical human CDF."""
    u = r.random()
    i = max(0, min(bisect.bisect_right(_FLIGHT_PS, u) - 1, len(_FLIGHT_CDF) - 2))
    (p0, m0), (p1, m1) = _FLIGHT_CDF[i], _FLIGHT_CDF[i + 1]
    return m0 + (m1 - m0) * ((u - p0) / (p1 - p0)) if p1 > p0 else m0


def human_type(page, text: str, *, rng: random.Random | None = None) -> None:
    """Type `text` into the currently-focused element one char at a time, over
    CDP, pacing inter-key flight time by inverse-CDF sampling from a real human
    keystroke trace (see _FLIGHT_CDF).

    The CALLER must focus the target first (e.g. browse.click_element on the
    editor / input). The browser emits the trusted keydown → beforeinput →
    input → keyup chain itself; we only shape the timing. Use this for EVERY
    text entry (comment body, login fields) instead of inline
    `page.keyboard.type` loops, so human timing has a single source of truth.
    """
    r = rng or random.Random()
    last = len(text) - 1
    for i, ch in enumerate(text):
        page.keyboard.type(ch)
        if i < last:
            time.sleep(_sample_flight_ms(r) / 1000.0)


def dismiss_cookie_popup(page, *, rng: random.Random | None = None) -> bool:
    """Dismiss Reddit's GDPR/cookie consent popup without relying on button text.

    Language-agnostic: uses structural CSS and data-testid selectors rather than
    visible text, so it works regardless of which language the account has configured.

    Targets the reject/decline action (minimises data exposure). Returns True if a
    popup was found and dismissed, False if nothing was visible.
    """
    r = rng or random

    # Ordered most-specific → most-generic; stops at first visible match.
    selectors = [
        # Shreddit (new Reddit) custom element — last button is always Reject
        "shreddit-cookie-banner button:last-of-type",
        # data-testid patterns from Reddit's consent form source
        '[data-testid="gdpr-consent-reject"]',
        '[data-testid="reject-nonessential"]',
        '[data-testid="gdpr-reject"]',
        '[data-testid="reject"]',
        # Named form container — last button = reject
        "#gdpr-consent-form button:last-of-type",
        "#GDPR-consent-form button:last-of-type",
        # Class-heuristic: secondary/outline button inside a consent/cookie wrapper
        '[class*="gdpr"] button[class*="secondary"]',
        '[class*="consent"] button[class*="secondary"]',
        '[class*="cookie"] button[class*="secondary"]',
        # Aria-labelled dialog containing cookie/consent reference
        '[aria-label*="Cookie" i] button:last-of-type',
        '[aria-label*="consent" i] button:last-of-type',
    ]

    for sel in selectors:
        try:
            btn = page.locator(sel).first
            if btn.is_visible(timeout=500):
                btn.click()
                time.sleep(r.uniform(0.8, 1.5))
                # Stamp localStorage so Reddit suppresses the banner on all
                # subsequent page navigations within this session.
                try:
                    page.evaluate("""() => {
                        try { localStorage.setItem('gdprConsentExpiry', String(Date.now() + 365*24*60*60*1000)); } catch(e) {}
                        try { localStorage.setItem('euCookieConsentExpiry', String(Date.now() + 365*24*60*60*1000)); } catch(e) {}
                        try { localStorage.setItem('consentExpiry', String(Date.now() + 365*24*60*60*1000)); } catch(e) {}
                    }""")
                except Exception:
                    pass
                return True
        except Exception:
            continue

    # Text-based fallback for Reddit's newer "cookie preferences" modal, whose
    # buttons are plain <button>s with visible text (the structural selectors
    # above miss it). Prefer the reject/decline action; fall back to accept so
    # the overlay never blocks the composer. English-locale accounts only.
    for pat in (r"reject optional|reject all|reject non|decline|necessary only",
                r"accept all|accept"):
        try:
            b = page.get_by_role("button", name=re.compile(pat, re.I)).first
            if b.count() > 0 and b.is_visible(timeout=500):
                b.click(timeout=4000)
                time.sleep(r.uniform(0.8, 1.5))
                return True
        except Exception:
            continue
    return False


def get_post_permalink(btn) -> tuple[str | None, str | None]:
    """Return (target_url, subreddit) from any button inside a <shreddit-post>.

    Walks up the DOM crossing shadow boundaries to find the enclosing
    shreddit-post element and reads its permalink attribute. Works for upvote,
    save, More options, comment-reply. Returns (None, None) if not inside a post.
    Capture the result BEFORE clicking the button.
    """
    permalink = btn.evaluate("""el => {
        let cur = el;
        while (cur && cur.tagName?.toLowerCase() !== 'shreddit-post') {
            cur = cur.parentElement || (cur.getRootNode() && cur.getRootNode().host);
        }
        return cur ? cur.getAttribute('permalink') : null;
    }""")
    if not permalink:
        return None, None
    target_url = f"https://www.reddit.com{permalink}"
    subreddit = permalink.split('/r/')[1].split('/')[0] if '/r/' in permalink else None
    return target_url, f"r/{subreddit}" if subreddit else None
