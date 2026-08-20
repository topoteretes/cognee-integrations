#!/usr/bin/env python3
"""Adapt Antigravity hook payloads to the Cognee hook script contract."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

MAX_TRANSCRIPT_TAIL_BYTES = 1_048_576

EVENT_FOR_SCRIPT = {
    "session-start.py": "SessionStart",
    "session-context-lookup.py": "UserPromptSubmit",
    "store-user-prompt.py": "UserPromptSubmit",
    "store-to-session.py": "PostToolUse",
    "sync-session-to-graph.py": "SessionEnd",
}

_PROMPT_SCRIPTS = frozenset({"session-context-lookup.py", "store-user-prompt.py"})


def default_marker_root() -> Path:
    """Return the private, Antigravity-specific once-marker directory."""
    return Path.home() / ".cognee-plugin" / "antigravity" / "adapter-once"


DEFAULT_MARKER_ROOT = default_marker_root()


def read_transcript_tail(transcript_path: object) -> list[dict[str, Any]]:
    """Read and decode a bounded tail of JSON-object transcript records."""
    if not transcript_path:
        return []

    try:
        path = Path(os.fspath(transcript_path))
        file_stat = path.stat()
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size == 0:
            return []

        start = max(0, file_stat.st_size - MAX_TRANSCRIPT_TAIL_BYTES)
        with path.open("rb") as transcript:
            transcript.seek(start)
            raw = transcript.read(MAX_TRANSCRIPT_TAIL_BYTES)
    except (OSError, TypeError, ValueError):
        return []

    if start:
        newline = raw.find(b"\n")
        if newline < 0:
            return []
        raw = raw[newline + 1 :]

    records: list[dict[str, Any]] = []
    for line in raw.splitlines():
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _is_user_record(record: dict[str, Any]) -> bool:
    return (
        record.get("source") == "USER_EXPLICIT"
        and record.get("type") == "USER_INPUT"
        and record.get("status") == "DONE"
    )


def _is_assistant_record(record: dict[str, Any]) -> bool:
    return (
        record.get("source") == "MODEL"
        and record.get("type") == "PLANNER_RESPONSE"
        and record.get("status") == "DONE"
        and bool(record.get("content"))
    )


def _is_tool_call_record(record: dict[str, Any]) -> bool:
    return record.get("source") == "MODEL" and bool(record.get("tool_calls"))


def _same_step_index(record: dict[str, Any], requested_step: object) -> bool:
    try:
        return int(record.get("step_index")) == int(requested_step)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _latest_user(records: list[dict[str, Any]]) -> tuple[int, dict[str, Any]] | None:
    for index in range(len(records) - 1, -1, -1):
        if _is_user_record(records[index]):
            return index, records[index]
    return None


def _extract_tool_event(
    normalized: dict[str, Any], records: list[dict[str, Any]], requested_step: object
) -> None:
    result_index = next(
        (
            index
            for index in range(len(records) - 1, -1, -1)
            if _same_step_index(records[index], requested_step)
        ),
        None,
    )
    if result_index is None:
        return

    result = records[result_index]
    call_record = next(
        (record for record in reversed(records[:result_index]) if _is_tool_call_record(record)),
        None,
    )
    if call_record is None:
        return

    calls = call_record.get("tool_calls")
    if not isinstance(calls, list):
        return
    candidates = [call for call in calls if isinstance(call, dict)]
    if not candidates:
        return

    tool_call_id = result.get("tool_call_id")
    call = next((item for item in candidates if item.get("id") == tool_call_id), candidates[0])
    normalized["tool_name"] = call.get("name", "")
    normalized["tool_input"] = call.get("args", {})
    normalized["tool_response"] = result.get("content", "")
    if "exit_code" in result:
        normalized["exit_code"] = result["exit_code"]
    if "error" in result:
        normalized["error"] = result["error"]


def normalize_payload(
    payload: dict[str, Any], script: str, *, stop: bool = False
) -> dict[str, Any]:
    """Map Antigravity fields and transcript events to the Cognee contract."""
    event = "Stop" if script == "store-to-session.py" and stop else EVENT_FOR_SCRIPT[script]
    normalized: dict[str, Any] = {"hook_event_name": event}

    common_fields = (
        ("session_id", "conversationId"),
        ("transcript_path", "transcriptPath"),
        ("model", "modelName"),
    )
    for canonical, antigravity in common_fields:
        if canonical in payload:
            normalized[canonical] = payload[canonical]
        elif antigravity in payload:
            normalized[canonical] = payload[antigravity]

    if "cwd" in payload:
        normalized["cwd"] = payload["cwd"]
    else:
        workspaces = payload.get("workspacePaths")
        if isinstance(workspaces, list) and workspaces:
            normalized["cwd"] = workspaces[0]

    if "fullyIdle" in payload:
        normalized["fullyIdle"] = payload["fullyIdle"]

    records = read_transcript_tail(normalized.get("transcript_path"))
    latest_user = _latest_user(records)
    if latest_user is not None:
        user_index, user_record = latest_user
        if "step_index" in user_record:
            normalized["turn_id"] = str(user_record["step_index"])
        if script in _PROMPT_SCRIPTS and isinstance(user_record.get("content"), str):
            normalized["prompt"] = user_record["content"]
    else:
        user_index = -1

    if event == "PostToolUse":
        _extract_tool_event(normalized, records, payload.get("stepIdx"))
    elif event == "Stop" and latest_user is not None:
        assistant = next(
            (
                record
                for record in reversed(records[user_index + 1 :])
                if _is_assistant_record(record)
            ),
            None,
        )
        if assistant is not None:
            message = assistant["content"]
            normalized["assistant_message"] = message
            normalized["last_assistant_message"] = message

    return normalized


def translate_stdout(content: str) -> dict[str, Any]:
    """Translate Cognee recall output to Antigravity's context injection shape."""
    try:
        output = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return {}
    if not isinstance(output, dict):
        return {}

    hook_output = output.get("hookSpecificOutput")
    context = hook_output.get("additionalContext") if isinstance(hook_output, dict) else None
    if not context:
        context = output.get("systemMessage")
    if not isinstance(context, str) or not context:
        return {}
    return {"injectSteps": [{"ephemeralMessage": context}]}


def _marker_path(payload: dict[str, Any], script: str, root: Path) -> Path | None:
    conversation = payload.get("session_id") or payload.get("conversationId")
    if not conversation:
        return None

    if script == "session-start.py":
        identity = ("antigravity-adapter-v1", "conversation", str(conversation))
    elif script in _PROMPT_SCRIPTS:
        turn_id = payload.get("turn_id")
        if turn_id is None:
            return None
        identity = (
            "antigravity-adapter-v1",
            "turn",
            str(conversation),
            str(turn_id),
            script,
        )
    else:
        return None

    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return root / f"{digest}.done"


Runner = Callable[[dict[str, Any], str], Any]


def _run_script(payload: dict[str, Any], script: str) -> str:
    if script not in EVENT_FOR_SCRIPT:
        raise ValueError(f"unsupported inner hook: {script}")

    command = [sys.executable, str(Path(__file__).with_name(script))]
    if payload.get("hook_event_name") == "Stop":
        command.append("--stop")
    elif payload.get("hook_event_name") == "SessionEnd":
        command.append("--session-end")

    completed = subprocess.run(
        command,
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        raise subprocess.CalledProcessError(
            completed.returncode,
            command,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.stdout


def run_inner_hook(
    payload: dict[str, Any],
    script: str,
    *,
    runner: Runner = _run_script,
    marker_dir: Path | None = None,
    session_end: bool = False,
) -> Any:
    """Run an inner hook, enforcing final-idle and scoped once semantics."""
    if session_end and payload.get("fullyIdle") is not True:
        return {}

    marker_root = Path(marker_dir) if marker_dir is not None else default_marker_root()
    marker = _marker_path(payload, script, marker_root)
    if marker is not None and marker.is_file():
        return {}

    output = runner(payload, script)
    if marker is not None:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.touch(exist_ok=True)
    return output


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in EVENT_FOR_SCRIPT:
        print("{}")
        return 0

    script = args[0]
    stop = "--stop" in args[1:]
    session_end = "--session-end" in args[1:]
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        normalized = normalize_payload(payload, script, stop=stop)
        output = run_inner_hook(normalized, script, session_end=session_end)
        content = output if isinstance(output, str) else json.dumps(output)
        translated = translate_stdout(content)
    except Exception:
        translated = {}

    print(json.dumps(translated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
