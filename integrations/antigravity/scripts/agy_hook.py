#!/usr/bin/env python3
"""Adapt Antigravity hook payloads to the Cognee hook script contract."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, Callable

MAX_TRANSCRIPT_TAIL_BYTES = 1_048_576
MAX_INNER_OUTPUT_BYTES = 1_048_576

EVENT_FOR_SCRIPT = {
    "session-start.py": "SessionStart",
    "session-context-lookup.py": "UserPromptSubmit",
    "store-user-prompt.py": "UserPromptSubmit",
    "store-to-session.py": "PostToolUse",
    "sync-session-to-graph.py": "Stop",
}

_PROMPT_SCRIPTS = frozenset({"session-context-lookup.py", "store-user-prompt.py"})
_STOP_SCRIPTS = frozenset({"store-to-session.py", "sync-session-to-graph.py"})
SCRIPT_TIMEOUT_SECONDS = {
    "session-start.py": 110.0,
    "session-context-lookup.py": 110.0,
    "store-user-prompt.py": 110.0,
    "store-to-session.py": 110.0,
    "sync-session-to-graph.py": 25.0,
}
PROCESS_CLEANUP_SECONDS = 2.0


def default_marker_root() -> Path:
    """Return the private, Antigravity-specific once-marker directory."""
    return Path.home() / ".cognee-plugin" / "antigravity" / "adapter-once"


DEFAULT_MARKER_ROOT = default_marker_root()


def read_transcript_tail(transcript_path: object) -> list[dict[str, Any]]:
    """Read and decode a bounded tail of JSON-object transcript records."""
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
    if "tool_response" not in normalized:
        normalized["tool_response"] = result.get("content", "")
    if "exit_code" in result and "exit_code" not in normalized:
        normalized["exit_code"] = result["exit_code"]
    if "error" in result and "error" not in normalized:
        normalized["error"] = result["error"]

    # Parallel tools may complete after newer calls have been recorded. Match
    # the result's identity across the tail, never borrow an unrelated call.
    tool_call_id = result.get("tool_call_id")
    for record in reversed(records[:result_index]):
        if not _is_tool_call_record(record):
            continue
        calls = record.get("tool_calls")
        if not isinstance(calls, list):
            continue
        candidates = [call for call in calls if isinstance(call, dict)]
        if tool_call_id:
            call = next((item for item in candidates if item.get("id") == tool_call_id), None)
        else:
            call = candidates[0] if len(candidates) == 1 else None
        if call is not None:
            normalized.setdefault("tool_name", call.get("name", ""))
            normalized.setdefault("tool_input", call.get("args", {}))
            return
        if not tool_call_id:
            return


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

    execution_id = payload.get("execution_id", payload.get("executionId"))
    if execution_id not in (None, ""):
        normalized["execution_id"] = execution_id

    if payload.get("turn_id") not in (None, ""):
        normalized["turn_id"] = str(payload["turn_id"])
    elif execution_id not in (None, ""):
        normalized["turn_id"] = str(execution_id)

    if isinstance(payload.get("prompt"), str):
        normalized["prompt"] = payload["prompt"]
    elif isinstance(payload.get("lastUserInput"), str):
        normalized["prompt"] = payload["lastUserInput"]

    if "cwd" in payload:
        normalized["cwd"] = payload["cwd"]
    else:
        workspaces = payload.get("workspacePaths")
        if isinstance(workspaces, list) and workspaces:
            normalized["cwd"] = workspaces[0]

    if "fullyIdle" in payload:
        normalized["fullyIdle"] = payload["fullyIdle"]

    if event == "PostToolUse":
        if payload.get("stepIdx") not in (None, ""):
            normalized["tool_step_id"] = str(payload["stepIdx"])
        tool_call = payload.get("toolCall")
        if isinstance(tool_call, dict):
            if tool_call.get("id") not in (None, ""):
                normalized["tool_call_id"] = str(tool_call["id"])
            if "name" in tool_call:
                normalized["tool_name"] = tool_call["name"]
            if "args" in tool_call:
                normalized["tool_input"] = tool_call["args"]
        if "result" in payload:
            normalized["tool_response"] = payload["result"]
        if "error" in payload:
            normalized["error"] = payload["error"]

    if event == "Stop":
        native_output = payload.get(
            "assistant_message",
            payload.get("last_assistant_message", payload.get("finalModelOutput")),
        )
        if isinstance(native_output, str) and native_output:
            normalized["assistant_message"] = native_output
            normalized["last_assistant_message"] = native_output

    records = read_transcript_tail(normalized.get("transcript_path"))
    latest_user = _latest_user(records)
    if latest_user is not None:
        user_index, user_record = latest_user
        if "turn_id" not in normalized and "step_index" in user_record:
            normalized["turn_id"] = str(user_record["step_index"])
        if (
            script in _PROMPT_SCRIPTS
            and "prompt" not in normalized
            and isinstance(user_record.get("content"), str)
        ):
            normalized["prompt"] = user_record["content"]
    else:
        user_index = -1

    # Current hosts document executionNum on Stop, rather than executionId.
    # It identifies an execution attempt, not the user turn used by the prompt
    # buffer. Include the transcript turn because execution counters can reset.
    execution_num = payload.get("executionNum")
    if event == "Stop" and execution_id in (None, "") and execution_num not in (None, ""):
        turn = normalized.get("turn_id", "")
        normalized["execution_id"] = f"turn:{turn}:execution:{execution_num}"

    if event == "PostToolUse":
        _extract_tool_event(normalized, records, payload.get("stepIdx"))
    elif event == "Stop" and "assistant_message" not in normalized and latest_user is not None:
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
    elif payload.get("hook_event_name") == "Stop" and script in _STOP_SCRIPTS:
        execution_id = payload.get("execution_id")
        if execution_id in (None, ""):
            return None
        identity = (
            "antigravity-adapter-v1",
            "execution-stop",
            str(conversation),
            str(execution_id),
            script,
        )
    elif script == "store-to-session.py" and payload.get("hook_event_name") == "PostToolUse":
        tool_id = payload.get("tool_step_id") or payload.get("tool_call_id")
        if tool_id is None:
            return None
        identity = ("antigravity-adapter-v1", "tool", str(conversation), str(tool_id))
    else:
        return None

    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return root / f"{digest}.done"


Claim = tuple[Path, BinaryIO]


def _lock_claim(handle: BinaryIO) -> bool:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
    else:
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
    return True


def _unlock_claim(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _close_locked_claim(handle: BinaryIO) -> None:
    try:
        _unlock_claim(handle)
    except OSError:
        pass
    finally:
        handle.close()


def _release_claim(claim: Claim) -> None:
    path, handle = claim
    if os.name != "nt":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
    _close_locked_claim(handle)
    if os.name == "nt":
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A new owner opened the stable claim between unlock and unlink.
            pass


def _bootstrap_owner_pid(payload: dict[str, Any], root: Path) -> int:
    """Read the host owner recorded by SessionStart without importing its runtime."""
    key = str(payload.get("session_id") or payload.get("conversationId") or "")
    key = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in key)
    key = key.strip("._")[:120]
    if not key:
        return 0
    try:
        record = json.loads((root.parent / "sessions" / f"{key}.json").read_text(encoding="utf-8"))
        return int(record.get("host_pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0


def _bootstrap_completed(marker: Path) -> bool:
    if not marker.is_file():
        return False
    try:
        owner = int(marker.read_text(encoding="utf-8").strip())
        if owner > 0:
            from _proc import pid_alive

            return pid_alive(owner)
    except (OSError, ValueError, ImportError):
        pass
    # Older or ownerless markers preserve the original once-per-conversation
    # behavior; never re-bootstrap a live conversation on an unreadable record.
    return True


def _acquire_claim(
    marker: Path, *, completed: Callable[[Path], bool] = Path.is_file
) -> Claim | None:
    """Acquire crash-released ownership of a stable, hash-only claim file."""
    marker.parent.mkdir(parents=True, exist_ok=True)
    claim = marker.with_suffix(".claim")
    if completed(marker):
        return None

    for _attempt in range(3):
        descriptor = os.open(claim, os.O_CREAT | os.O_RDWR, 0o600)
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        if os.fstat(descriptor).st_size == 0:
            handle.write(b"0")
        if not _lock_claim(handle):
            handle.close()
            return None

        try:
            same_generation = os.path.samestat(os.fstat(descriptor), claim.stat())
        except (FileNotFoundError, OSError):
            same_generation = False
        if not same_generation:
            _close_locked_claim(handle)
            continue

        ownership = (claim, handle)
        if completed(marker):
            _release_claim(ownership)
            return None
        return ownership
    return None


Runner = Callable[[dict[str, Any], str], Any]


def _script_timeout(script: str) -> float:
    limit = SCRIPT_TIMEOUT_SECONDS[script]
    raw_override = os.environ.get("COGNEE_AGY_HOOK_TIMEOUT_SECONDS", "")
    try:
        override = float(raw_override)
    except ValueError:
        return limit
    return min(limit, override) if override > 0 else limit


def _terminate_process_tree(process: subprocess.Popen[bytes]) -> None:
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


def _bounded_wait(process: subprocess.Popen[bytes]) -> None:
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


def _run_script(payload: dict[str, Any], script: str) -> str:
    if script not in EVENT_FOR_SCRIPT:
        raise ValueError(f"unsupported inner hook: {script}")

    command = [sys.executable, str(Path(__file__).with_name(script))]
    if script == "store-to-session.py" and payload.get("hook_event_name") == "Stop":
        command.append("--stop")
    elif script == "sync-session-to-graph.py" and payload.get("hook_event_name") == "Stop":
        command.append("--execution-stop")

    process_kwargs: dict[str, Any] = {}
    if os.name == "posix":
        process_kwargs["start_new_session"] = True
    elif os.name == "nt":
        process_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    timeout = _script_timeout(script)
    with (
        tempfile.TemporaryFile(mode="w+b") as stdout_file,
        tempfile.TemporaryFile(mode="w+b") as stderr_file,
    ):
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=stdout_file,
            stderr=stderr_file,
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
        raise subprocess.CalledProcessError(
            process.returncode,
            command,
            output=stdout,
            stderr=stderr,
        )
    return stdout


def run_inner_hook(
    payload: dict[str, Any],
    script: str,
    *,
    runner: Runner = _run_script,
    marker_dir: Path | None = None,
    claim_stale_after: float | None = None,
) -> Any:
    """Run an inner hook with conversation-, turn-, or execution-scoped once semantics."""
    if (
        script == "store-to-session.py"
        and payload.get("hook_event_name") == "Stop"
        and payload.get("fullyIdle") is False
    ):
        return {}
    if (
        script == "sync-session-to-graph.py"
        and payload.get("hook_event_name") == "Stop"
        and payload.get("fullyIdle") is not True
    ):
        return {}

    marker_root = Path(marker_dir) if marker_dir is not None else default_marker_root()
    marker = _marker_path(payload, script, marker_root)
    del claim_stale_after  # Kept for compatibility; OS ownership replaces time-based leases.
    claim = None
    if marker is not None:
        completed = _bootstrap_completed if script == "session-start.py" else Path.is_file
        claim = _acquire_claim(marker, completed=completed)
        if claim is None:
            return {}

    try:
        output = runner(payload, script)
        if marker is not None:
            if script == "session-start.py":
                marker.write_text(str(_bootstrap_owner_pid(payload, marker_root)), encoding="utf-8")
            else:
                marker.touch(exist_ok=True)
        return output
    finally:
        if claim is not None:
            _release_claim(claim)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in EVENT_FOR_SCRIPT:
        print("{}")
        return 0

    script = args[0]
    stop = "--stop" in args[1:]
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        normalized = normalize_payload(payload, script, stop=stop)
        if script in _PROMPT_SCRIPTS and not normalized.get("prompt"):
            print("{}")
            return 0
        if script == "store-to-session.py" and stop and not normalized.get("assistant_message"):
            print("{}")
            return 0
        output = run_inner_hook(normalized, script)
        content = output if isinstance(output, str) else json.dumps(output)
        translated = translate_stdout(content)
    except Exception:
        translated = {}

    print(json.dumps(translated))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
