"""Synthetic stdin hook-payload builders covering every hook type.

Field names are verified against the hook scripts (session-start.py,
store-to-session.py, session-context-lookup.py, store-user-prompt.py,
sync-session-to-graph.py). Each builder returns a plain dict and accepts
``**overrides`` to tweak or add fields. Use ``dumps`` to serialize for stdin, or
pass the dict straight to ``run_hook(stdin=...)`` which serializes for you.
"""

from __future__ import annotations

import json
from typing import Any

DEFAULT_SESSION_ID = "cc_test_session"
DEFAULT_CWD = "/tmp/cognee-test-project"


def session_start(
    session_id: str = DEFAULT_SESSION_ID,
    cwd: str = DEFAULT_CWD,
    source: str = "startup",  # startup | resume | clear
    **overrides: Any,
) -> dict[str, Any]:
    payload = {
        "hook_event_name": "SessionStart",
        "session_id": session_id,
        "cwd": cwd,
        "source": source,
    }
    payload.update(overrides)
    return payload


def user_prompt(
    session_id: str = DEFAULT_SESSION_ID,
    cwd: str = DEFAULT_CWD,
    prompt: str = "What did we decide about the API?",
    model: str | None = None,
    turn_id: str | None = None,
    transcript_path: str | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session_id,
        "cwd": cwd,
        "prompt": prompt,
    }
    if model is not None:
        payload["model"] = model
    if turn_id is not None:
        payload["turn_id"] = turn_id
    if transcript_path is not None:
        payload["transcript_path"] = transcript_path
    payload.update(overrides)
    return payload


def post_tool_use(
    session_id: str = DEFAULT_SESSION_ID,
    tool_name: str = "Bash",
    tool_input: Any = None,
    tool_response: Any = "command output",
    tool_output: Any = "command output",
    **overrides: Any,
) -> dict[str, Any]:
    # Both tool_response and tool_output are read by the scripts (intentional
    # fallbacks) — populate both.
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input if tool_input is not None else {"command": "echo hi"},
        "tool_response": tool_response,
        "tool_output": tool_output,
    }
    payload.update(overrides)
    return payload


def stop(
    session_id: str = DEFAULT_SESSION_ID,
    assistant_message: str = "Here is the answer.",
    **overrides: Any,
) -> dict[str, Any]:
    # Scripts read several fallbacks for the assistant text; set them all.
    payload = {
        "hook_event_name": "Stop",
        "session_id": session_id,
        "assistant_message": assistant_message,
        "last_assistant_message": assistant_message,
        "message": assistant_message,
    }
    payload.update(overrides)
    return payload


def session_end(
    session_id: str = DEFAULT_SESSION_ID,
    reason: str = "exit",
    **overrides: Any,
) -> dict[str, Any]:
    payload = {
        "hook_event_name": "SessionEnd",
        "session_id": session_id,
        "reason": reason,
    }
    payload.update(overrides)
    return payload


def dumps(payload: dict[str, Any]) -> str:
    return json.dumps(payload)
