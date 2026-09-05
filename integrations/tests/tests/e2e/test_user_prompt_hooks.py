"""Both UserPromptSubmit hooks exit cleanly and emit well-formed output.

A too-short prompt takes the no-op path, which is the case most likely to regress
unnoticed: the hook still has to leave the host with parseable output (or, for
claude-code, deliberately nothing) rather than a traceback on stdout.

Migrated from codex/tests/test_user_prompt_hooks.py — claude-code had no
equivalent, and the two hosts' no-op contracts genuinely differ: codex always
prints an envelope, while claude-code's main() prints only when there is output.
"""

from __future__ import annotations

import json

import pytest

_HOOKS = ("session-context-lookup.py", "store-user-prompt.py")


@pytest.mark.parametrize("script", _HOOKS)
def test_noop_hook_exits_zero_with_parseable_output(suite, run_hook, payloads, script):
    result = run_hook(
        suite,
        script,
        # "no" is under the minimum prompt length, so main() takes the no-op path.
        stdin=payloads.user_prompt(prompt="no", session_id="test"),
    )
    assert result.returncode == 0, result.stderr

    stdout = result.stdout.strip()
    if not stdout:
        # claude-code prints nothing when there is no context to inject.
        assert suite.name == "claude-code", f"{suite.name} must always emit an envelope"
        return

    output = json.loads(stdout)
    assert output["hookSpecificOutput"] == {
        "hookEventName": "UserPromptSubmit",
        "additionalContext": "",
    }


def test_stop_reads_latest_assistant_text_from_claude_transcript(
    suite, hook_module, tmp_path
):
    """Current Claude Stop payloads provide a transcript path, not answer text."""
    if suite.name != "claude-code":
        pytest.skip("Claude transcript fallback is host-specific")

    transcript = tmp_path / "session.jsonl"
    records = [
        {"type": "user", "message": {"role": "user", "content": "Question"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "Persist this final answer."},
                ],
            },
        },
        {"type": "attachment", "attachment": {"type": "command-permissions"}},
    ]
    transcript.write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
    )

    store = hook_module(suite, "store-to-session.py")
    payload = {"transcript_path": str(transcript)}
    assert store._assistant_message(payload) == "Persist this final answer."
