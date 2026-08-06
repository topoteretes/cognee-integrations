"""Tests for the `{agent}_{host_session_id}` Cognee session-id convention.

The host (Claude) session id is embedded so the Cognee session maps 1:1 to the
conversation and is self-describing in the dashboard (no working-directory coupling).

Run: python integrations/claude-code/tests/test_session_id.py
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def test_embeds_host_session_id():
    os.environ.pop("COGNEE_SESSION_PREFIX", None)
    assert pc._generate_session_id("/tmp/whatever", "c92cc618-cc37-42ac") == (
        "claude_c92cc618-cc37-42ac"
    )


def test_fallback_without_host_id_uses_agent_and_dir():
    os.environ.pop("COGNEE_SESSION_PREFIX", None)
    sid = pc._generate_session_id("/tmp/myproj", "")
    assert sid.startswith("claude_myproj_")  # agent + dir + random token


def test_prefix_env_override():
    os.environ["COGNEE_SESSION_PREFIX"] = "custom"  # a non-default value, to prove override
    try:
        assert pc._generate_session_id("/x", "abc123") == "custom_abc123"
    finally:
        os.environ.pop("COGNEE_SESSION_PREFIX", None)


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
