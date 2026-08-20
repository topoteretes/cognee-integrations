"""Both UserPromptSubmit hooks exit cleanly and emit well-formed output.

A too-short prompt takes the no-op path, which is the case most likely to regress
unnoticed: the hook still has to leave the host with parseable output (or, for
claude-code, deliberately nothing) rather than a traceback on stdout.

Migrated from codex/tests/test_user_prompt_hooks.py — Claude Code had no
equivalent, and the host no-op contracts genuinely differ: Codex and Antigravity
always print an envelope, while Claude Code's main() prints only when there is
output.
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
