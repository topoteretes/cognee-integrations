"""Coding memory mode (``COGNEE_CODING_MEMORY=true``, SDK-1336).

An opt-in mode for the server's coding distillation (``DISTILLATION_MODE=coding``,
SDK-1335). With it on, the plugin does nothing while the agent works:

* **Capture at the end of each request.** The ``Stop`` hook reads the host's
  transcript and sends every tool call not sent yet (failed calls included) as a
  ``trace`` entry, then one ``qa`` entry with the request and the final message.
  Every entry carries the time of its event (``time``, and ``prompt_time`` on the
  QA entry), which the server uses to pair calls to requests. Sent calls are
  remembered by id, so a second ``Stop`` sends only what is new.
* **Delivery at the first request.** The first prompt of a session (and the first
  one after a compaction) gets one short block of lessons: the newest
  ``test-baseline``, the two newest pitfalls and the lessons recalled for the
  request, each tagged with what changed in git since its session, plus the last
  three commits.

The module is shared verbatim by the Claude Code and Codex plugins; ``AGENT``
selects the transcript format. It never raises into a hook: every entry point
fails open and logs through ``hook_log``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from _plugin_common import hook_log

#: Byte caps, as the per-call capture path uses them (store-to-session.py).
MAX_PARAMS_BYTES = 4000
MAX_RETURN_BYTES = 8000
MAX_ERROR_BYTES = 500
MAX_ANSWER_BYTES = 8000
MAX_PROMPT_CHARS = 8000
#: Calls sent per capture; the rest wait for the next Stop (a resumed session
#: with a long unsent history must not stall one hook).
MAX_TRACES_PER_CAPTURE = 400
#: Sent ids kept per session (bounded state file).
MAX_SENT_IDS = 20000

#: Delivery budgets (characters).
LESSON_BUDGET = 1500
FRAMING_CAP = 500
LESSON_LINES_CAP = 1600
COMMITS_CAP = 300
RECALL_MATCH_PREFIX = 120
SERVER_CALL_TIMEOUT = 4.0
DELIVERY_BUDGET_SECONDS = 10.0
MAX_LISTING_PAGES = 5
LISTING_PAGE_SIZE = 1000
#: Lesson bodies fetched on the prompt path when the cache misses them.
MAX_BODY_FETCHES_ON_PROMPT = 30
SUPPORT_TTL_SECONDS = 3600.0

FRAMING = (
    "Project memory (Cognee), learned from {sessions} in the {dataset} dataset. "
    "Check it before exploring: if a lesson answers your question, use it instead of "
    "searching. Lessons describe the code as of the session and commit they cite and "
    "name files changed since; confirm such a lesson with one cheap command (e.g. a "
    'single grep or the test run) rather than re-exploring. Pitfalls ("→ use:") are '
    "errors already hit and the remedy that worked."
)

_TITLE_RE = re.compile(r"^#\s*Session learning \(session ([^)]*)\)\s*$")
_HEADER_RE = re.compile(r"^(Lesson-Type|State-Key):\s*(.*)$")


# -- Setting -------------------------------------------------------------------


def enabled() -> bool:
    """True when ``COGNEE_CODING_MEMORY`` is on (env, or ~/.cognee/.env loaded into it)."""
    from config import coding_memory_enabled

    return coding_memory_enabled()


def agent() -> str:
    from _plugin_common import _PLUGIN_DIR

    return "codex" if Path(_PLUGIN_DIR).name == "codex" else "claude"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# -- State -----------------------------------------------------------------------


def _root() -> Path:
    from _plugin_common import _PLUGIN_DIR

    return Path(_PLUGIN_DIR) / "coding"


def _digest(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:32]


def state_path(session_key: str) -> Path:
    return _root() / "sessions" / (_digest(session_key) + ".json")


def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


class _SessionState:
    """The session's coding state under an exclusive lock (``with`` block)."""

    def __init__(self, session_key: str, timeout: float = 10.0):
        self.path = state_path(session_key)
        self.timeout = timeout
        self.data: dict = {}
        self._lock = None
        self.acquired = False

    def __enter__(self):
        from _file_lock import file_lock

        self._lock = file_lock(self.path.with_suffix(".lock"), timeout=self.timeout)
        self.acquired = self._lock.__enter__()
        self.data = _read_json(self.path) if self.acquired else {}
        return self

    def save(self) -> None:
        if self.acquired:
            _write_json(self.path, self.data)

    def __exit__(self, *exc):
        if self._lock is not None:
            self._lock.__exit__(*exc)
        return False


def read_state(session_key: str) -> dict:
    return _read_json(state_path(session_key))


# -- Server support ---------------------------------------------------------------


def _support_file() -> Path:
    return _root() / "server-support.json"


def _schema_props(spec: dict, name: str) -> set:
    schema = spec.get("components", {}).get("schemas", {}).get(name, {})
    return set(schema.get("properties", {})) if isinstance(schema, dict) else set()


def check_server_support(*, refresh: bool = False, timeout: float = 5.0) -> dict:
    """Whether the server takes the event times coding mode needs (cognee PR #6568).

    Reads ``/openapi.json``: ``QAEntry`` must have ``time`` and ``prompt_time`` and
    ``TraceEntry`` ``time``. The verdict is cached an hour per server. Returns
    ``{"ok": True|False|None, "reason": str}``; ``None`` means unknown (the server
    could not be asked), which callers treat as allowed so capture is buffered
    rather than lost.
    """
    from _plugin_common import _json_http_request, _local_api_url, _normalize_service_url

    url = _normalize_service_url(_local_api_url())
    cache = _read_json(_support_file())
    entry = cache.get(url) if isinstance(cache.get(url), dict) else None
    if entry and not refresh and time.time() - float(entry.get("at") or 0) < SUPPORT_TTL_SECONDS:
        return {"ok": entry.get("ok"), "reason": str(entry.get("reason") or "")}
    try:
        spec = _json_http_request("/openapi.json", None, method="GET", timeout=timeout)
    except Exception as exc:
        if entry:
            return {"ok": entry.get("ok"), "reason": str(entry.get("reason") or "")}
        return {"ok": None, "reason": "unreachable: " + str(exc)[:120]}
    spec = spec if isinstance(spec, dict) else {}
    qa, trace = _schema_props(spec, "QAEntry"), _schema_props(spec, "TraceEntry")
    ok = {"time", "prompt_time"} <= qa and "time" in trace
    reason = (
        "" if ok else "the server's QAEntry/TraceEntry take no event times (needs cognee PR #6568)"
    )
    cache[url] = {"ok": ok, "reason": reason, "at": time.time()}
    try:
        _write_json(_support_file(), cache)
    except OSError:
        pass
    if not ok:
        hook_log("coding_memory_server_unsupported", {"base_url": url, "reason": reason})
    return {"ok": ok, "reason": reason}


def server_supported() -> bool:
    """False only when the server is known to be too old (memory is then off)."""
    try:
        return check_server_support().get("ok") is not False
    except Exception as exc:
        hook_log("coding_memory_support_check_failed", {"error": str(exc)[:200]})
        return True


def delete_permission(dataset: str, *, timeout: float = 5.0):
    """Whether this key holds ``delete`` on ``dataset`` directly: True, False or None.

    The coding distillation deletes the lessons a newer one replaces, so it refuses
    to run without delete permission next to write. The server lists only grants
    held by the user itself, so a grant through a role or tenant reads as False
    here; None means the server could not be asked.
    """
    from _plugin_common import _json_http_request

    try:
        me = _json_http_request("/api/v1/users/me", None, method="GET", timeout=timeout)
        user_id = str((me or {}).get("id") or "")
        if not user_id:
            return None
        deadline = time.monotonic() + timeout
        dataset_id = resolve_dataset_id(dataset, deadline)
        rows = _json_http_request(
            f"/api/v1/permissions/principals/{user_id}/datasets?permission_name=delete",
            None,
            method="GET",
            timeout=timeout,
        )
    except Exception as exc:
        hook_log("coding_permission_check_failed", {"error": str(exc)[:200]})
        return None
    ids = {str(row.get("id")) for row in rows or [] if isinstance(row, dict)}
    names = {str(row.get("name")) for row in rows or [] if isinstance(row, dict)}
    return (dataset_id in ids) if dataset_id else (dataset in names)


def doctor_line(dataset: str) -> str:
    """One line for the doctor report."""
    if not enabled():
        return "off"
    support = check_server_support(refresh=True)
    if support.get("ok") is False:
        return "on, but memory is off: " + str(support.get("reason") or "server too old")
    if support.get("ok") is None:
        return "on (server not reachable: could not check it)"
    allowed = delete_permission(dataset)
    if allowed is True:
        return f"on (server supports it; delete permission on {dataset})"
    if allowed is False:
        return (
            f"on, but no direct delete permission on {dataset} was found: improve fails "
            "unless a role or tenant grants it"
        )
    return "on (server supports it; delete permission not checked)"


# -- Git -----------------------------------------------------------------------------


def _git(cwd: str, *args: str, timeout: float = 3.0) -> str:
    if not cwd:
        return ""
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except Exception:
        return ""
    return result.stdout if result.returncode == 0 else ""


def git_root(cwd: str) -> str:
    return _git(cwd, "rev-parse", "--show-toplevel").strip()


def git_head(cwd: str) -> str:
    return _git(cwd, "rev-parse", "HEAD").strip()


def git_branch(cwd: str) -> str:
    branch = _git(cwd, "rev-parse", "--abbrev-ref", "HEAD").strip()
    return "" if branch == "HEAD" else branch


# -- Session identity --------------------------------------------------------------


def _session() -> tuple[str, str]:
    """(cognee session id, dataset) of this launch, as the capture hooks resolve them."""
    from _plugin_common import load_resolved
    from config import get_dataset, get_session_id, load_config

    resolved = load_resolved(identity=False)
    session_id = str(resolved.get("session_id") or "")
    dataset = str(resolved.get("dataset") or "")
    if not session_id or not dataset:
        config = load_config()
        session_id = session_id or get_session_id(config)
        dataset = dataset or get_dataset(config)
    return session_id, dataset


# -- Request start (UserPromptSubmit) ------------------------------------------------


def begin_turn(session_key: str, prompt: str, cwd: str) -> None:
    """Record the request's start: text, time, start commit and branch.

    Runs in the synchronous prompt hook, so it is on disk before the agent starts
    and the Stop of the same request always finds it.
    """
    from _capture_policy import sanitize

    if not session_key:
        return
    turn = {
        "id": uuid.uuid4().hex,
        "prompt": str(sanitize(prompt or ""))[:MAX_PROMPT_CHARS],
        "prompt_time": now_iso(),
        "start_commit": git_head(cwd),
        "branch": git_branch(cwd),
        "cwd": cwd,
        "qa_sent": False,
    }
    with _SessionState(session_key) as state:
        if not state.acquired:
            hook_log("coding_state_locked", {"op": "begin_turn"})
            return
        state.data["turn"] = turn
        state.data.setdefault("started_at", time.time())
        state.save()


# -- Transcripts ---------------------------------------------------------------------


def _iter_jsonl(path: Path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError:
        return


def _block_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") in ("text", "input_text", "Text"):
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


_NOT_A_REQUEST = (
    "<command-",
    "<local-command",
    "<task-notification",
    "<system-reminder",
    "Caveat:",
)


def _claude_files(transcript: Path) -> list:
    files = [transcript]
    sub = transcript.with_suffix("") / "subagents"
    try:
        files.extend(sorted(sub.glob("*.jsonl")))
    except OSError:
        pass
    return files


def parse_claude(transcript_path: str) -> dict:
    """Tool calls (main thread and subagents), the last request and the last answer."""
    transcript = Path(str(transcript_path or "")).expanduser()
    uses: dict = {}
    results: dict = {}
    last_text, last_text_time = "", ""
    last_request, last_request_time = "", ""
    if not transcript.is_file():
        return {"calls": [], "answer": "", "request": "", "request_time": ""}
    for index, path in enumerate(_claude_files(transcript)):
        main = index == 0
        for record in _iter_jsonl(path):
            kind = record.get("type")
            message = record.get("message") if isinstance(record.get("message"), dict) else {}
            content = message.get("content")
            stamp = str(record.get("timestamp") or "")
            if kind == "assistant" and isinstance(content, list):
                texts = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_use" and block.get("id"):
                        uses[str(block["id"])] = {
                            "name": str(block.get("name") or "unknown"),
                            "input": block.get("input"),
                        }
                    elif block.get("type") == "text":
                        texts.append(str(block.get("text") or ""))
                text = "\n".join(t for t in texts if t.strip())
                if main and text.strip() and not record.get("isSidechain"):
                    last_text, last_text_time = text, stamp
            elif kind == "user":
                if isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result" for b in content
                ):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            tool_id = str(block.get("tool_use_id") or "")
                            if tool_id:
                                results[tool_id] = {
                                    "output": _block_text(block.get("content"))
                                    if not isinstance(block.get("content"), str)
                                    else block.get("content"),
                                    "is_error": bool(block.get("is_error")),
                                    "time": stamp,
                                }
                    continue
                if not main or record.get("isMeta") or record.get("isSidechain"):
                    continue
                if record.get("isCompactSummary"):
                    continue
                text = _block_text(content).strip()
                if text and not text.startswith(_NOT_A_REQUEST):
                    last_request, last_request_time = text, stamp
    calls = []
    for tool_id, result in results.items():
        use = uses.get(tool_id)
        if not use:
            continue
        calls.append(
            {
                "id": tool_id,
                "name": use["name"],
                "input": use["input"],
                "output": result["output"],
                "is_error": result["is_error"],
                "error": str(result["output"] or "") if result["is_error"] else "",
                "time": result["time"],
            }
        )
    calls.sort(key=lambda c: c["time"])
    return {
        "calls": calls,
        "answer": last_text,
        "answer_time": last_text_time,
        "request": last_request,
        "request_time": last_request_time,
    }


#: Codex thread items that are not tool calls.
_CODEX_SKIP = {
    "UserMessage",
    "AgentMessage",
    "Reasoning",
    "Plan",
    "HookPrompt",
    "ContextCompaction",
    "EnteredReviewMode",
    "ExitedReviewMode",
}
_FAILED = {"failed", "declined", "error", "errored", "cancelled", "canceled", "interrupted"}


def _codex_time(record: dict, payload: dict) -> str:
    millis = payload.get("completed_at_ms")
    if isinstance(millis, (int, float)) and millis > 0:
        return datetime.fromtimestamp(millis / 1000.0, timezone.utc).isoformat(
            timespec="milliseconds"
        )
    return str(record.get("timestamp") or "")


def _codex_command(command) -> str:
    if isinstance(command, list):
        parts = [str(p) for p in command]
        if (
            len(parts) >= 3
            and parts[-2] in ("-lc", "-c")
            and Path(parts[0]).name
            in (
                "bash",
                "zsh",
                "sh",
                "fish",
                "pwsh",
                "powershell",
            )
        ):
            return parts[-1]
        return " ".join(parts)
    return str(command or "")


def _codex_call(item: dict, stamp: str) -> dict | None:
    kind = str(item.get("type") or "")
    if not kind or kind in _CODEX_SKIP:
        return None
    status = str(item.get("status") or "").lower()
    failed = status in _FAILED
    if kind == "CommandExecution":
        exit_code = item.get("exit_code")
        failed = failed or exit_code not in (0, None)
        output = str(item.get("aggregated_output") or "")
        return {
            "name": "Bash",
            "input": {"command": _codex_command(item.get("command"))},
            "output": output,
            "is_error": failed,
            "error": (f"exit code {exit_code}: " if exit_code not in (0, None) else "")
            + (output[-MAX_ERROR_BYTES:] if failed else ""),
            "time": stamp,
        }
    if kind == "FileChange":
        changes = item.get("changes")
        return {
            "name": "Edit",
            "input": {"changes": changes},
            "output": status or "completed",
            "is_error": failed,
            "error": status if failed else "",
            "time": stamp,
        }
    if kind == "McpToolCall":
        name = "mcp__{}__{}".format(item.get("server") or "mcp", item.get("tool") or "tool")
        error = item.get("error")
        return {
            "name": name,
            "input": item.get("arguments"),
            "output": item.get("result"),
            "is_error": failed or bool(error),
            "error": str(error or (status if failed else "")),
            "time": stamp,
        }
    if kind == "WebSearch":
        return {
            "name": "WebSearch",
            "input": {"query": item.get("query")},
            "output": status or "completed",
            "is_error": failed,
            "error": status if failed else "",
            "time": stamp,
        }
    params = {k: v for k, v in item.items() if k not in ("id", "type", "status")}
    return {
        "name": kind,
        "input": params,
        "output": status or "completed",
        "is_error": failed,
        "error": status if failed else "",
        "time": stamp,
    }


def parse_codex(transcript_path: str) -> dict:
    """Tool calls, the last request and the last answer from a Codex rollout file."""
    path = Path(str(transcript_path or "")).expanduser()
    calls = []
    answer, answer_time = "", ""
    request, request_time = "", ""
    if not path.is_file():
        return {"calls": [], "answer": "", "request": "", "request_time": ""}
    for record in _iter_jsonl(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") != "event_msg":
            continue
        kind = payload.get("type")
        if kind == "task_complete" and payload.get("last_agent_message"):
            answer, answer_time = str(payload["last_agent_message"]), str(record.get("timestamp"))
            continue
        if kind != "item_completed":
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        stamp = _codex_time(record, payload)
        if item.get("type") == "UserMessage":
            text = _block_text(item.get("content")).strip()
            if text:
                request, request_time = text, stamp
            continue
        if item.get("type") == "AgentMessage":
            text = _block_text(item.get("content")).strip()
            if text:
                answer, answer_time = text, stamp
            continue
        call = _codex_call(item, stamp)
        if call and item.get("id"):
            call["id"] = str(item["id"])
            calls.append(call)
    calls.sort(key=lambda c: c["time"])
    return {
        "calls": calls,
        "answer": answer,
        "answer_time": answer_time,
        "request": request,
        "request_time": request_time,
    }


def parse_transcript(transcript_path: str) -> dict:
    return parse_codex(transcript_path) if agent() == "codex" else parse_claude(transcript_path)


# -- Entries ----------------------------------------------------------------------------


def _truncate(value, cap: int) -> str:
    if value is None:
        return ""
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)
    encoded = text.encode("utf-8", errors="replace")
    if len(encoded) <= cap:
        return encoded.decode("utf-8")
    return encoded[: cap - 3].decode("utf-8", errors="ignore") + "..."


def _event_time(stamp: str) -> str:
    """An aware ISO timestamp (the server rejects naive ones); "" when unparseable."""
    stamp = str(stamp or "").strip()
    if not stamp:
        return ""
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.isoformat()


def trace_entry(call: dict) -> dict:
    tool_input = call.get("input")
    if isinstance(tool_input, dict):
        params = {str(k): _truncate(v, MAX_PARAMS_BYTES) for k, v in tool_input.items()}
    else:
        params = {"value": _truncate(tool_input, MAX_PARAMS_BYTES)}
    entry = {
        "type": "trace",
        "origin_function": str(call.get("name") or "unknown"),
        "status": "error" if call.get("is_error") else "success",
        "method_params": params,
        "method_return_value": _truncate(call.get("output"), MAX_RETURN_BYTES),
        "error_message": _truncate(call.get("error"), MAX_ERROR_BYTES)
        if call.get("is_error")
        else "",
        "generate_feedback_with_llm": False,
    }
    stamp = _event_time(call.get("time"))
    if stamp:
        entry["time"] = stamp
    return entry


def _keep_call(call: dict) -> str:
    """ "" to send the call, else why it is dropped (it still counts as handled)."""
    from _capture_policy import allow_tool, is_plugin_invocation

    tool_input = call.get("input") if isinstance(call.get("input"), dict) else {}
    if not allow_tool(str(call.get("name") or ""), tool_input):
        return "not_allowed"
    if call.get("name") == "Bash" and is_plugin_invocation(str(tool_input.get("command") or "")):
        return "plugin_invocation"
    return ""


# -- Capture (Stop / SessionEnd) ----------------------------------------------------------


def capture(payload: dict, *, final: bool = False) -> dict:
    """Send what this session has not sent yet: new tool calls, then the QA entry.

    ``final`` (SessionEnd) only buffers: the final sync drains the buffer before
    its improve. Otherwise the buffer is drained here, and an improve is requested
    when the session's cooldown has passed. Returns a summary for the log.
    """
    from _capture_policy import CapturePatternError, capture_enabled, sanitize
    from _plugin_common import get_session_key

    if not capture_enabled():
        return {"skipped": "capture_disabled"}
    if not server_supported():
        return {"skipped": "server_unsupported"}
    session_key = get_session_key()
    if not session_key:
        return {"skipped": "no_session_key"}
    session_id, dataset = _session()
    if not session_id or not dataset:
        return {"skipped": "no_session"}
    parsed = parse_transcript(str(payload.get("transcript_path") or ""))
    cwd = str(payload.get("cwd") or "") or os.getcwd()

    entries: list = []
    summary = {"traces": 0, "dropped": 0, "qa": False, "continuation": False}
    with _SessionState(session_key) as state:
        if not state.acquired:
            hook_log("coding_state_locked", {"op": "capture"})
            return {"skipped": "locked"}
        data = state.data
        data.setdefault("started_at", time.time())
        sent = list(data.get("sent_ids") or [])
        sent_set = set(sent)
        handled = []
        try:
            for call in parsed["calls"]:
                if call["id"] in sent_set:
                    continue
                if len(handled) >= MAX_TRACES_PER_CAPTURE:
                    break
                handled.append(call["id"])
                reason = _keep_call(call)
                if reason:
                    summary["dropped"] += 1
                    continue
                entries.append(sanitize(trace_entry(call)))
        except CapturePatternError as exc:
            # Fail closed, but say so: a broken COGNEE_CAPTURE_TOOLS pattern.
            hook_log("coding_capture_pattern_invalid", {"error": str(exc)[:200]})
            entries, handled = [], []
        summary["traces"] = len(entries)

        answer = str(payload.get("last_assistant_message") or "").strip()
        if not answer or answer == "null":
            answer = str(parsed.get("answer") or "").strip()
        turn = data.get("turn") if isinstance(data.get("turn"), dict) else {}
        answer_hash = _digest(answer) if answer else ""
        if answer and answer_hash != data.get("last_answer_hash"):
            question = str(turn.get("prompt") or "") or str(parsed.get("request") or "")
            qa = {
                "type": "qa",
                "question": str(sanitize(question))[:MAX_PROMPT_CHARS],
                "answer": _truncate(sanitize(answer), MAX_ANSWER_BYTES),
                "time": now_iso(),
            }
            prompt_time = ""
            if turn and not turn.get("qa_sent"):
                prompt_time = str(turn.get("prompt_time") or "")
            elif not turn and parsed.get("request_time"):
                prompt_time = _event_time(parsed["request_time"])
            if prompt_time:
                qa["prompt_time"] = prompt_time
            else:
                summary["continuation"] = True
            started = prompt_time or str(turn.get("prompt_time") or "") or qa["time"]
            context = {
                "cwd": cwd,
                "transcript_path": str(payload.get("transcript_path") or ""),
                "date": started[:10],
                "start_commit": str(turn.get("start_commit") or ""),
                "end_commit": git_head(cwd),
                "branch": str(turn.get("branch") or "") or git_branch(cwd),
            }
            if summary["continuation"]:
                context["continuation"] = True
            qa["context"] = json.dumps(context, ensure_ascii=False)
            entries.append(qa)
            summary["qa"] = True
            if turn:
                turn["qa_sent"] = True
                data["turn"] = turn
            data["last_answer_hash"] = answer_hash

        if entries:
            _deliver(dataset, session_id, entries)
        sent.extend(handled)
        data["sent_ids"] = sent[-MAX_SENT_IDS:]
        state.save()

    hook_log("coding_unit_captured", {"final": final, **summary})
    if not final:
        _drain(dataset, session_id)
        _maybe_cooldown_improve(dataset, session_id, session_key)
    return summary


def _cloud_spool() -> bool:
    try:
        from _cloud_worker import enabled as cloud_enabled
    except ImportError:
        return False
    try:
        return bool(cloud_enabled())
    except Exception:
        return False


def _deliver(dataset: str, session_id: str, entries: list) -> None:
    """Queue the entries locally, in order: the cloud spool, else the replay buffer.

    Both are durable and drained in order by the existing machinery (the cloud
    worker; ``drain_warmup_entries`` at the next prompt, the next Stop and before
    every session-end improve), so a network failure never loses a request.
    """
    from _plugin_common import append_warmup_entry, bump_save_counter, bump_turn_counter

    spool = _cloud_spool()
    for entry in entries:
        kind = "answer" if entry.get("type") == "qa" else "trace"
        if spool:
            from _cloud_worker import enqueue

            enqueue(dataset, session_id, entry, kind)
        else:
            append_warmup_entry(dataset, session_id, entry)
        bump_save_counter(session_id, kind, buffered=True)
        # Keeps the improve gate's "no new entries" check truthful; the every-N
        # improve this counter used to trigger is off in coding mode.
        bump_turn_counter(session_id)


def _drain(dataset: str, session_id: str) -> None:
    if _cloud_spool():
        try:
            from _cloud_worker import ensure_worker

            ensure_worker()
        except Exception as exc:
            hook_log("coding_drain_failed", {"error": str(exc)[:200]})
        return
    try:
        from _plugin_common import drain_warmup_entries, resolve_runtime_mode, server_usable

        if server_usable(resolve_runtime_mode().get("base_url", "")):
            drained, remaining = drain_warmup_entries(dataset, session_id)
            hook_log("coding_unit_drained", {"drained": drained, "remaining": remaining})
    except Exception as exc:
        hook_log("coding_drain_failed", {"error": str(exc)[:200]})


def improve_due(session_key: str, session_id: str) -> bool:
    """A cooldown improve is due: the cooldown passed since the session started or
    since its last successful improve (the session end runs one regardless)."""
    from _plugin_common import improve_cooldown_seconds, read_improve_state

    cooldown = improve_cooldown_seconds()
    if cooldown <= 0:
        return False
    state = read_state(session_key)
    try:
        since = max(
            float(state.get("started_at") or time.time()),
            float(read_improve_state(session_id).get("last_improved_at") or 0),
        )
    except (TypeError, ValueError):
        return False
    return time.time() - since >= cooldown


def _maybe_cooldown_improve(dataset: str, session_id: str, session_key: str) -> None:
    try:
        if not improve_due(session_key, session_id):
            return
        if _cloud_spool():
            from _cloud_worker import request_improve

            request_improve(dataset, session_id, "coding_cooldown")
        else:
            from _plugin_common import run_session_improve_detailed

            outcome = run_session_improve_detailed(dataset, session_id, trigger="auto")
            hook_log("coding_cooldown_improve", {"ok": bool(outcome.get("ok"))})
            if not outcome.get("ok"):
                return
        with _SessionState(session_key) as state:
            if state.acquired:
                state.data["started_at"] = time.time()
                state.save()
    except Exception as exc:
        hook_log("coding_cooldown_improve_failed", {"error": str(exc)[:200]})


# -- Lessons -----------------------------------------------------------------------------


def parse_lesson_text(text: str) -> dict:
    """``Lesson-Type`` / ``State-Key`` headers, the session of the title, the statement."""
    lesson = {"lesson_type": "", "state_key": "", "session_id": "", "body": ""}
    lines = str(text or "").splitlines()
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        header = _HEADER_RE.match(line)
        if header:
            key = "lesson_type" if header.group(1) == "Lesson-Type" else "state_key"
            lesson[key] = header.group(2).strip()
            index += 1
            continue
        title = _TITLE_RE.match(line)
        if title:
            lesson["session_id"] = title.group(1).strip()
            index += 1
            break
        if line:
            break
        index += 1
    lesson["body"] = "\n".join(lines[index:]).strip()
    return lesson


def is_lesson(text: str) -> bool:
    lesson = parse_lesson_text(text)
    return bool(lesson["lesson_type"] or lesson["session_id"])


def _norm(text: str) -> str:
    return " ".join(str(text or "").lower().split())


def _cache_dir() -> Path:
    return _root() / "lessons"


def _cached_body(data_id: str) -> str | None:
    path = _cache_dir() / (re.sub(r"[^A-Za-z0-9-]", "", data_id) + ".txt")
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _store_body(data_id: str, text: str) -> None:
    path = _cache_dir() / (re.sub(r"[^A-Za-z0-9-]", "", data_id) + ".txt")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    except OSError:
        pass


def _raw_text(dataset_id: str, data_id: str, timeout: float) -> str:
    """The stored document. ``/raw`` serves the file, so it is read as text, not JSON."""
    import urllib.request

    from _plugin_common import _api_key, _local_api_url

    url = "{}/api/v1/datasets/{}/data/{}/raw".format(
        _local_api_url().rstrip("/"), dataset_id, data_id
    )
    headers = {}
    key = _api_key()
    if key:
        headers["X-Api-Key"] = key
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        from _plugin_common import _https_context, urlopen_following_307

        opener = urlopen_following_307(request, timeout=timeout, context=_https_context())
    except ImportError:
        opener = urllib.request.urlopen(request, timeout=timeout)
    with opener as response:
        return response.read().decode("utf-8", errors="replace")


def resolve_dataset_id(dataset: str, deadline: float) -> str:
    from _plugin_common import _json_http_request, dataset_id_for

    known = dataset_id_for(dataset)
    if known:
        return known
    rows = _json_http_request("/api/v1/datasets", None, method="GET", timeout=_remaining(deadline))
    for row in rows or []:
        if isinstance(row, dict) and row.get("name") == dataset and row.get("id"):
            return str(row["id"])
    return ""


def _remaining(deadline: float) -> float:
    return max(0.5, min(SERVER_CALL_TIMEOUT, deadline - time.monotonic()))


def _project_ok(metadata: dict, project: list) -> bool:
    """Same project scope as this session. A lesson without the field predates it."""
    if "project_node_sets" not in metadata:
        return True
    scope = metadata.get("project_node_sets") or []
    return sorted(str(s) for s in scope) == sorted(project)


def list_lessons(
    dataset: str, project: list, deadline: float, *, max_fetches: int = MAX_BODY_FETCHES_ON_PROMPT
) -> tuple:
    """(dataset id, lessons) of this project, newest first, bodies from the cache.

    A row is a lesson when its ``external_metadata`` has ``lesson_type`` (cognee
    #6568). Bodies missing from the cache are fetched until ``max_fetches`` or the
    deadline; lessons still without a body are left out this time.
    """
    from _plugin_common import _json_http_request

    dataset_id = resolve_dataset_id(dataset, deadline)
    if not dataset_id:
        return "", []
    rows: list = []
    for page in range(MAX_LISTING_PAGES):
        if time.monotonic() >= deadline:
            break
        batch = _json_http_request(
            "/api/v1/datasets/{}/data?limit={}&offset={}".format(
                dataset_id, LISTING_PAGE_SIZE, page * LISTING_PAGE_SIZE
            ),
            None,
            method="GET",
            timeout=_remaining(deadline),
        )
        batch = batch if isinstance(batch, list) else []
        rows.extend(batch)
        if len(batch) < LISTING_PAGE_SIZE:
            break
    lessons = []
    fetched = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get("externalMetadata", row.get("external_metadata"))
        metadata = metadata if isinstance(metadata, dict) else {}
        if not metadata.get("lesson_type") or not _project_ok(metadata, project):
            continue
        data_id = str(row.get("id") or "")
        if not data_id:
            continue
        text = _cached_body(data_id)
        if text is None and fetched < max_fetches and time.monotonic() < deadline:
            fetched += 1
            try:
                text = _raw_text(dataset_id, data_id, _remaining(deadline))
                _store_body(data_id, text)
            except Exception as exc:
                hook_log(
                    "coding_lesson_fetch_failed", {"data_id": data_id, "error": str(exc)[:200]}
                )
                text = None
        if text is None:
            continue
        parsed = parse_lesson_text(text)
        lessons.append(
            {
                "data_id": data_id,
                "lesson_type": str(metadata.get("lesson_type") or parsed["lesson_type"]),
                "state_key": str(metadata.get("state_key") or parsed["state_key"]),
                "session_id": str(metadata.get("session_id") or parsed["session_id"]),
                "session_date": str(metadata.get("session_date") or ""),
                "git_commit": str(metadata.get("git_commit") or ""),
                "files": [str(f) for f in metadata.get("files") or [] if f],
                "created_at": str(row.get("createdAt") or row.get("created_at") or ""),
                "body": parsed["body"],
            }
        )
    lessons.sort(key=lambda item: (item["session_date"], item["created_at"]), reverse=True)
    return dataset_id, lessons


def recall_answer(query: str, dataset: str, dataset_id: str, deadline: float) -> str:
    """The graph recall over the lessons, as one text; "" on any error (404 = empty)."""
    from _plugin_common import _json_http_request

    body = {
        "query": query,
        "top_k": 10,
        "scope": ["graph"],
        "search_type": "HYBRID_COMPLETION",
        "only_context": True,
        "node_name": ["session_learnings"],
        "node_name_filter_operator": "OR",
    }
    if dataset_id:
        body["dataset_ids"] = [dataset_id]
    else:
        body["datasets"] = [dataset]
    try:
        rows = _json_http_request("/api/v1/recall", body, timeout=_remaining(deadline))
    except Exception as exc:
        hook_log("coding_recall_failed", {"error": str(exc)[:200]})
        return ""
    return json.dumps(rows, ensure_ascii=False, default=str) if rows else ""


def select_lessons(lessons: list, recalled_text: str, budget: int = LESSON_BUDGET) -> list:
    """The newest ``test-baseline``, the two newest pitfalls, then recalled lessons.

    ``lessons`` is newest first. Only the newest lesson of a state key counts;
    duplicates (ignoring case and whitespace) are skipped; a lesson that does not
    fit the budget is skipped and a later, shorter one may still fit.
    """
    seen_keys = set()
    current = []
    for lesson in lessons:
        key = lesson.get("state_key") or ""
        if key:
            if key in seen_keys:
                continue
            seen_keys.add(key)
        if lesson.get("body"):
            current.append(lesson)

    ordered = []
    baseline = [item for item in current if item.get("state_key") == "test-baseline"][:1]
    ordered.extend(baseline)
    ordered.extend([item for item in current if item.get("lesson_type") == "pitfall"][:2])
    answer = _norm(recalled_text)
    if answer:
        hits = []
        for lesson in current:
            prefix = _norm(lesson["body"])[:RECALL_MATCH_PREFIX]
            position = answer.find(prefix) if prefix else -1
            if position >= 0:
                hits.append((position, lesson))
        ordered.extend(item for _, item in sorted(hits, key=lambda pair: pair[0]))

    chosen, used, bodies = [], 0, set()
    for lesson in ordered:
        normalized = _norm(lesson["body"])
        if normalized in bodies:
            continue
        size = len(lesson["body"])
        if used + size > budget:
            continue
        bodies.add(normalized)
        chosen.append(lesson)
        used += size
    return chosen


# -- Git tags -----------------------------------------------------------------------------


def _match(name: str, paths: list) -> list:
    """Paths equal to ``name`` or ending with ``/name``, case-insensitive."""
    lowered = name.lower().lstrip("./")
    if not lowered:
        return []
    return [p for p in paths if p.lower() == lowered or p.lower().endswith("/" + lowered)]


def _kind(names: list, label: str) -> str:
    if not names:
        return ""
    shown = ", ".join(names[:2])
    more = len(names) - 2
    return f"{shown} +{more} more {label}" if more > 0 else f"{shown} {label}"


def lesson_tag(lesson: dict, cwd: str) -> str:
    """``(session <date>, commit <short>; <files> changed since; <files> no longer exists)``.

    Empty outside git, without a commit, or when the commit is unknown here.
    """
    commit = str(lesson.get("git_commit") or "").strip()
    if not commit or not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit):
        return ""
    if not _git(cwd, "rev-parse", "--verify", "--quiet", commit + "^{commit}").strip():
        return ""
    short = _git(cwd, "rev-parse", "--short", commit).strip() or commit[:7]
    changed_paths = [p for p in _git(cwd, "diff", "--name-only", commit).splitlines() if p]
    now_paths = [p for p in _git(cwd, "ls-files").splitlines() if p]
    then_paths = [p for p in _git(cwd, "ls-tree", "-r", "--name-only", commit).splitlines() if p]
    changed, gone = [], []
    for name in lesson.get("files") or []:
        in_now = _match(name, now_paths)
        in_then = _match(name, then_paths)
        if not in_now and not in_then:
            continue
        if in_then and not in_now:
            gone.append(name)
        elif _match(name, changed_paths):
            changed.append(name)
    parts = []
    date = str(lesson.get("session_date") or "")[:10]
    parts.append(f"session {date}, commit {short}" if date else f"commit {short}")
    detail = [p for p in (_kind(changed, "changed since"), _kind(gone, "no longer exists")) if p]
    return "(" + "; ".join([parts[0]] + detail) + ")"


def recent_commits(cwd: str, limit: int = 3) -> list:
    """``<hash> (<date>): <files> +N more (+<added>/−<deleted> lines)``, newest first."""
    out = _git(
        cwd,
        "log",
        f"-{limit}",
        "--no-renames",
        "--numstat",
        "--date=short",
        "--format=%x1e%h%x1f%ad",
    )
    lines = []
    for chunk in out.split("\x1e"):
        chunk = chunk.strip("\n")
        if not chunk:
            continue
        head, _, rest = chunk.partition("\n")
        sha, _, date = head.partition("\x1f")
        files, added, deleted = [], 0, 0
        for row in rest.splitlines():
            cols = row.split("\t")
            if len(cols) != 3:
                continue
            files.append(cols[2])
            added += int(cols[0]) if cols[0].isdigit() else 0
            deleted += int(cols[1]) if cols[1].isdigit() else 0
        shown = ", ".join(files[:2]) or "no file changes"
        if len(files) > 2:
            shown += f" +{len(files) - 2} more"
        lines.append(f"{sha} ({date}): {shown} (+{added}/−{deleted} lines)")
    return lines


# -- Render ---------------------------------------------------------------------------------


def _cap_lines(lines: list, cap: int) -> list:
    kept, used = [], 0
    for line in lines:
        if used + len(line) + 1 > cap:
            continue
        kept.append(line)
        used += len(line) + 1
    return kept


def render_block(dataset: str, sessions: int, lessons: list, cwd: str, in_git: bool) -> str:
    if not lessons:
        return ""
    noun = "1 earlier session" if sessions == 1 else f"{sessions} earlier sessions"
    framing = FRAMING.format(sessions=noun, dataset=dataset)[:FRAMING_CAP]
    lesson_lines = []
    for lesson in lessons:
        body = " ".join(str(lesson["body"]).split())
        tag = lesson_tag(lesson, cwd) if in_git else ""
        lesson_lines.append(f"- {body} {tag}".rstrip())
    lesson_lines = _cap_lines(lesson_lines, LESSON_LINES_CAP)
    if not lesson_lines:
        return ""
    parts = [framing, "Lessons:", *lesson_lines]
    if in_git:
        commits = _cap_lines([f"- {c}" for c in recent_commits(cwd)], COMMITS_CAP)
        if commits:
            parts.extend(["Recent commits (newest first):", *commits])
    return "\n".join(parts)


def project_scope(dataset: str, session_id: str, cwd: str) -> list:
    """The project node set this session's lessons are stored under (may be empty)."""
    try:
        from _project_memory import route

        node_set = route(dataset, session_id).get("node_set") or []
        if node_set:
            return [str(n) for n in node_set]
    except Exception:
        pass
    try:
        from _project_memory import project_tag

        tag = project_tag(git_root(cwd) or cwd, os.environ.get("COGNEE_PROJECT_NODE_SET", ""))
        return [tag] if tag else []
    except Exception:
        return []


def build_block(query: str, cwd: str) -> str:
    """The memory block for ``query``; "" when there is nothing to show or on any error."""
    deadline = time.monotonic() + DELIVERY_BUDGET_SECONDS
    session_id, dataset = _session()
    if not dataset:
        return ""
    project = project_scope(dataset, session_id, cwd)
    dataset_id, lessons = list_lessons(dataset, project, deadline)
    if not lessons:
        return ""
    recalled = recall_answer(query, dataset, dataset_id, deadline) if query.strip() else ""
    chosen = select_lessons(lessons, recalled)
    sessions = len(
        {
            item["session_id"]
            for item in lessons
            if item["session_id"] and item["session_id"] != session_id
        }
    )
    in_git = bool(git_root(cwd))
    return render_block(dataset, max(1, sessions), chosen, cwd, in_git)


def deliver(session_key: str, query: str, cwd: str, *, force: bool = False) -> str:
    """The block once per session (again after a compaction); "" otherwise.

    One attempt per session: a completed lookup marks the session even when it
    found no lessons, so a dataset without lessons costs one listing, not one per
    prompt. A failed lookup (server down, timeout) is not marked and is retried on
    the next prompt.
    """
    if not session_key or not server_supported():
        return ""
    if not force and read_state(session_key).get("delivered"):
        return ""
    try:
        block = build_block(query, cwd)
    except Exception as exc:
        hook_log("coding_delivery_failed", {"error": str(exc)[:200]})
        return ""
    with _SessionState(session_key) as state:
        if state.acquired:
            state.data["delivered"] = True
            state.save()
    hook_log("coding_block_delivered", {"chars": len(block), "force": force})
    return block


def reset_delivery(session_key: str) -> None:
    """After a compaction the block is gone from the context: allow it once more."""
    with _SessionState(session_key) as state:
        if state.acquired and state.data.get("delivered"):
            state.data["delivered"] = False
            state.save()


def last_request(session_key: str) -> str:
    turn = read_state(session_key).get("turn")
    return str(turn.get("prompt") or "") if isinstance(turn, dict) else ""


def warm_lessons(project_cwd: str) -> int:
    """Fill the lesson cache in the background (SessionStart), so the first prompt
    only lists the dataset and fetches nothing. Returns the lessons cached."""
    deadline = time.monotonic() + 60.0
    session_id, dataset = _session()
    if not dataset:
        return 0
    project = project_scope(dataset, session_id, project_cwd)
    _, lessons = list_lessons(dataset, project, deadline, max_fetches=500)
    return len(lessons)
