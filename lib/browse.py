"""Humanlike browsing primitives.

Used by the test scripts AND by the agent during sessions. Keeps the
scroll/dwell rhythm consistent across both contexts.

Design notes:
  - Mouse-wheel deltas vary per event, not constant.
  - Bursts of 3-7 consecutive wheels (skimming) → reading pause → repeat.
  - ~5% chance of a small reverse scroll (re-read).
  - Total duration is approximate, not strict — humans don't time their browsing.
  - All `time.sleep` durations are randomized in narrow but realistic bands.

Anti-bot rationale: simple loops with constant deltas + constant sleeps
produce a perfectly periodic scroll-velocity histogram. Anti-bot systems
fingerprint that. Real users have heavy-tailed distributions in both delta
and inter-event time.
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
        # --- One "burst" = 3-7 wheels with short inter-wheel pauses ---
        burst_len = r.randint(3, 7)
        bursts += 1
        for _ in range(burst_len):
            if time.time() >= deadline:
                break
            # 5% reverse scroll within the burst
            if r.random() < 0.05:
                delta = -r.randint(150, 400)
                reverses += 1
            else:
                delta = r.randint(250, 1100)
            page.mouse.wheel(0, delta)
            distance += abs(delta)
            wheels += 1
            time.sleep(r.uniform(0.08, 0.45))

        # --- Reading pause between bursts ---
        # Two flavors: quick glance (0.6-1.8s) or read (2-5s). Heavy-tailed.
        if r.random() < 0.7:
            time.sleep(r.uniform(0.6, 1.8))
        else:
            time.sleep(r.uniform(2.0, 5.0))

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
