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

    async def fake_run(prompt, cwd, **kwargs):
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


@pytest.mark.parametrize(
    "prompt",
    [
        "<task-notification><summary>Tests finished</summary></task-notification>",
        "<system-reminder>Use the task tool</system-reminder>",
        "<scheduled-task>Run the hourly summary</scheduled-task>",
        "<cross-session-message>Another agent finished</cross-session-message>",
        "<system-reminder>Incomplete notification",
        "[SUGGESTION MODE: Suggest the user's next message]",
        "[Request interrupted by user for tool use]",
        "# Files mentioned by the user\n/path/to/file.py",
    ],
)
def test_machine_only_prompts_do_not_recall(suite, hook_module, monkeypatch, prompt):
    hook = hook_module(suite, "session-context-lookup.py")
    assert _run(hook, monkeypatch, prompt) == []


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        (
            "<system-reminder>Context</system-reminder>\nFind the earlier decision",
            "Find the earlier decision",
        ),
        (
            "# Files mentioned by the user\n/path.py\n"
            "## My request for Codex:\nExplain this decision",
            "Explain this decision",
        ),
        ("Explain what <system-reminder> means", "Explain what <system-reminder> means"),
        ("Try again", "Try again"),
    ],
)
def test_user_text_is_preserved(suite, hook_module, monkeypatch, prompt, expected):
    hook = hook_module(suite, "session-context-lookup.py")
    assert _run(hook, monkeypatch, prompt) == [expected]


def test_non_user_role_is_not_recalled(suite, isolated_modules):
    gate = isolated_modules(suite, "_recall_prompt")
    assert gate.recall_user_prompt({"role": "assistant", "prompt": "The task is finished"}) == ""


def test_machine_notification_does_not_consume_coding_memory_turn(suite, hook_module, monkeypatch):
    hook = hook_module(suite, "session-context-lookup.py")
    if not hasattr(hook, "coding_memory_enabled"):
        pytest.skip("This host has no coding-memory delivery mode")
    monkeypatch.setattr(hook, "coding_memory_enabled", lambda: True)

    def unexpected(*args, **kwargs):
        pytest.fail("machine notification reached coding-memory delivery")

    monkeypatch.setattr(hook, "_coding_prompt", unexpected)
    assert _run(hook, monkeypatch, "<task-notification>Done</task-notification>") == []
