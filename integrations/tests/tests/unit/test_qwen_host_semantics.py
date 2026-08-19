"""Qwen-specific hook protocol seams that the shared suite cannot infer."""

from __future__ import annotations

import io
import json
import sys

from utils.suites import QWEN, state_dir


def _set_stdin(monkeypatch, payload: dict) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))


def test_precompact_anchor_uses_qwen_additional_context_envelope(hook_module, monkeypatch, capsys):
    """Qwen ignores raw PreCompact stdout; the anchor must be structured context."""
    module = hook_module(QWEN, "pre-compact.py")
    anchor = "## Cognee Memory Anchor\nkeep this context"

    async def return_anchor():
        return anchor

    monkeypatch.setattr(module, "_run", return_anchor)
    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "PreCompact",
            "session_id": "qwen-session",
            "trigger": "auto",
        },
    )

    module.main()

    assert json.loads(capsys.readouterr().out) == {
        "hookSpecificOutput": {
            "hookEventName": "PreCompact",
            "additionalContext": anchor,
        }
    }


def test_internal_qwen_continuation_does_not_replace_submitted_prompt(
    hook_module, monkeypatch, temp_home
):
    """ToolResult/Hook sends lack provenance and must not replace the user turn."""
    module = hook_module(QWEN, "store-user-prompt.py")
    monkeypatch.setenv("COGNEE_IDLE_DISABLED", "1")
    monkeypatch.setattr(
        module,
        "_load_session",
        lambda: ("cognee-session", "agent_sessions", "user", "tenant"),
    )
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(
        module,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "http://127.0.0.1:9"},
    )
    monkeypatch.setattr(module, "server_usable", lambda _url: False)

    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "qwen-host-session",
            "prompt": "expanded prompt plus injected context",
            "submitted_prompt": "original user question",
        },
    )
    module.main()

    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "qwen-host-session",
            "prompt": '{"functionResponse":{"name":"read_file"}}',
        },
    )
    module.main()

    pending_files = list((state_dir(QWEN, temp_home) / "pending").glob("*.json"))
    assert len(pending_files) == 1
    pending = json.loads(pending_files[0].read_text(encoding="utf-8"))
    assert {entry["prompt"] for entry in pending.values()} == {"original user question"}


def test_qwen_recall_uses_submitted_prompt_provenance(hook_module, monkeypatch):
    """Recall must query the user's text, not the model-bound expanded prompt."""
    module = hook_module(QWEN, "session-context-lookup.py")
    queries = []

    async def capture_query(prompt):
        queries.append(prompt)
        return None

    monkeypatch.setattr(module, "_run", capture_query)
    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "qwen-host-session",
            "prompt": "expanded prompt plus injected context",
            "submitted_prompt": "original user question",
        },
    )

    module.main()

    assert queries == ["original user question"]


def test_qwen_recall_skips_internal_continuations_without_provenance(hook_module, monkeypatch):
    """ToolResult/Hook sends are not user questions and must not trigger recall."""
    module = hook_module(QWEN, "session-context-lookup.py")
    queries = []

    async def capture_query(prompt):
        queries.append(prompt)
        return None

    monkeypatch.setattr(module, "_run", capture_query)
    _set_stdin(
        monkeypatch,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "qwen-host-session",
            "prompt": '{"functionResponse":{"name":"read_file"}}',
        },
    )

    module.main()

    assert queries == []
