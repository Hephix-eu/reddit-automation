"""Regex extraction tests for lib.creepjs against synthetic CreepJS-like text.

Real CreepJS HTML evolves, but the visible text format ("Trust Score 73%" etc.)
is what we scrape via document.body.innerText. These cases cover the variations
we've seen in the wild.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.creepjs import _extract_trust, _extract_lies


# ---- Trust score variants ----

def test_trust_with_percent():
    assert _extract_trust("Trust Score 73%") == 73


def test_trust_with_colon():
    assert _extract_trust("Trust Score: 85") == 85


def test_trust_with_space_percent():
    assert _extract_trust("Trust Score 100 %") == 100


def test_trust_zero():
    assert _extract_trust("Trust Score 0%") == 0


def test_trust_embedded_in_paragraph():
    text = "Some preamble\nTrust Score 67%\nMore stuff after"
    assert _extract_trust(text) == 67


def test_trust_missing_returns_none():
    assert _extract_trust("no relevant marker here") is None


def test_trust_out_of_range_rejected():
    # 150 isn't a valid percentage — guard against weirdness
    assert _extract_trust("Trust Score 150%") is None


def test_trust_case_insensitive():
    assert _extract_trust("trust score 50%") == 50


# ---- Lies variants ----

def test_lies_with_colon():
    assert _extract_lies("Lies: 3") == 3


def test_lies_space_separated():
    assert _extract_lies("Lies 0") == 0


def test_lies_in_parens():
    assert _extract_lies("Lies (12)") == 12


def test_lies_number_before():
    assert _extract_lies("0 Lies detected") == 0


def test_lies_missing_returns_none():
    assert _extract_lies("nothing about that") is None


def test_lies_case_insensitive():
    assert _extract_lies("LIES: 5") == 5


# ---- Realistic combined snippet ----

REALISTIC_SAMPLE = """
CreepJS
======================================
Trust Score 72%
Lies 0
Performance: 1.4s
Visits: 1
...
"""


def test_combined_realistic():
    assert _extract_trust(REALISTIC_SAMPLE) == 72
    assert _extract_lies(REALISTIC_SAMPLE) == 0


if __name__ == "__main__":
    # Allow `python tests/test_creepjs_extract.py` without pytest
    import inspect
    tests = [(n, fn) for n, fn in globals().items() if n.startswith("test_") and inspect.isfunction(fn)]
    passed, failed = 0, []
    for name, fn in tests:
        try:
            fn()
            passed += 1
        except AssertionError as e:
            failed.append((name, str(e) or "AssertionError"))
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
    print(f"{passed} passed, {len(failed)} failed")
    for name, msg in failed:
        print(f"  FAIL {name}: {msg}")
    sys.exit(0 if not failed else 1)
