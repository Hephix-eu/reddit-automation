"""Stealth verification via CreepJS.

Loads https://abrahamjuliot.github.io/creepjs/ in a Playwright page bound to
the Multilogin profile, waits for results to render, extracts Trust Score
and Lies count. Returns a verdict the agent uses to decide whether to proceed.

The page renders progressively over ~5-15s. We poll for the "Trust Score"
block to appear, then read it.

Robustness note: CreepJS's DOM evolves. Rather than brittle CSS selectors,
we scrape via `document.body.innerText` and regex out the numbers. Works
across DOM changes as long as the visible text format stays roughly:
    "Trust Score 73% ..."
    "Lies 0 ..."
"""

import re
import time
from dataclasses import dataclass
from typing import Optional


CREEPJS_URL = "https://abrahamjuliot.github.io/creepjs/"


@dataclass
class CreepJSVerdict:
    trust_score: Optional[int]   # 0-100, None if extraction failed
    lies: Optional[int]          # count of detected lies, None if extraction failed
    raw_text: str                # the page's innerText snapshot, for debugging
    passed: bool                 # True if both metrics passed thresholds

    @property
    def summary(self) -> str:
        return f"trust={self.trust_score} lies={self.lies} passed={self.passed}"


def verify(page, *, min_trust_score: int = 60, max_lies: int = 5,
           timeout_s: int = 90) -> CreepJSVerdict:
    """Navigate to CreepJS, wait for results, extract score + lies.

    Args:
        page: Playwright sync `Page` (already connected via CDP to Multilogin profile)
        min_trust_score: pass threshold (0-100)
        max_lies: pass threshold (count)
        timeout_s: max time to wait for results to render (CreepJS can take 60s+ on fresh profiles)

    Returns CreepJSVerdict.
    """
    page.goto(CREEPJS_URL, wait_until="domcontentloaded", timeout=60_000)

    # CreepJS computes results progressively (FP IDs, then scores). Wait until
    # we can actually extract both numbers — label presence alone isn't enough,
    # they appear with "Computing..." placeholders early.
    deadline = time.time() + timeout_s
    text = ""
    trust = None
    lies = None
    while time.time() < deadline:
        text = page.evaluate("() => document.body.innerText") or ""
        trust = _extract_trust(text)
        lies = _extract_lies(text)
        if trust is not None and lies is not None and "Computing" not in text:
            break
        time.sleep(2)

    passed = (
        trust is not None and lies is not None
        and trust >= min_trust_score and lies <= max_lies
    )
    return CreepJSVerdict(trust_score=trust, lies=lies, raw_text=text, passed=passed)


def _extract_trust(text: str) -> Optional[int]:
    # Matches "Trust Score 73%" / "Trust Score: 73" / "Trust Score 73 %"
    m = re.search(r"Trust\s*Score[:\s]*([0-9]{1,3})\s*%?", text, re.IGNORECASE)
    if not m:
        return None
    try:
        v = int(m.group(1))
        return v if 0 <= v <= 100 else None
    except ValueError:
        return None


def _extract_lies(text: str) -> Optional[int]:
    # Matches "Lies 0", "Lies: 3", "0 Lies", "Lies (0)" etc.
    for pat in (r"Lies[:\s]*\(?([0-9]+)\)?", r"\b([0-9]+)\s+Lies\b"):
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                return int(m.group(1))
            except ValueError:
                pass
    return None
