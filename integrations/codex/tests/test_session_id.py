"""Tests for the `{agent}_{host_session_id}` Cognee session-id convention.

The host (Codex) session id is embedded so the Cognee session maps 1:1 to the
conversation and is self-describing in the dashboard (no working-directory coupling).

Codex-specific differences vs claude-code:
  - default agent prefix is "codex" (not "claude")
  - fallback uses CODEX_CWD env var (not CLAUDE_CWD)

Run: python integrations/codex/tests/test_session_id.py
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"))

import _plugin_common as pc  # noqa: E402


def test_embeds_host_session_id():
    os.environ.pop("COGNEE_SESSION_PREFIX", None)
    assert pc._generate_session_id("/tmp/whatever", "c92cc618-cc37-42ac") == (
        "codex_c92cc618-cc37-42ac"
    )


def test_fallback_without_host_id_uses_agent_and_dir():
    os.environ.pop("COGNEE_SESSION_PREFIX", None)
    sid = pc._generate_session_id("/tmp/myproj", "")
    assert sid.startswith("codex_myproj_")  # agent + dir + random token


def test_prefix_env_override():
    os.environ["COGNEE_SESSION_PREFIX"] = "custom"  # a non-default value, to prove override
    try:
        assert pc._generate_session_id("/x", "abc123") == "custom_abc123"
    finally:
        os.environ.pop("COGNEE_SESSION_PREFIX", None)


def test_codex_cwd_used_in_fallback():
    """Verify CODEX_CWD (not CLAUDE_CWD) is the env var for the working-directory fallback."""
    os.environ.pop("COGNEE_SESSION_PREFIX", None)
    os.environ.pop("CLAUDE_CWD", None)
    os.environ["CODEX_CWD"] = "/tmp/codex_project"
    try:
        sid = pc._generate_session_id("", "")
        assert sid.startswith("codex_codex_project_")
    finally:
        os.environ.pop("CODEX_CWD", None)


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
