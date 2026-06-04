"""Mechanical quality floor for Reddit comments authored by the warmup agent.

This module is the LAST line of defense before a comment hits oauth.reddit.com.
The LLM is supposed to enforce voice/dedup rules in the reddit-commenter
subagent prompt, but prompt-only rules have failed in production:

  - 2026-06-02: crispygopher_9 posted the same string ("been wondering about
    this myself. the comments here are interesting.") on two unrelated
    AskReddit threads 7 minutes apart -> shadowbanned.
  - 2026-06-02: steepsalmon_13 posted "might be worth trying the simplest
    option first..." on a thread asking "what's the best thing about
    monkeys?" -> shadowbanned (off-topic generic filler).

So we add a mechanical floor:

  - check_dedupe   - rejects exact + near-duplicate (5-gram overlap) vs the
                     account's last 30 days of submitted_content.
  - check_phrase_blacklist - rejects any text containing a known bot-tell
                             phrase.

These are PURE functions: text in, reject-reason or None out. The only I/O is
read-only sqlite for dedupe. They are deliberately simple so they cannot lie:
no LLM call, no fuzzy ML, just substring + n-gram set arithmetic.
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Optional


# ---- Banned phrases (same list as in .claude/agents/reddit-commenter.md) ----
#
# Lowercase, normalized whitespace. The blacklist is a HARD floor - if any of
# these substrings appears in the candidate comment (case-insensitive), the
# comment is rejected outright. Keep this list in sync with the subagent prompt.
BLACKLIST: frozenset[str] = frozenset({
    # Generic LLM tells already in the legacy AGENT_PROMPT.md voice rules.
    "great question",
    "i hope this helps",
    # "Indeed" is too short for a plain substring match - we apply word
    # boundaries below to avoid false positives on words like "indeedlikely".
    "indeed",
    # The actual offenders that got two accounts shadowbanned 2026-06-02.
    "been wondering about this myself",
    "the comments here are interesting",
    "might be worth trying the simplest option first",
    "simplest option first",
    "in my experience overthinking it",
    "comes up more often than you'd think",
    "depends a lot on your situation",
    "sounds simple but there's a lot to unpack",
    "depending on the week",
    # Other generic-acknowledger phrases that read as bot-paste.
    "this is such an interesting topic",
    "i've been thinking about this too",
})

# Phrases short enough that a plain substring match would false-positive.
# For these we require word boundaries on both sides.
_SHORT_PHRASES_NEED_WORDBOUNDARY: frozenset[str] = frozenset({
    "indeed",
})


# ---- Helpers ---------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9']+")


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip. Used for exact-match dedupe and
    blacklist matching."""
    return re.sub(r"\s+", " ", text.lower().strip())


def _word_tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _ngrams(tokens: list[str], n: int = 5) -> set[tuple[str, ...]]:
    if len(tokens) < n:
        # For short texts, fall back to the full token tuple as the only n-gram
        # so we still detect identical short comments.
        return {tuple(tokens)} if tokens else set()
    return {tuple(tokens[i:i + n]) for i in range(len(tokens) - n + 1)}


# ---- Public checks ---------------------------------------------------------

def check_phrase_blacklist(text: str) -> Optional[str]:
    """Return a reject reason if `text` contains any blacklisted bot-tell
    phrase, else None.

    Matching is case-insensitive, on the whitespace-normalized text. For
    very short phrases (e.g. "indeed") we require word boundaries to avoid
    matching across legitimate words.
    """
    norm = _normalize(text)
    for phrase in BLACKLIST:
        if phrase in _SHORT_PHRASES_NEED_WORDBOUNDARY:
            if re.search(rf"\b{re.escape(phrase)}\b", norm):
                return f"blacklisted_phrase:{phrase}"
        else:
            if phrase in norm:
                return f"blacklisted_phrase:{phrase}"
    return None


def check_dedupe(
    text: str,
    state_db: Path,
    lookback_days: int = 30,
) -> Optional[str]:
    """Return a reject reason if `text` is too similar to a past comment
    this account already submitted, else None.

    Rules:
      - Exact match (case-insensitive, whitespace-normalized) of any past
        `submitted_content` -> reject "exact_duplicate".
      - >=60% 5-gram overlap (size of intersection divided by size of the
        candidate's 5-gram set) with ANY past comment -> reject
        "near_duplicate:5gram_overlap=NN%".

    We pull past submitted_content from `actions_log` where
    type='Action' and action_type='comment' and submitted_content IS NOT NULL,
    within the lookback window.
    """
    if not text or not text.strip():
        return None  # Empty draft - let the caller decide; not our job.

    state_db = Path(state_db)
    if not state_db.exists():
        # No DB = no history to dedupe against. The check is vacuously satisfied.
        return None

    candidate_norm = _normalize(text)
    candidate_tokens = _word_tokens(text)
    candidate_ngrams = _ngrams(candidate_tokens, n=5)

    conn = sqlite3.connect(f"file:{state_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT submitted_content FROM actions_log "
            "WHERE type='Action' AND action_type='comment' "
            "AND submitted_content IS NOT NULL "
            "AND datetime(executed_at) >= datetime('now', ?)",
            (f"-{lookback_days} days",),
        ).fetchall()
    finally:
        conn.close()

    for (past,) in rows:
        if not past:
            continue
        past_norm = _normalize(past)
        if past_norm == candidate_norm:
            return "exact_duplicate"

        if candidate_ngrams:
            past_ngrams = _ngrams(_word_tokens(past), n=5)
            if past_ngrams:
                overlap = len(candidate_ngrams & past_ngrams) / len(candidate_ngrams)
                if overlap >= 0.6:
                    pct = int(round(overlap * 100))
                    return f"near_duplicate:5gram_overlap={pct}%"

    return None


def check_all(text: str, state_db: Path) -> Optional[str]:
    """Run every quality check; return the first reject reason, or None if
    all pass.

    Order is intentional: blacklist (cheap, no I/O) before dedupe
    (sqlite query). If blacklist rejects, we never touch the DB.
    """
    reason = check_phrase_blacklist(text)
    if reason:
        return reason
    return check_dedupe(text, state_db)
