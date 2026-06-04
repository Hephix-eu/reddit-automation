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

import random
import time


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
