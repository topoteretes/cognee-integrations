"""Regression tests for _sanitize_dataset_name in the hermes-agent integration.

Covers every valid/invalid input from issue #3549 and verifies that the
hermes-agent sanitizer produces exactly the expected output.

Run:
    python integrations/hermes-agent/tests/test_dataset_name.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from cognee_integration_hermes.provider import _sanitize_dataset_name  # noqa: E402

DEFAULT = "hermes"

# (input_name, expected_output)
# When expected_output is DEFAULT the sanitizer must fall back to the default.
CASES = [
    # ── valid names — must pass through unchanged ──
    ("mydataset",    "mydataset"),
    ("project_01",   "project_01"),
    ("company-prod", "company-prod"),
    ("test.v1",      "test.v1"),
    # ── invalid names — must be normalised ──
    ("My Dataset",   "My_Dataset"),     # space → _
    ("My@Data",      "My_Data"),        # @ → _
    ("!!hello!!",    "hello"),          # leading/trailing _ stripped
    ("###",          DEFAULT),          # all _ after replace → stripped → empty → fallback
    ("........",     DEFAULT),          # all . stripped → empty → fallback
    ("___",          DEFAULT),          # all _ stripped → empty → fallback
    ("a" * 200,      "a" * 120),        # truncated to 120
    ("你好 Dataset",  "Dataset"),        # non-ASCII → _, then stripped
    # ── edge cases ──
    ("",             DEFAULT),          # empty string → fallback
    ("  ",           DEFAULT),          # spaces only → __ → stripped → fallback
    ("-leading",     "-leading"),       # leading - is NOT stripped (only . and _ are)
    (".leading",     "leading"),        # leading . stripped
    ("_leading",     "leading"),        # leading _ stripped
    ("trailing.",    "trailing"),       # trailing . stripped
    ("a" * 120,      "a" * 120),        # exactly at the limit — unchanged
    ("a" * 121,      "a" * 120),        # one over the limit — truncated
]


def test_sanitize_dataset_name_valid_names() -> None:
    for name, expected in CASES[:4]:
        result = _sanitize_dataset_name(name, DEFAULT)
        assert result == expected, (
            f"[valid] _sanitize_dataset_name({name!r}) = {result!r}, want {expected!r}"
        )


def test_sanitize_dataset_name_invalid_names() -> None:
    for name, expected in CASES[4:]:
        result = _sanitize_dataset_name(name, DEFAULT)
        assert result == expected, (
            f"[invalid] _sanitize_dataset_name({name!r}) = {result!r}, want {expected!r}"
        )


def test_fallback_used_only_when_empty_after_sanitization() -> None:
    """Fallback must NOT fire for names that are non-empty after sanitization."""
    assert _sanitize_dataset_name("ok_name", "fallback") == "ok_name"
    assert _sanitize_dataset_name("###", "fallback") == "fallback"


def test_max_length_is_120() -> None:
    long = "x" * 200
    result = _sanitize_dataset_name(long, DEFAULT)
    assert len(result) == 120, f"expected length 120, got {len(result)}"


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, "→", exc)
    raise SystemExit(1 if failures else 0)
