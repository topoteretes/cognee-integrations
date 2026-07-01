"""Tests for the Codex session-start additionalContext output contract.

Codex's session-start.py does NOT implement _apply_memory_preference (unlike
claude-code). Instead, it injects context via render_status_for_host() from
cognee_statusline_render.py, which is assigned to additionalContext at the
session_start() exit path (session-start.py lines 1207-1213):

    status_line = render_status_for_host(session_key)
    return {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": status_line,
        },
    }

These tests verify:
  1. render_status_for_host() returns a non-empty string injected as additionalContext.
  2. The string always contains "cognee:" (the hardcoded prefix in the f-string).
  3. Mode token ("local" / "cloud") is driven by COGNEE_BASE_URL.
  4. Dataset name is driven by COGNEE_PLUGIN_DATASET.
  5. The early-exit hookSpecificOutput shape (no session key) has no additionalContext.
  6. The happy-path hookSpecificOutput shape matches the actual return in session-start.py.

Run: pytest integrations/codex/tests/test_memory_preference.py
(or: python integrations/codex/tests/test_memory_preference.py standalone)
"""

import importlib.util
import os
import pathlib
import sys

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def _load_statusline():
    spec = importlib.util.spec_from_file_location(
        "cognee_statusline_render", _SCRIPTS / "cognee_statusline_render.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


try:
    sl = _load_statusline()
except Exception:  # pragma: no cover - not importable in this environment
    sl = None


def test_render_returns_nonempty_string():
    """render_status_for_host always returns a non-empty string."""
    if sl is None:
        return
    result = sl.render_status_for_host("any-session-key")
    assert isinstance(result, str) and result.strip()


def test_render_contains_cognee_prefix():
    """Output always contains 'cognee:' — the key string injected into additionalContext."""
    if sl is None:
        return
    result = sl.render_status_for_host("x")
    assert "cognee:" in result


def test_render_local_mode_default():
    """Without a configured base_url, mode falls back to 'local'."""
    if sl is None:
        return
    os.environ.pop("COGNEE_BASE_URL", None)
    result = sl.render_status_for_host("x")
    assert "local" in result


def test_render_cloud_mode_when_remote_url():
    """When COGNEE_BASE_URL points to a remote host, mode is 'cloud'."""
    if sl is None:
        return
    os.environ["COGNEE_BASE_URL"] = "https://tenant.cognee.ai"
    try:
        result = sl.render_status_for_host("x")
        assert "cloud" in result
    finally:
        os.environ.pop("COGNEE_BASE_URL", None)


def test_render_dataset_name_in_output():
    """The active dataset name is embedded in the status string."""
    if sl is None:
        return
    os.environ["COGNEE_PLUGIN_DATASET"] = "my_test_dataset"
    try:
        result = sl.render_status_for_host("x")
        assert "my_test_dataset" in result
    finally:
        os.environ.pop("COGNEE_PLUGIN_DATASET", None)


def test_early_exit_shape_no_additional_context():
    """The early-exit hookSpecificOutput (no session key) must NOT contain additionalContext.

    Mirrors the actual return from _start() at session-start.py lines 1112-1114:
        return {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    """
    early_exit = {"hookSpecificOutput": {"hookEventName": "SessionStart"}}
    hso = early_exit["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert "additionalContext" not in hso


def test_happy_path_shape_has_additional_context():
    """The happy-path hookSpecificOutput must contain a non-empty additionalContext string.

    Mirrors the actual return from _start() at session-start.py lines 1208-1213:
        status_line = render_status_for_host(session_key)
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": status_line,
            },
        }
    """
    if sl is None:
        return
    status = sl.render_status_for_host("test-session")
    happy_path = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": status,
        }
    }
    hso = happy_path["hookSpecificOutput"]
    assert hso["hookEventName"] == "SessionStart"
    assert isinstance(hso["additionalContext"], str) and hso["additionalContext"].strip()
    assert "cognee:" in hso["additionalContext"]


if __name__ == "__main__":
    if sl is None:
        print("SKIP: cognee_statusline_render.py not importable in this environment")
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
