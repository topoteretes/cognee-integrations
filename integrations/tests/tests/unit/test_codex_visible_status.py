"""Visible Cognee status for Codex app-server clients.

Codex hosts do not expose a reliable capability signal for hook
``systemMessage`` rendering. The plugin therefore asks Codex, in every host, to
end the answer with the status header as a one-line receipt.

Contract:
  * only a turn whose header reaches the model gets a receipt (a hit, or a
    recall skipped by an outage); an empty recall keeps the header out of the
    model's context (SDK-1071) and gets none;
  * the receipt instruction takes the header's place, so the header is never
    in the context twice, and the recalled memory is left as it was;
  * the receipt is the answer's last line, below any memory credit line;
  * the Stop hook strips the receipt before the answer is stored, and leaves
    a credit line alone.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys

from utils.suites import CODEX


def _lookup(hook_module):
    return hook_module(CODEX, "session-context-lookup.py")


def test_orca_transcript_enables_visible_status(hook_module, monkeypatch):
    lookup = _lookup(hook_module)
    monkeypatch.delenv("COGNEE_CODEX_VISIBLE_STATUS", raising=False)
    payload = {
        "transcript_path": (
            "/Users/me/Library/Application Support/orca/codex-accounts/account/home/"
            "sessions/rollout.jsonl"
        )
    }
    assert lookup._visible_status_enabled(payload)


def test_regular_codex_transcript_enables_visible_status_by_default(hook_module, monkeypatch):
    lookup = _lookup(hook_module)
    monkeypatch.delenv("COGNEE_CODEX_VISIBLE_STATUS", raising=False)
    assert lookup._visible_status_enabled(
        {"transcript_path": "/Users/me/.codex/sessions/rollout.jsonl"}
    )


def test_missing_transcript_enables_visible_status_by_default(hook_module, monkeypatch):
    lookup = _lookup(hook_module)
    monkeypatch.delenv("COGNEE_CODEX_VISIBLE_STATUS", raising=False)
    assert lookup._visible_status_enabled({})


def test_explicit_setting_overrides_default(hook_module, monkeypatch):
    lookup = _lookup(hook_module)
    orca = {
        "transcript_path": (
            "C:\\Users\\me\\Application Support\\orca\\codex-accounts\\account\\rollout.jsonl"
        )
    }
    monkeypatch.setenv("COGNEE_CODEX_VISIBLE_STATUS", "off")
    assert not lookup._visible_status_enabled(orca)
    monkeypatch.setenv("COGNEE_CODEX_VISIBLE_STATUS", "on")
    assert lookup._visible_status_enabled({})


def _hit_output(header: str, memory: str = "private recalled context") -> dict:
    return {
        "systemMessage": header,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": f"{header}\n\n{memory}",
        },
    }


_HEADER = (
    "● cognee: agent_sessions · local\n"
    "Cognee memory: 2 memory hits · 1/1 turns had hits this session"
)
_RECEIPT = (
    "> 🧠 ● cognee: agent_sessions · local · "
    "Cognee memory: 2 memory hits · 1/1 turns had hits this session"
)


def test_receipt_replaces_header_and_preserves_private_context(hook_module):
    lookup = _lookup(hook_module)

    actual = lookup._with_visible_status_receipt(_hit_output(_HEADER), True)
    context = actual["hookSpecificOutput"]["additionalContext"]

    assert _RECEIPT in context
    assert context.count("Cognee memory: 2 memory hits") == 1
    assert context.endswith("private recalled context")
    assert "very last line" in context and "credit line directly above" in context
    assert "this client hides" not in context
    assert actual["systemMessage"] == _HEADER


def test_empty_recall_gets_no_receipt(hook_module):
    lookup = _lookup(hook_module)
    output = {
        "systemMessage": _HEADER,
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "dataset hint",
        },
    }

    actual = lookup._with_visible_status_receipt(output, True)

    assert actual["hookSpecificOutput"]["additionalContext"] == "dataset hint"


def test_outage_context_carries_the_header_once(hook_module):
    lookup = _lookup(hook_module)
    header = "✕ (unreachable) cognee: ds · cloud\nCognee memory: recall skipped"
    output = {
        "systemMessage": header,
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": header},
    }

    context = lookup._with_visible_status_receipt(output, True)["hookSpecificOutput"][
        "additionalContext"
    ]

    assert "> 🧠 ✕ (unreachable) cognee: ds · cloud · Cognee memory: recall skipped" in context
    assert context.count("recall skipped") == 1


def test_disabled_receipt_leaves_output_unchanged(hook_module):
    lookup = _lookup(hook_module)
    output = _hit_output(_HEADER)

    actual = lookup._with_visible_status_receipt(output, False)

    assert actual["hookSpecificOutput"]["additionalContext"].startswith(_HEADER)


def test_main_applies_receipt_for_regular_codex_payload(hook_module, monkeypatch):
    lookup = _lookup(hook_module)
    monkeypatch.delenv("COGNEE_CODEX_VISIBLE_STATUS", raising=False)
    payload = {
        "session_id": "codex_session",
        "turn_id": "turn-1",
        "prompt": "show me the status",
        "cwd": "/repo",
        "transcript_path": "/Users/me/.codex/sessions/rollout.jsonl",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(
        lookup, "resolve_session_key_from_payload", lambda value: ("session", "payload.session_id")
    )
    monkeypatch.setattr(lookup, "set_session_key", lambda value: None)
    monkeypatch.setattr(lookup, "get_session_key", lambda: "session")
    monkeypatch.setattr(lookup, "hook_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(lookup, "with_base_url_notice", lambda output, event: output)

    async def fake_run(prompt, cwd):
        return _hit_output("cognee: ds · local\nCognee memory: 1 memory hit", "memory")

    monkeypatch.setattr(lookup, "_run", fake_run)
    output = lookup.main()
    assert (
        "> 🧠 cognee: ds · local · Cognee memory: 1 memory hit"
        in output["hookSpecificOutput"]["additionalContext"]
    )


def test_strip_receipt_keeps_answer_and_credit_line(hook_module):
    common = hook_module(CODEX, "store-to-session.py")
    answer = (
        "The fix is in.\n\n"
        "> 🧠 Cognee memory [m1]: the routing bug, from an earlier session\n"
        f"{_RECEIPT}\n"
    )

    stripped = common.strip_status_receipt(answer)

    assert stripped == (
        "The fix is in.\n\n> 🧠 Cognee memory [m1]: the routing bug, from an earlier session"
    )
    failed = "Done.\n> 🧠 ✕ (unreachable) cognee: ds · cloud · Cognee memory: recall skipped"
    assert common.strip_status_receipt(failed) == "Done."
    assert common.strip_status_receipt("no receipt here") == "no receipt here"


def test_stop_hook_stores_answer_without_receipt(hook_module, monkeypatch):
    store = hook_module(CODEX, "store-to-session.py")
    stored = []
    monkeypatch.setenv("COGNEE_CAPTURE", "true")
    monkeypatch.setattr(store, "_load_session", lambda: ("session", "ds", "user"))
    monkeypatch.setattr(store, "resolve_runtime_mode", lambda: {"base_url": "http://x"})
    monkeypatch.setattr(store, "pop_pending_prompt", lambda *a, **k: {"prompt": "q"})
    monkeypatch.setattr(store, "server_usable", lambda url: True)
    monkeypatch.setattr(
        store,
        "remember_entry_via_http",
        lambda dataset, session_id, entry: stored.append(entry) or None,
    )

    asyncio.run(store._store_assistant_stop({"assistant_message": f"The answer.\n\n{_RECEIPT}"}))

    assert [entry["answer"] for entry in stored] == ["The answer."]
