"""Regression tests for sanitize_dataset_name in the claude-code integration.

Run:
    python integrations/claude-code/tests/test_dataset_name.py
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402

DEFAULT = "agent_sessions"

CASES = [
    # ── valid names ──
    ("mydataset",    "mydataset"),
    ("project_01",   "project_01"),
    ("company-prod", "company-prod"),
    ("test.v1",      "test.v1"),
    # ── invalid names ──
    ("My Dataset",   "My_Dataset"),
    ("My@Data",      "My_Data"),
    ("!!hello!!",    "hello"),
    ("###",          DEFAULT),
    ("........",     DEFAULT),
    ("___",          DEFAULT),
    ("a" * 200,      "a" * 120),
    ("你好 Dataset",  "Dataset"),
    # ── edge cases ──
    ("",             DEFAULT),
    ("  ",           DEFAULT),
    ("-leading",     "-leading"),
    (".leading",     "leading"),
    ("_leading",     "leading"),
    ("trailing.",    "trailing"),
]


def test_sanitize_dataset_name_issue_cases() -> None:
    for name, expected in CASES:
        result = pc.sanitize_dataset_name(name, DEFAULT)
        assert result == expected, (
            f"sanitize_dataset_name({name!r}) = {result!r}, want {expected!r}"
        )


def test_matches_session_key_sanitizer_for_valid_chars() -> None:
    """For ASCII-only valid names, both sanitizers must agree."""
    valid = ["mydataset", "project_01", "company-prod", "test.v1"]
    for name in valid:
        assert pc.sanitize_dataset_name(name, DEFAULT) == pc._sanitize_session_key(name), (
            f"sanitizers disagree on {name!r}"
        )


def test_fallback_only_when_empty_after_sanitization() -> None:
    assert pc.sanitize_dataset_name("ok", "fb") == "ok"
    assert pc.sanitize_dataset_name("###", "fb") == "fb"


def test_max_length() -> None:
    result = pc.sanitize_dataset_name("x" * 200, DEFAULT)
    assert len(result) == 120


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
