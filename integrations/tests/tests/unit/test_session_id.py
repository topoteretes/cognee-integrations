"""Tests for the `{agent}_{host_session_id}` Cognee session-id convention.

The host session id is embedded so the Cognee session maps 1:1 to the
conversation and is self-describing in the dashboard (no working-directory
coupling). Migrated from claude-code/tests/test_session_id.py, parametrized
over both suites (the prefix is the only per-suite difference).
"""

from __future__ import annotations


def test_embeds_host_session_id(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    sid = pc._generate_session_id("/tmp/whatever", "c92cc618-cc37-42ac")
    assert sid == f"{suite.session_prefix}_c92cc618-cc37-42ac"


def test_fallback_without_host_id_uses_agent_and_dir(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    sid = pc._generate_session_id("/tmp/myproj", "")
    assert sid.startswith(f"{suite.session_prefix}_myproj_")  # agent + dir + random token


def test_prefix_env_override(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    # A non-default value, to prove the override wins over the suite prefix.
    monkeypatch.setenv("COGNEE_SESSION_PREFIX", "custom")
    assert pc._generate_session_id("/x", "abc123") == "custom_abc123"
