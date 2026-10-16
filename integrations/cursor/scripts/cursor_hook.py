#!/usr/bin/env python3
"""Adapt Cursor hook payloads to the Cognee hook script contract.

Cursor (https://cursor.com/docs/hooks) spawns one process per hook, writes one
JSON payload to stdin and reads one JSON reply from stdout. The Cognee hook
scripts in this directory are shared with the Claude Code and Codex plugins and
speak the Claude Code contract instead: a PascalCase ``hook_event_name``,
``session_id``, ``prompt``, ``tool_name`` / ``tool_input`` / ``tool_response``,
``assistant_message``, and a reply carrying
``hookSpecificOutput.additionalContext`` / ``systemMessage``.

This module is the boundary between the two:

* ``normalize_payload`` maps Cursor's fields (``conversation_id``,
  ``generation_id``, ``workspace_roots``, ``tool_output``, ``Shell`` ...) onto the
  Cognee contract. The prompt/answer pair is stored from ``afterAgentResponse``
  (the one Cursor hook that reliably fires at the end of a turn and carries the
  answer ``text``); it runs the inner ``Stop`` hook. Cursor's own ``stop`` is a
  fallback only: it skips when the turn was already stored and otherwise takes
  the answer from the tail of Cursor's JSONL transcript.
* ``run_inner_hook`` launches the inner script through ``hook_runner.py`` (so a
  crash is reported to ``hook-crash.log`` instead of vanishing) with a bounded
  timeout and a kill of the whole process tree on expiry.
* ``translate_output`` turns the Cognee reply into the shape Cursor documents
  for the hook: ``additional_context`` on ``sessionStart`` / ``postToolUse``,
  ``continue: true`` (plus the recalled context) on ``beforeSubmitPrompt``,
  nothing at all on ``stop`` — a Cognee hook must never auto-submit a follow-up.

Every path fails open: on any error the adapter prints the hook's neutral reply
and exits 0, so memory trouble can never block the agent.
"""

from __future__ import annotations

import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable

MAX_TRANSCRIPT_TAIL_BYTES = 1_048_576
MAX_INNER_OUTPUT_BYTES = 1_048_576
PROCESS_CLEANUP_SECONDS = 2.0

#: Cursor hook event -> the Claude Code event name the inner scripts understand.
EVENT_MAP = {
    "sessionStart": "SessionStart",
    "beforeSubmitPrompt": "UserPromptSubmit",
    "postToolUse": "PostToolUse",
    "postToolUseFailure": "PostToolUse",
    "afterAgentResponse": "AfterAgentResponse",
    "stop": "Stop",
    "preCompact": "PreCompact",
    "sessionEnd": "SessionEnd",
}

#: Cursor tool names -> the Claude Code names the capture policy and the trace
#: store were written against. Anything not listed passes through unchanged;
#: ``MCP:<tool>`` becomes ``mcp__<tool>`` (Claude Code's MCP tool prefix).
TOOL_NAME_MAP = {
    "Shell": "Bash",
    "Task": "Agent",
}

#: Inner script -> the Claude Code event it expects when no event is given.
EVENT_FOR_SCRIPT = {
    "session-start.py": "SessionStart",
    "session-context-lookup.py": "UserPromptSubmit",
    "store-user-prompt.py": "UserPromptSubmit",
    "store-to-session.py": "PostToolUse",
    "credits-refresh.py": "Stop",
    "pre-compact.py": "PreCompact",
    "sync-session-to-graph.py": "SessionEnd",
}

#: Seconds each inner script may run. Kept under the hooks.json ``timeout`` so
#: the adapter, not Cursor, ends a slow hook and still prints a neutral reply.
SCRIPT_TIMEOUT_SECONDS = {
    "session-start.py": 110.0,
    "session-context-lookup.py": 110.0,
    "store-user-prompt.py": 110.0,
    "store-to-session.py": 110.0,
    "credits-refresh.py": 8.0,
    "pre-compact.py": 110.0,
    "sync-session-to-graph.py": 110.0,
}

#: What ``hooks/hooks.json`` registers, event by event: (script, flags, timeout).
#: ``install-cursor-hooks.py`` renders the same table into a user or project
#: ``hooks.json``, and the contract test checks the plugin manifest against it.
HOOK_TABLE: dict[str, list[tuple[str, tuple[str, ...], int]]] = {
    "sessionStart": [("session-start.py", (), 120)],
    "beforeSubmitPrompt": [
        ("session-context-lookup.py", (), 120),
        ("store-user-prompt.py", (), 120),
    ],
    "postToolUse": [("store-to-session.py", (), 120)],
    "postToolUseFailure": [("store-to-session.py", (), 120)],
    # The answer is stored here: afterAgentResponse is the end-of-turn hook
    # Cursor's IDE actually fires (its ``stop`` was never observed to launch,
    # IDE 3.16.17 / CLI 2026.09.26) and it carries the answer ``text``.
    "afterAgentResponse": [("store-to-session.py", ("--stop",), 120)],
    # Fallback only: skipped when afterAgentResponse already stored the turn.
    "stop": [
        ("store-to-session.py", ("--stop",), 120),
        ("credits-refresh.py", (), 10),
    ],
    "preCompact": [("pre-compact.py", (), 120)],
    "sessionEnd": [("sync-session-to-graph.py", ("--session-end",), 120)],
}

_PROMPT_SCRIPTS = frozenset({"session-context-lookup.py", "store-user-prompt.py"})


def plugin_root() -> Path:
    return Path(__file__).resolve().parent.parent


def state_dir() -> Path:
    """The Cursor plugin's private state dir (matches ``config.py``)."""
    return Path.home() / ".cognee-plugin" / "cursor"


def neutral_reply(event: str) -> dict[str, Any]:
    """The reply that changes nothing for ``event``."""
    return {"continue": True} if event == "UserPromptSubmit" else {}


ADAPTER_LOG_NAME = "adapter.log"
_ADAPTER_LOG_MAX_BYTES = 2 * 1024 * 1024


def adapter_log(detail: dict[str, Any], root: Path | None = None) -> None:
    """Append one JSON line to ``~/.cognee-plugin/cursor/adapter.log``; never raises.

    Cursor shows hook launches only in the IDE's *Hooks* output channel, and the
    inner scripts log nothing when they are skipped, so this is the one record
    of *which Cursor hooks actually fired* with what session/turn — the first
    thing to check when a stage seems missing. Kept separate from ``hook.log``
    (whose writers hold a lock) and bounded by truncation.
    """
    try:
        base = root or state_dir()
        base.mkdir(parents=True, exist_ok=True)
        path = base / ADAPTER_LOG_NAME
        try:
            if path.stat().st_size > _ADAPTER_LOG_MAX_BYTES:
                path.write_text("", encoding="utf-8")
        except OSError:
            pass
        record = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pid": os.getpid(), **detail}
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Payload normalization
# --------------------------------------------------------------------------- #


def _first_str(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _resolve_cwd(payload: dict[str, Any]) -> str:
    cwd = _first_str(payload, "cwd")
    if cwd:
        return cwd
    roots = payload.get("workspace_roots")
    if isinstance(roots, list):
        for root in roots:
            if isinstance(root, str) and root.strip():
                return root
    return os.environ.get("CURSOR_PROJECT_DIR") or os.getcwd()


def map_tool_name(name: object) -> str:
    text = str(name or "")
    if text.startswith("MCP:"):
        return "mcp__" + text[4:]
    return TOOL_NAME_MAP.get(text, text)


def _parse_json_ish(value: object) -> Any:
    """Cursor ships ``tool_input`` / ``tool_output`` as JSON strings at times."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "["):
            try:
                return json.loads(stripped)
            except (json.JSONDecodeError, ValueError):
                return value
    return value


def infer_event(payload: dict[str, Any], script: str, flags: tuple[str, ...]) -> str:
    """The Claude Code event for this invocation."""
    if script == "store-to-session.py" and "--stop" in flags:
        # The QA store runs from ``afterAgentResponse`` (and ``stop``); either
        # way the inner script must see Claude Code's ``Stop``.
        return "Stop"
    native = payload.get("hook_event_name")
    if isinstance(native, str) and native in EVENT_MAP:
        return EVENT_MAP[native]
    if isinstance(native, str) and native in EVENT_MAP.values():
        return native
    return EVENT_FOR_SCRIPT.get(script, "")


def normalize_payload(
    payload: dict[str, Any], script: str, flags: tuple[str, ...] = ()
) -> dict[str, Any]:
    """Map a Cursor hook payload onto the Cognee (Claude Code) hook contract."""
    event = infer_event(payload, script, flags)
    normalized: dict[str, Any] = {"hook_event_name": event, "host": "cursor"}

    session = _first_str(payload, "session_id", "conversation_id")
    if session:
        normalized["session_id"] = session
    turn = _first_str(payload, "generation_id")
    if turn:
        normalized["turn_id"] = turn
    transcript = _first_str(payload, "transcript_path") or os.environ.get(
        "CURSOR_TRANSCRIPT_PATH", ""
    )
    if transcript:
        normalized["transcript_path"] = transcript
    normalized["cwd"] = _resolve_cwd(payload)
    for key in ("model", "model_id", "cursor_version", "user_email"):
        if isinstance(payload.get(key), str) and payload[key]:
            normalized[key] = payload[key]

    if event == "SessionStart":
        normalized["source"] = "startup"
        for key in ("is_background_agent", "composer_mode"):
            if key in payload:
                normalized[key] = payload[key]

    elif event == "UserPromptSubmit":
        prompt = payload.get("prompt")
        if isinstance(prompt, str):
            normalized["prompt"] = prompt

    elif event == "PostToolUse":
        normalized["tool_name"] = map_tool_name(payload.get("tool_name"))
        tool_input = _parse_json_ish(payload.get("tool_input"))
        normalized["tool_input"] = tool_input if isinstance(tool_input, dict) else {}
        if "tool_output" in payload:
            normalized["tool_response"] = _parse_json_ish(payload.get("tool_output"))
        else:
            normalized["tool_response"] = ""
        if _first_str(payload, "tool_use_id"):
            normalized["tool_call_id"] = payload["tool_use_id"]
        if isinstance(payload.get("duration"), (int, float)):
            normalized["duration_ms"] = payload["duration"]
        if payload.get("hook_event_name") == "postToolUseFailure":
            failure = _first_str(payload, "failure_type") or "error"
            message = _first_str(payload, "error_message") or failure
            normalized["error"] = message
            normalized["failure_type"] = failure

    elif event == "Stop":
        normalized["stop_hook_active"] = False
        if isinstance(payload.get("status"), str):
            normalized["status"] = payload["status"]
        message = _first_str(payload, "assistant_message", "last_assistant_message", "text")
        if not message and payload.get("hook_event_name") != "afterAgentResponse":
            # Cursor's stop carries no text. If afterAgentResponse already
            # stored this turn, leave the message empty so the hook is skipped
            # instead of storing the answer twice; otherwise recover it from
            # the transcript tail. (An afterAgentResponse without text has
            # nothing to store and is skipped as well.)
            if consume_stored_marker(session, turn):
                normalized["answer_already_stored"] = True
            else:
                message = last_assistant_text(
                    read_transcript_tail(normalized.get("transcript_path"))
                )
        if message:
            normalized["assistant_message"] = message
            normalized["last_assistant_message"] = message

    elif event == "AfterAgentResponse":
        if isinstance(payload.get("text"), str):
            normalized["text"] = payload["text"]

    elif event == "PreCompact":
        if isinstance(payload.get("trigger"), str):
            normalized["trigger"] = payload["trigger"]

    elif event == "SessionEnd":
        if isinstance(payload.get("reason"), str):
            normalized["reason"] = payload["reason"]

    return normalized


# --------------------------------------------------------------------------- #
# Stored-turn marker (afterAgentResponse -> stop) and transcript-tail recovery
# --------------------------------------------------------------------------- #


def _stored_marker_path(session: str, root: Path | None = None) -> Path:
    key = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(session))
    key = key.strip("_")[:120] or "unknown"
    base = root if root is not None else state_dir()
    return base / "responses" / f"{key}.stored"


def mark_answer_stored(session: str, turn: str, root: Path | None = None) -> Path | None:
    """Record that ``afterAgentResponse`` stored the QA pair for this turn."""
    if not session:
        return None
    path = _stored_marker_path(session, root)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(str(turn or ""), encoding="utf-8")
        os.replace(tmp, path)
        return path
    except OSError:
        return None


def consume_stored_marker(session: str, turn: str, root: Path | None = None) -> bool:
    """True when the marker says this turn's answer is already stored.

    The marker is removed either way: a marker for a *different* turn is stale
    (that turn's ``stop`` never came) and must not suppress a later one.
    """
    if not session:
        return False
    path = _stored_marker_path(session, root)
    try:
        stored_turn = path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    try:
        path.unlink()
    except OSError:
        pass
    return not stored_turn or not turn or stored_turn == str(turn)


def read_transcript_tail(transcript_path: object) -> list[dict[str, Any]]:
    """Decode a bounded tail of Cursor's JSONL transcript (``{"role", "message"}``)."""
    if not transcript_path:
        return []

    descriptor = -1
    try:
        path = Path(os.fspath(transcript_path)).expanduser()
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_BINARY", 0)
        descriptor = os.open(path, flags)
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size == 0:
            return []
        start = max(0, file_stat.st_size - MAX_TRANSCRIPT_TAIL_BYTES)
        with os.fdopen(descriptor, "rb") as transcript:
            descriptor = -1
            if start:
                transcript.seek(start - 1)
                previous_byte = transcript.read(1)
            else:
                previous_byte = b"\n"
            raw = transcript.read(MAX_TRANSCRIPT_TAIL_BYTES)
    except (OSError, TypeError, ValueError):
        return []
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    if start and previous_byte != b"\n":
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


def _text_blocks(record: dict[str, Any]) -> list[str]:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        return [content] if content.strip() else []
    if not isinstance(content, list):
        return []
    texts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                texts.append(text)
    return texts


def last_assistant_text(records: list[dict[str, Any]]) -> str:
    """The final assistant message of the last turn, or ''."""
    last_user = -1
    for index in range(len(records) - 1, -1, -1):
        if records[index].get("role") == "user":
            last_user = index
            break
    for record in reversed(records[last_user + 1 :]):
        if record.get("role") != "assistant":
            continue
        texts = _text_blocks(record)
        if texts:
            return "\n\n".join(texts).strip()
    return ""


# --------------------------------------------------------------------------- #
# Output translation
# --------------------------------------------------------------------------- #


def _extract(output: dict[str, Any], key: str) -> str:
    hook_output = output.get("hookSpecificOutput")
    value = hook_output.get(key) if isinstance(hook_output, dict) else None
    if not isinstance(value, str) or not value.strip():
        value = output.get(key)
    return value.strip() if isinstance(value, str) and value.strip() else ""


def translate_output(event: str, content: object) -> dict[str, Any]:
    """Turn a Cognee hook reply into what Cursor documents for ``event``."""
    reply = neutral_reply(event)
    if isinstance(content, dict):
        output = content
    else:
        try:
            output = json.loads(content) if isinstance(content, str) and content.strip() else {}
        except (json.JSONDecodeError, TypeError, ValueError):
            output = {}
    if not isinstance(output, dict):
        return reply

    context = _extract(output, "additionalContext")
    message = _extract(output, "systemMessage")

    if event in ("SessionStart", "UserPromptSubmit"):
        # Cursor has no user-facing channel for a non-blocking hook message, so a
        # notice (memory off, update available) rides along in the model's
        # context where the agent can relay it. The recall hook repeats its
        # header as the systemMessage; skip the note when the context has it.
        if message and message not in context:
            note = f"[cognee notice] {message}"
            context = f"{context}\n\n{note}" if context else note
        if context:
            reply["additional_context"] = context
            if event == "UserPromptSubmit":
                # The nested Claude Code shape is what Cursor's third-party hook
                # layer already accepts for UserPromptSubmit context injection.
                reply["hookSpecificOutput"] = {
                    "hookEventName": "UserPromptSubmit",
                    "additionalContext": context,
                }
    elif event == "PostToolUse":
        if context:
            reply["additional_context"] = context
    elif event == "PreCompact":
        if message:
            reply["user_message"] = message
    # Stop, SessionEnd, AfterAgentResponse: nothing is ever forwarded. In
    # particular no ``followup_message`` / ``decision`` may leak through.
    return reply


# --------------------------------------------------------------------------- #
# Inner script execution
# --------------------------------------------------------------------------- #


def _script_timeout(script: str) -> float:
    limit = SCRIPT_TIMEOUT_SECONDS.get(script, 110.0)
    raw_override = os.environ.get("COGNEE_CURSOR_HOOK_TIMEOUT_SECONDS", "")
    try:
        override = float(raw_override)
    except ValueError:
        return limit
    return min(limit, override) if override > 0 else limit


def _terminate_process_tree(process: subprocess.Popen) -> None:
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    elif os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=PROCESS_CLEANUP_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()


def _bounded_wait(process: subprocess.Popen) -> None:
    try:
        process.wait(timeout=PROCESS_CLEANUP_SECONDS)
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=PROCESS_CLEANUP_SECONDS)
        except subprocess.TimeoutExpired:
            pass


def _read_inner_output(output: BinaryIO) -> str:
    output.flush()
    output.seek(0)
    return output.read(MAX_INNER_OUTPUT_BYTES).decode("utf-8", errors="replace")


def inner_command(script: str, flags: tuple[str, ...]) -> list[str]:
    scripts_dir = Path(__file__).resolve().parent
    return [
        sys.executable,
        str(scripts_dir / "hook_runner.py"),
        str(scripts_dir / script),
        *flags,
    ]


def inner_environment(payload: dict[str, Any]) -> dict[str, str]:
    env = dict(os.environ)
    root = str(plugin_root())
    env.setdefault("PLUGIN_ROOT", root)
    env.setdefault("CURSOR_PLUGIN_ROOT", root)
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        env["CURSOR_CWD"] = cwd
    return env


def _run_script(payload: dict[str, Any], script: str, flags: tuple[str, ...]) -> str:
    if script not in SCRIPT_TIMEOUT_SECONDS:
        raise ValueError(f"unsupported inner hook: {script}")

    command = inner_command(script, flags)
    process_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        process_kwargs["start_new_session"] = True
    elif os.name == "nt":
        process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    timeout = _script_timeout(script)
    # Two statements rather than one parenthesized ``with``: the hooks must
    # still parse on Python 3.9.
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
            env=inner_environment(payload),
            **process_kwargs,
        )
        timed_out = False
        try:
            process.communicate(json.dumps(payload).encode(), timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _terminate_process_tree(process)
            _bounded_wait(process)
        finally:
            if process.stdin is not None and not process.stdin.closed:
                process.stdin.close()
        stdout = _read_inner_output(stdout_file)
        stderr = _read_inner_output(stderr_file)

    if timed_out:
        raise subprocess.TimeoutExpired(command, timeout, output=stdout, stderr=stderr)
    if stderr:
        sys.stderr.write(stderr)
    if process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
    return stdout


Runner = Callable[[dict[str, Any], str, tuple[str, ...]], Any]


def run_inner_hook(
    payload: dict[str, Any],
    script: str,
    flags: tuple[str, ...] = (),
    *,
    runner: Runner | None = None,
) -> Any:
    """Run one inner hook through ``hook_runner.py`` (or ``runner`` in tests)."""
    return (runner or _run_script)(payload, script, flags)


def stores_the_answer(cursor_event: str, script: str, flags: tuple[str, ...]) -> bool:
    """Is this the ``afterAgentResponse`` QA store whose turn ``stop`` must skip?"""
    if cursor_event != "afterAgentResponse" or script != "store-to-session.py":
        return False
    return "--stop" in flags


def should_skip(normalized: dict[str, Any], script: str, flags: tuple[str, ...]) -> bool:
    """Skip inner hooks that would only log a missing field."""
    if script in _PROMPT_SCRIPTS and not str(normalized.get("prompt") or "").strip():
        return True
    if script == "store-to-session.py" and "--stop" in flags:
        if not normalized.get("assistant_message"):
            return True
    return False


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in EVENT_FOR_SCRIPT:
        print("{}")
        return 0
    script = args[0]
    flags = tuple(arg for arg in args[1:] if arg.startswith("--"))

    event = EVENT_FOR_SCRIPT.get(script, "")
    reply = neutral_reply(event)
    started = time.monotonic()
    record: dict[str, Any] = {"event": "adapter.invoked", "script": script, "flags": list(flags)}
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        record["cursor_event"] = str(payload.get("hook_event_name") or "")
        record["session"] = str(payload.get("conversation_id") or payload.get("session_id") or "")
        record["turn"] = str(payload.get("generation_id") or "")
        normalized = normalize_payload(payload, script, flags)
        event = normalized["hook_event_name"]
        record["inner_event"] = event
        if normalized.get("tool_name"):
            record["tool"] = normalized["tool_name"]
        reply = neutral_reply(event)
        if should_skip(normalized, script, flags):
            record["outcome"] = "skipped"
            if normalized.get("answer_already_stored"):
                record["skip_reason"] = "answer_already_stored"
        else:
            output = run_inner_hook(normalized, script, flags)
            reply = translate_output(event, output)
            record["outcome"] = "ran"
            record["reply_keys"] = sorted(reply)
            if stores_the_answer(record["cursor_event"], script, flags):
                mark_answer_stored(record["session"], record["turn"])
    except Exception as exc:  # fail open, always
        record["outcome"] = "failed"
        record["error"] = f"{type(exc).__name__}: {exc}"[:200]
        try:
            sys.stderr.write(f"cognee-cursor: {script} failed: {type(exc).__name__}: {exc}\n")
        except Exception:
            pass
        reply = neutral_reply(event)

    record["ms"] = round((time.monotonic() - started) * 1000)
    adapter_log(record)
    print(json.dumps(reply))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
