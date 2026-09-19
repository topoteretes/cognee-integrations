"""Conformance test: claude-code session-id sanitization matches the shared spec.

Loads the shared case table in integrations/conformance/session_id_cases.json
(the same table the codex, hermes-agent and openclaw tests use) and checks the
claude-code sanitizer against it. If any implementation drifts from the shared
rule, its test fails.

Run: python integrations/claude-code/tests/test_session_id_conformance.py (or via pytest).
"""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def _load_cases():
    root = next(p for p in pathlib.Path(__file__).resolve().parents if p.name == "integrations")
    text = (root / "conformance" / "session_id_cases.json").read_text(encoding="utf-8")
    return json.loads(text)


def test_sanitizer_matches_shared_table():
    mismatches = []
    for case in _load_cases():
        result = pc._sanitize_session_key(case["input"])
        if result != case["expected"]:
            mismatches.append(f"{case['input']!r} -> {result!r}, expected {case['expected']!r}")
    assert not mismatches, "session-id sanitization drift:\n" + "\n".join(mismatches)


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
