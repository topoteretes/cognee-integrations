"""Tests for `_apply_memory_preference` (session-start.py) — asserting Cognee as
the preferred memory over the host's own built-in auto memory.

The SessionStart output gets an `additionalContext` block telling the agent to
consult Cognee FIRST, without disturbing any field already in the envelope. Opt
out with `COGNEE_PREFER_MEMORY=false`.

claude-code only: the steer exists because Claude Code ships its own MEMORY.md
auto-memory that would otherwise compete. Migrated from
claude-code/tests/test_memory_preference.py, whose `ss is None` guard reported
skipped tests as PASS, and which read the developer's real config.json (the
isolated import now supplies a temp HOME).
"""

from __future__ import annotations

import pytest


@pytest.fixture
def ss(suite, hook_module):
    module = hook_module(suite, "session-start.py")
    if not hasattr(module, "_apply_memory_preference"):
        pytest.skip(f"{suite.name}: no built-in auto-memory to steer away from")
    return module


def test_steer_injected_by_default(ss):
    out = ss._apply_memory_preference(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "systemMessage": "hi"}}
    )
    hook_specific = out["hookSpecificOutput"]
    assert "Cognee" in hook_specific["additionalContext"]
    assert "FIRST" in hook_specific["additionalContext"]  # "consult Cognee FIRST"
    assert hook_specific["systemMessage"] == "hi"  # existing fields preserved
    assert hook_specific["hookEventName"] == "SessionStart"


def test_empty_output_gets_session_start_block(ss):
    out = ss._apply_memory_preference({})
    hook_specific = out["hookSpecificOutput"]
    assert hook_specific["hookEventName"] == "SessionStart"
    assert "memory" in hook_specific["additionalContext"].lower()


def test_opt_out_disables_steer(ss, monkeypatch):
    monkeypatch.setenv("COGNEE_PREFER_MEMORY", "false")
    out = ss._apply_memory_preference({"hookSpecificOutput": {"hookEventName": "SessionStart"}})
    assert "additionalContext" not in out["hookSpecificOutput"]
