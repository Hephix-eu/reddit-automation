"""Tests for the mechanical comment-quality floor.

These tests pin the behavior of the validators that catch what the LLM
missed on 2026-06-02 (duplicate comments + off-topic generic filler).
"""
from __future__ import annotations

import sys
import sqlite3
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from lib import comment_quality, db  # noqa: E402


# ---- Fixtures --------------------------------------------------------------

@pytest.fixture
def empty_db(tmp_path: Path) -> Path:
    """Initialized state.db with no comment history."""
    p = tmp_path / "state.db"
    db.init(p)
    return p


@pytest.fixture
def db_with_past(tmp_path: Path) -> Path:
    """state.db pre-populated with one past comment."""
    p = tmp_path / "state.db"
    db.init(p)
    db.insert(
        p,
        type="Action",
        status="done",
        action_type="comment",
        subreddit="r/AskReddit",
        target_url="https://www.reddit.com/r/AskReddit/comments/aaa/foo/",
        submitted_content=(
            "the stuff i actually regret isn't the risky things i did, "
            "it's the boring hours that slipped by without me noticing. "
            "easier to see in retrospect but still."
        ),
    )
    return p


# ---- Dedupe ---------------------------------------------------------------

def test_clean_comment_passes(db_with_past: Path) -> None:
    """A genuinely fresh comment should not be rejected."""
    text = (
        "honestly the cheapest one i tried was a notebook on my desk. "
        "no app, no notifications, just a pen."
    )
    assert comment_quality.check_all(text, db_with_past) is None


def test_exact_duplicate_rejected(db_with_past: Path) -> None:
    """Exact match of a past comment (case + whitespace variations) is rejected."""
    past = (
        "the stuff i actually regret isn't the risky things i did, "
        "it's the boring hours that slipped by without me noticing. "
        "easier to see in retrospect but still."
    )
    # Same text
    assert comment_quality.check_dedupe(past, db_with_past) == "exact_duplicate"
    # Case + extra whitespace shouldn't fool it.
    munged = "  THE STUFF i actually REGRET isn't the risky things i did, " \
             "it's the boring hours that slipped by without me noticing. " \
             "easier to see in retrospect but still.  "
    assert comment_quality.check_dedupe(munged, db_with_past) == "exact_duplicate"


def test_near_duplicate_5gram_rejected(db_with_past: Path) -> None:
    """A heavily-overlapping rephrase should be caught by 5-gram overlap."""
    # Keep most of the original n-grams; tweak the ending only.
    near = (
        "the stuff i actually regret isn't the risky things i did, "
        "it's the boring hours that slipped by without me noticing. "
        "much easier to see later on."
    )
    reason = comment_quality.check_dedupe(near, db_with_past)
    assert reason is not None
    assert reason.startswith("near_duplicate:")


def test_dissimilar_comment_not_flagged_as_dupe(db_with_past: Path) -> None:
    """A completely different comment should NOT trip the 5-gram check."""
    text = "bought a $12 kitchen timer instead of using my phone for it."
    assert comment_quality.check_dedupe(text, db_with_past) is None


def test_empty_db_never_rejects(empty_db: Path) -> None:
    text = "anything goes when there's no history yet."
    assert comment_quality.check_dedupe(text, empty_db) is None


def test_nonexistent_db_never_rejects(tmp_path: Path) -> None:
    """If the DB file doesn't exist, dedupe is vacuously satisfied."""
    text = "first comment ever, no DB at all."
    missing = tmp_path / "nope.db"
    assert comment_quality.check_dedupe(text, missing) is None


# ---- Blacklist ------------------------------------------------------------

# Parametrize over every entry in BLACKLIST so adding a new phrase is
# automatically covered by a test.
@pytest.mark.parametrize("phrase", sorted(comment_quality.BLACKLIST))
def test_each_blacklist_phrase_rejected(phrase: str) -> None:
    """Every blacklisted phrase, wrapped in a plausible comment, must be rejected."""
    sample = f"yeah honestly, {phrase} and that's been my experience."
    reason = comment_quality.check_phrase_blacklist(sample)
    assert reason is not None, f"phrase {phrase!r} not rejected"
    assert reason.startswith("blacklisted_phrase:")


def test_blacklist_case_insensitive() -> None:
    assert comment_quality.check_phrase_blacklist(
        "GREAT QUESTION, here's what I think"
    ) is not None


def test_clean_text_passes_blacklist() -> None:
    """A comment with none of the banned phrases should pass."""
    text = "bought a kitchen timer and my screen time dropped overnight."
    assert comment_quality.check_phrase_blacklist(text) is None


def test_indeed_word_boundary_no_false_positive() -> None:
    """The blacklist entry 'indeed' must require word boundaries - it should
    not match a substring inside another word."""
    # 'indeedlikely' contains 'indeed' as a substring but is not the
    # English word 'indeed'. Word-boundary matching should let it pass.
    text = "indeedlikely the most underrated tool in my workflow."
    assert comment_quality.check_phrase_blacklist(text) is None


def test_indeed_as_standalone_word_rejected() -> None:
    text = "Indeed, that's been my experience too."
    assert comment_quality.check_phrase_blacklist(text) is not None


# ---- Real 2026-06-02 ban-cluster strings ---------------------------------

def test_off_topic_monkey_case_blacklisted() -> None:
    """Real failing comment from steepsalmon_13 - shadowbanned 2026-06-02."""
    assert (
        comment_quality.check_phrase_blacklist(
            "might be worth trying the simplest option first"
        )
        is not None
    )


def test_real_crispygopher_duplicate_blacklisted() -> None:
    """Real failing comment from crispygopher_9 - shadowbanned 2026-06-02."""
    assert (
        comment_quality.check_phrase_blacklist(
            "been wondering about this myself"
        )
        is not None
    )


def test_real_crispygopher_full_comment_blacklisted() -> None:
    """The actual full string crispygopher_9 pasted twice."""
    assert (
        comment_quality.check_phrase_blacklist(
            "been wondering about this myself. "
            "the comments here are interesting."
        )
        is not None
    )


# ---- check_all orchestration ---------------------------------------------

def test_check_all_short_circuits_on_blacklist(db_with_past: Path) -> None:
    """If the text fails the blacklist, we don't even reach the DB check."""
    text = "great question!"
    reason = comment_quality.check_all(text, db_with_past)
    assert reason is not None
    assert reason.startswith("blacklisted_phrase:")


def test_check_all_falls_through_to_dedupe(db_with_past: Path) -> None:
    """Clean of blacklist but a duplicate -> still rejected, via dedupe."""
    past = (
        "the stuff i actually regret isn't the risky things i did, "
        "it's the boring hours that slipped by without me noticing. "
        "easier to see in retrospect but still."
    )
    assert comment_quality.check_all(past, db_with_past) == "exact_duplicate"


def test_check_all_passes_clean_comment(db_with_past: Path) -> None:
    text = "bought a $12 kitchen timer instead of my phone, helped a lot."
    assert comment_quality.check_all(text, db_with_past) is None
