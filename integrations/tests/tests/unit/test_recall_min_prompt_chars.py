"""Recall skips prompts shorter than COGNEE_RECALL_MIN_PROMPT_CHARS.

The lookup hook keeps the stock 5-character floor by default; a host may raise
it so acknowledgements and one-word nudges do not cost a recall and an injected
context block. Capture (store-user-prompt) is unaffected and keeps its own gate.
"""

import io
import json

import pytest


def _run(hook, monkeypatch, prompt, **env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(hook, "resolve_session_key_from_payload", lambda payload: ("key", "test"))
    monkeypatch.setattr(hook, "set_session_key", lambda value: None)
    monkeypatch.setattr(hook, "get_session_key", lambda: "key")
    recalled = []

    async def fake_run(prompt, cwd):
        recalled.append(prompt)
        return None

    monkeypatch.setattr(hook, "_run", fake_run)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"prompt": prompt, "cwd": "."})))
    hook.main()
    return recalled


def test_default_floor_matches_the_stock_gate(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    assert _run(hook, monkeypatch, "12345") == ["12345"]
    assert _run(hook, monkeypatch, "1234") == []


def test_raised_floor_skips_short_prompts_only(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    env = {"COGNEE_RECALL_MIN_PROMPT_CHARS": "20"}
    assert _run(hook, monkeypatch, "Try again", **env) == []
    assert _run(hook, monkeypatch, "what remains outstanding?", **env) == [
        "what remains outstanding?"
    ]


def test_whitespace_does_not_count_toward_the_floor(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    padded = "go" + " " * 30
    assert _run(hook, monkeypatch, padded, COGNEE_RECALL_MIN_PROMPT_CHARS="20") == []


@pytest.mark.parametrize("raw", ["", "abc", "-3", "2"])
def test_invalid_or_low_values_fall_back_to_the_stock_gate(suite, hook_module, monkeypatch, raw):
    hook = hook_module(suite, "session-context-lookup.py")
    assert _run(hook, monkeypatch, "12345", COGNEE_RECALL_MIN_PROMPT_CHARS=raw) == ["12345"]
    assert _run(hook, monkeypatch, "1234", COGNEE_RECALL_MIN_PROMPT_CHARS=raw) == []


def test_unset_floor_keeps_the_stock_gate_exactly(suite, hook_module, monkeypatch):
    """The stock gate counts whitespace; unset, the new floor must not change that."""
    hook = hook_module(suite, "session-context-lookup.py")
    monkeypatch.delenv("COGNEE_RECALL_MIN_PROMPT_CHARS", raising=False)
    assert _run(hook, monkeypatch, "1234 ") == ["1234 "]
