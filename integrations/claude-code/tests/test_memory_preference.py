"""Tests for the 'prefer Cognee memory' SessionStart steer (session-start.py).

Claude Code's native auto-memory (MEMORY.md) can't be reliably disabled by a
plugin, so session-start injects a SessionStart ``additionalContext`` instruction
asserting Cognee as the preferred memory. These drive ``_apply_memory_preference``
directly. The session-start module pulls in hook helpers; if it can't import in
this environment the tests skip (return) rather than fail.

Run: python integrations/claude-code/tests/test_memory_preference.py
(or via pytest).
"""

import importlib.util
import os
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load():
    spec = importlib.util.spec_from_file_location(
        "session_start_mod", _SCRIPTS / "session-start.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    ss = _load()
except Exception:  # pragma: no cover - hook deps not importable in this environment
    ss = None


def test_steer_injected_by_default():
    if ss is None:
        return
    os.environ.pop("COGNEE_PREFER_MEMORY", None)
    out = ss._apply_memory_preference(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "systemMessage": "hi"}}
    )
    hso = out["hookSpecificOutput"]
    assert "Cognee" in hso["additionalContext"]
    assert "FIRST" in hso["additionalContext"]  # "consult Cognee FIRST"
    assert hso["systemMessage"] == "hi"  # existing fields preserved
    assert hso["hookEventName"] == "SessionStart"


def test_empty_output_gets_session_start_block():
    if ss is None:
        return
    os.environ.pop("COGNEE_PREFER_MEMORY", None)
    out = ss._apply_memory_preference({})
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "memory" in hso["additionalContext"].lower()


def test_opt_out_disables_steer():
    if ss is None:
        return
    os.environ["COGNEE_PREFER_MEMORY"] = "false"
    try:
        out = ss._apply_memory_preference({"hookSpecificOutput": {"hookEventName": "SessionStart"}})
        assert "additionalContext" not in out["hookSpecificOutput"]
    finally:
        os.environ.pop("COGNEE_PREFER_MEMORY", None)


if __name__ == "__main__":
    if ss is None:
        print("SKIP: session-start.py not importable in this environment")
        sys.exit(0)
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
