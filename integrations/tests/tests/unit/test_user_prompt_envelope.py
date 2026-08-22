"""Contract tests for the UserPromptSubmit hook output envelope.

Both suites emit `hookSpecificOutput.hookEventName == "UserPromptSubmit"` with
the recalled text in `additionalContext`, but the **status header goes in a
different place**, and each host only reads its own:

  * codex puts `systemMessage` at the TOP level (and must NOT nest it);
  * claude-code puts `systemMessage` INSIDE `hookSpecificOutput`.

Getting this wrong is silent — the hook still exits 0 and the host just shows no
status — so it is worth pinning per suite. Migrated from
codex/tests/test_user_prompt_hooks.py, which existed only on the codex side even
though the claude contract is equally load-bearing (claude had no
UserPromptSubmit output coverage at all).
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    """session-context-lookup.py with every resolution seam stubbed."""
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "load_config", lambda *a, **k: {})
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": ""})
    monkeypatch.setattr(module, "server_ready_hint", lambda _url: True)
    monkeypatch.setattr(module, "get_session_key", lambda: "session")
    monkeypatch.setattr(
        module,
        "read_and_reset_save_counter",
        lambda _session: {"prompt": 0, "trace": 0, "answer": 0},
    )
    monkeypatch.setattr(module, "recall_via_http", lambda *a, **k: [])
    if hasattr(module, "render_status_for_host"):
        monkeypatch.setattr(module, "render_status_for_host", lambda _session: "Cognee")
    return module


def test_event_name_is_always_declared(lookup):
    output = asyncio.run(lookup._run("remember this"))
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_status_header_sits_where_this_host_reads_it(suite, lookup):
    output = asyncio.run(lookup._run("remember this"))
    hook_specific = output["hookSpecificOutput"]

    if hasattr(lookup, "render_status_for_host"):  # codex: top level
        assert output["systemMessage"].startswith("Cognee")
        assert "systemMessage" not in hook_specific
    else:  # claude-code: nested
        assert "systemMessage" in hook_specific
        assert "systemMessage" not in output
