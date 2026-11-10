"""Coding memory mode (``COGNEE_CODING_MEMORY=true``, SDK-1336).

An opt-in mode for the server's coding distillation (``DISTILLATION_MODE=coding``,
SDK-1335). With it on, the plugin does nothing while the agent works:

* **Capture at the end of each request.** The ``Stop`` hook reads the host's
  transcript and sends every tool call not sent yet (failed calls included) as a
  ``trace`` entry, then one ``qa`` entry per answered request not sent yet, with
  the request and its final message as the transcript pairs them. Every entry
  carries the time of its event (``time``, and ``prompt_time`` on the QA entry),
  which the server uses to pair calls to requests. Sent calls and requests are
  remembered by id, so a second ``Stop`` sends only what is new and a late or
  skipped ``Stop`` loses nothing.
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
#: QA entries sent per capture (requests answered but not sent yet, oldest first).
MAX_QA_PER_CAPTURE = 50
#: Requests whose QA was sent, kept per session (bounded state file).
MAX_QA_SENT_IDS = 2000
#: Request starts (git context) kept per session.
MAX_TURNS = 20
#: How far a recorded request start may be from the transcript's prompt time.
TURN_MATCH_SECONDS = 10.0

#: Delivery budgets (characters), both applied once, at selection.
#: ``LESSON_BUDGET`` counts each lesson's line without its git tag (``- `` +
#: statement + newline), as the measured runs did; ``LESSON_LINES_CAP`` bounds
#: the lines as rendered, git tags included.
LESSON_BUDGET = 1500
LESSON_LINES_CAP = 1800
FRAMING_CAP = 500
COMMITS_CAP = 300
#: Candidates measured (each with its git tag) per block inside git; the rest
#: are not considered, which bounds the git calls on the prompt path.
MAX_TAGGED_CANDIDATES = 10
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


def _entry_schemas(spec) -> tuple | None:
    """``(QAEntry properties, TraceEntry properties)``, or None when ``spec`` is not
    an OpenAPI document carrying both schemas (every cognee version has them)."""
    components = spec.get("components") if isinstance(spec, dict) else None
    schemas = components.get("schemas") if isinstance(components, dict) else None
    if not isinstance(schemas, dict):
        return None
    found = []
    for name in ("QAEntry", "TraceEntry"):
        schema = schemas.get(name)
        if not isinstance(schema, dict):
            return None
        props = schema.get("properties")
        found.append(set(props) if isinstance(props, dict) else set())
    return found[0], found[1]


def check_server_support(*, refresh: bool = False, timeout: float = 5.0) -> dict:
    """Whether the server takes the event times coding mode needs (cognee PR #6568).

    Reads ``/openapi.json``: ``QAEntry`` must have ``time`` and ``prompt_time`` and
    ``TraceEntry`` ``time``. A definite verdict is cached an hour per server.
    Returns ``{"ok": True|False|None, "reason": str}``. ``False`` only when the
    spec has both schemas and they lack the times; ``None`` means unknown (the
    server could not be asked, or its reply is not a spec with those schemas),
    which callers treat as allowed so capture is buffered rather than lost. An
    unknown is never cached, so it does not replace an earlier verdict.
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
        # The cached entry stays for callers within its hour; this check says
        # only that the server could not be asked now.
        return {"ok": None, "reason": "unreachable: " + str(exc)[:120]}
    schemas = _entry_schemas(spec)
    if schemas is None:
        return {
            "ok": None,
            "reason": "could not tell: /openapi.json has no QAEntry/TraceEntry schemas",
        }
    qa, trace = schemas
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
    """One line for the doctor report.

    For a local server it also says whether the running server was started in
    coding mode: the coding env reaches only a server the plugin spawns, and a
    shared server keeps the mode it booted with.
    """
    if not enabled():
        return "off"

    def server_mode() -> str:
        try:
            import urllib.parse

            from _plugin_common import _local_api_url, live_server_record, service_url_is_local

            url = _local_api_url()
            if not service_url_is_local(url):
                return ""
            if "://" not in url:
                url = "http://" + url
            record = live_server_record(urllib.parse.urlparse(url).port or 80)
        except Exception:
            return ""
        if not record:
            return ""
        stamp = record.get("coding_memory")
        pid = record.get("pid")
        if stamp is True:
            return f"; local server (pid {pid}) in coding mode"
        if stamp is False:
            return (
                f"; local server (pid {pid}) not in coding mode: no coding lessons "
                "until it restarts"
            )
        return f"; local server (pid {pid}) coding mode unknown"

    support = check_server_support(refresh=True)
    if support.get("ok") is False:
        return "on, but memory is off: " + str(support.get("reason") or "server too old")
    if support.get("ok") is None:
        return "on (server not reachable: could not check it)"
    mode = server_mode()
    allowed = delete_permission(dataset)
    if allowed is True:
        return f"on (server supports it{mode}; delete permission on {dataset})"
    if allowed is False:
        return (
            f"on (server supports it{mode}), but no direct delete permission on {dataset} "
            "was found: improve fails unless a role or tenant grants it"
        )
    return f"on (server supports it{mode}; delete permission not checked)"


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
    """Record the request's git context: start commit, branch, cwd and time.

    Runs in the synchronous prompt hook, so it is on disk before the agent starts.
    The question and its answer come from the transcript at capture time; the
    capture pairs each request with the entry whose ``prompt_time`` is closest to
    the request's transcript time. Entries are kept in a short list (not one
    slot), so a Stop that runs after the next prompt still finds its own entry.
    The sanitized prompt is kept for :func:`last_request`.
    """
    from _capture_policy import sanitize

    if not session_key:
        return
    turn = {
        "prompt": str(sanitize(prompt or ""))[:MAX_PROMPT_CHARS],
        "prompt_time": now_iso(),
        "start_commit": git_head(cwd),
        "branch": git_branch(cwd),
        "cwd": cwd,
    }
    with _SessionState(session_key) as state:
        if not state.acquired:
            hook_log("coding_state_locked", {"op": "begin_turn"})
            return
        turns = [t for t in state.data.get("turns") or [] if isinstance(t, dict)]
        turns.append(turn)
        state.data["turns"] = turns[-MAX_TURNS:]
        state.data.pop("turn", None)  # the single slot of earlier versions
        state.data.setdefault("started_at", time.time())
        state.save()


def _parse_time(stamp: str) -> float | None:
    stamp = _event_time(stamp)
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp).timestamp()
    except ValueError:
        return None


def match_turn(turns: list, request_time: str) -> dict:
    """The recorded request start closest to ``request_time``, within
    ``TURN_MATCH_SECONDS``; ``{}`` when none is that close."""
    target = _parse_time(request_time)
    if target is None:
        return {}
    best, best_gap = {}, TURN_MATCH_SECONDS
    for turn in turns or []:
        if not isinstance(turn, dict):
            continue
        at = _parse_time(str(turn.get("prompt_time") or ""))
        if at is None:
            continue
        gap = abs(at - target)
        if gap <= best_gap:
            best, best_gap = turn, gap
    return best


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


#: Claude's marker after Esc: ``[Request interrupted by user]``, or ``... for tool use]``.
_INTERRUPTED = "[Request interrupted by user"
INTERRUPTED_ANSWER = "[Interrupted by the user before finishing.]"

_NOT_A_REQUEST = (
    "<command-",
    "<local-command",
    "<task-notification",
    "<system-reminder",
    "Caveat:",
    _INTERRUPTED,
)
#: A Claude Bash error result's first line: ``Exit code N``.
_EXIT_CODE_RE = re.compile(r"\AExit code (-?\d+)[ \t]*(?:\r?\n|\Z)")


def _claude_files(transcript: Path) -> list:
    files = [transcript]
    sub = transcript.with_suffix("") / "subagents"
    try:
        files.extend(sorted(sub.glob("*.jsonl")))
    except OSError:
        pass
    return files


def _record_id(record: dict, stamp: str, text: str) -> str:
    """The record's ``uuid``, else a hash of its time and text (Codex rollouts)."""
    return str(record.get("uuid") or "") or _digest(stamp + "\n" + text)


def _new_unit(record: dict, text: str, stamp: str) -> dict:
    return {
        "id": _record_id(record, stamp, text),
        "request": text,
        "request_time": stamp,
        "answer": "",
        "answer_id": "",
        "answer_time": "",
    }


def _set_answer(unit: dict | None, record: dict, text: str, stamp: str) -> None:
    # An interrupted request's turn closed at the interrupt.
    if unit is not None and not unit.get("interrupted"):
        unit["answer"] = text
        unit["answer_id"] = _record_id(record, stamp, text)
        unit["answer_time"] = stamp


def _interrupt(unit: dict | None, record: dict, stamp: str) -> None:
    """Close ``unit`` at the user's interrupt: its answer is the interrupted
    prefix plus the last text the agent wrote before it, its time the marker's."""
    if unit is None or unit.get("interrupted"):
        return
    partial = str(unit.get("answer") or "").strip()
    unit["answer"] = INTERRUPTED_ANSWER + ("\n" + partial if partial else "")
    unit["answer_id"] = _record_id(record, stamp, _INTERRUPTED)
    unit["answer_time"] = stamp
    unit["interrupted"] = True


def _empty_parse() -> dict:
    return {
        "calls": [],
        "answer": "",
        "answer_time": "",
        "request": "",
        "request_time": "",
        "units": [],
    }


def parse_claude(transcript_path: str) -> dict:
    """Tool calls (main thread and subagents), the last request and the last answer.

    ``units`` holds one entry per real user prompt of the main thread, in order:
    the request and its answer (the last main-thread assistant text before the
    next prompt), each with an id and a time. A request the user interrupted
    (Esc) is closed at the interrupt marker and flagged ``interrupted``.

    Records carrying ``forkedFrom`` are skipped: a forked session copies its
    parent's history (same ids and times), which belongs to the parent session.
    """
    transcript = Path(str(transcript_path or "")).expanduser()
    uses: dict = {}
    results: dict = {}
    last_text, last_text_time = "", ""
    last_request, last_request_time = "", ""
    units: list = []
    if not transcript.is_file():
        return _empty_parse()
    for index, path in enumerate(_claude_files(transcript)):
        main = index == 0
        for record in _iter_jsonl(path):
            if record.get("forkedFrom") is not None:
                continue
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
                    _set_answer(units[-1] if units else None, record, text, stamp)
            elif kind == "user":
                if (
                    main
                    and not record.get("isSidechain")
                    and _block_text(content).strip().startswith(_INTERRUPTED)
                ):
                    _interrupt(units[-1] if units else None, record, stamp)
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
                    units.append(_new_unit(record, text, stamp))
    calls = []
    for tool_id, result in results.items():
        use = uses.get(tool_id)
        if not use:
            continue
        # No "error" here: trace_entry derives it from the output after the
        # capture sanitized the output (a cut <private> span must not leak).
        call = {
            "id": tool_id,
            "name": use["name"],
            "input": use["input"],
            "output": result["output"],
            "is_error": result["is_error"],
            "time": result["time"],
        }
        # A failed Bash result starts with "Exit code N": move it to exit_code,
        # so call_error treats Claude and Codex commands alike.
        output = call["output"]
        if call["is_error"] and call["name"] == "Bash" and isinstance(output, str):
            match = _EXIT_CODE_RE.match(output)
            if match:
                call["exit_code"] = int(match.group(1))
                call["output"] = output[match.end() :]
        calls.append(call)
    calls.sort(key=lambda c: c["time"])
    return {
        "calls": calls,
        "answer": last_text,
        "answer_time": last_text_time,
        "request": last_request,
        "request_time": last_request_time,
        "units": units,
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


def _codex_file_changes(item: dict, status: str, failed: bool, stamp: str):
    """One call per changed path of a ``FileChange`` item, as Claude's tools
    name them: ``Write`` for an add, ``Edit`` for an update or a delete.

    ``changes`` maps each path to ``{type, content | unified_diff, move_path}``
    (openai/codex protocol). Each call carries ``file_path`` (and ``move_path``),
    so the capture policy drops secret paths, and its own id
    (``<item id>:<path>``), so each file is sent once.
    """
    changes = item.get("changes")
    common = {
        "output": status or "completed",
        "is_error": failed,
        "error": status if failed else "",
        "time": stamp,
    }
    if not isinstance(changes, dict):
        return {"name": "Edit", "input": {"changes": changes}, **common}
    item_id = str(item.get("id") or "")
    calls = []
    for path, change in changes.items():
        change = change if isinstance(change, dict) else {}
        change_type = str(change.get("type") or "update").lower()
        tool_input = {"file_path": str(path), "change": change_type}
        if change_type == "update":
            tool_input["diff"] = change.get("unified_diff")
        else:
            tool_input["content"] = change.get("content")
        if change.get("move_path"):
            tool_input["move_path"] = str(change["move_path"])
        call = {"name": "Write" if change_type == "add" else "Edit", "input": tool_input}
        if item_id:
            call["id"] = f"{item_id}:{path}"
        calls.append({**call, **common})
    return calls


def _codex_call(item: dict, stamp: str):
    """The Claude-shaped call of a Codex item: one dict, a list (a ``FileChange``
    touching several files), or None for an item that is not a tool call."""
    kind = str(item.get("type") or "")
    if not kind or kind in _CODEX_SKIP:
        return None
    status = str(item.get("status") or "").lower()
    failed = status in _FAILED
    if kind == "CommandExecution":
        exit_code = item.get("exit_code")
        failed = failed or exit_code not in (0, None)
        # The error is the tail of the output, built by trace_entry after the
        # capture sanitized the whole output: a tail sliced here could keep
        # "SECRET</private>" without its opener.
        return {
            "name": "Bash",
            "input": {"command": _codex_command(item.get("command"))},
            "output": str(item.get("aggregated_output") or ""),
            "is_error": failed,
            "exit_code": exit_code,
            "time": stamp,
        }
    if kind == "FileChange":
        return _codex_file_changes(item, status, failed, stamp)
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
    """Tool calls, the last request and the last answer from a Codex rollout file.

    ``units`` as in :func:`parse_claude`; rollouts carry no record ids, so a
    unit's id is a hash of the prompt's time and text (and likewise the answer's).

    A fork of a legacy-history thread copies its parent's events (with their
    original times) into the new rollout; those events, timed before the fork's
    ``session_meta``, are skipped. A paginated fork references its parent
    instead of copying it and parses as any rollout.
    """
    path = Path(str(transcript_path or "")).expanduser()
    calls = []
    answer, answer_time = "", ""
    request, request_time = "", ""
    units: list = []
    fork_point: float | None = None
    if not path.is_file():
        return _empty_parse()
    for record in _iter_jsonl(path):
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        if record.get("type") == "session_meta":
            if (
                fork_point is None
                and payload.get("forked_from_id")
                and str(payload.get("history_mode") or "").lower() != "paginated"
            ):
                fork_point = _parse_time(
                    str(payload.get("timestamp") or record.get("timestamp") or "")
                )
            continue
        if record.get("type") != "event_msg":
            continue
        if fork_point is not None:
            at = _parse_time(_codex_time(record, payload))
            if at is not None and at < fork_point:
                continue  # copied from the parent thread
        kind = payload.get("type")
        if kind == "task_complete" and payload.get("last_agent_message"):
            answer, answer_time = str(payload["last_agent_message"]), str(record.get("timestamp"))
            _set_answer(units[-1] if units else None, {}, answer.strip(), answer_time)
            continue
        if kind == "turn_aborted":
            # Any reason (an Esc interrupt or otherwise): the turn did not finish,
            # so its request must not read as answered by a progress note.
            _interrupt(units[-1] if units else None, {}, _codex_time(record, payload))
            continue
        if kind != "item_completed":
            continue
        item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
        stamp = _codex_time(record, payload)
        if item.get("type") == "UserMessage":
            text = _block_text(item.get("content")).strip()
            if text and not text.startswith(_NOT_A_REQUEST):
                request, request_time = text, stamp
                units.append(_new_unit({}, text, stamp))
            continue
        if item.get("type") == "AgentMessage":
            text = _block_text(item.get("content")).strip()
            if text:
                answer, answer_time = text, stamp
                _set_answer(units[-1] if units else None, {}, text, stamp)
            continue
        found = _codex_call(item, stamp)
        if not found or not item.get("id"):
            continue
        for call in found if isinstance(found, list) else [found]:
            call.setdefault("id", str(item["id"]))
            calls.append(call)
    calls.sort(key=lambda c: c["time"])
    return {
        "calls": calls,
        "answer": answer,
        "answer_time": answer_time,
        "request": request,
        "request_time": request_time,
        "units": units,
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


def _as_text(value) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False)


_ERROR_LINE_RE = re.compile(
    r"error|failed|fatal|exception|traceback|not found|no such|denied|cannot", re.IGNORECASE
)
#: Bytes of the error line kept at the head of a command's error.
MAX_ERROR_LINE_BYTES = 300


_TRACEBACK_HEADER = "Traceback (most recent call last):"
#: A Python exception line: ``FileNotFoundError: …``, ``pkg.errors.CustomError``.
#: Inside a traceback a warning can end it (``python -W error``), so ``Warning``
#: counts there; outside one a warning is never the error (:data:`_ERROR_NAME_RE`).
_EXCEPTION_LINE_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Warning|Interrupt|Exit)\b")
_ERROR_NAME_RE = re.compile(r"^[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)\b")
#: A line that opens with an error word: ``error: …``, ``error[E0425]: …``,
#: ``fatal: …``, ``ERROR …``, ``npm ERR! …`` (npm < 10), ``npm error …``, ``panic: …``.
_LEADING_ERROR_RE = re.compile(r"^(?:(?:error|fatal|panic)\b|npm (?:err!|error\b))", re.I)
#: A compiler or linter diagnostic at a location: ``a.py:3:1: F401 …`` (ruff,
#: flake8), ``a.py:3: error: …`` (mypy), ``a.c:3:5: error: …`` (gcc, clang).
_LOCATED_ERROR_RE = re.compile(
    r"^\S+:\d+(?::\d+)?:\s*(?:(?:error|fatal)\b|[A-Z]{1,4}\d{2,5}\b)", re.IGNORECASE
)
#: A program's own error message: ``bash: foo: command not found``,
#: ``cat: x: No such file or directory``, ``ls: cannot access 'x'``.
_PROGRAM_PREFIX_RE = re.compile(r"^[\w./-]+: \S")
#: A run's summary, which names a count, not the error: ``Found 1 error.``,
#: ``2 failed, 3 passed``, ``=== 1 failed in 0.1s ===``.
_SUMMARY_LINE_RE = re.compile(r"^(?:found \d+ errors?\b|\d+ (?:failed|errors?)\b|=+ .* =+$)", re.I)


def _last_exception(lines: list) -> str:
    """The exception that ended a Python traceback: with chained exceptions
    ("During handling of the above exception…") the last one, never a header."""
    found, seen_header = "", False
    for raw in lines:
        if raw.strip() == _TRACEBACK_HEADER:
            seen_header = True
        elif seen_header and raw[:1] not in (" ", "\t") and _EXCEPTION_LINE_RE.match(raw):
            found = raw.strip()
    return found


def _error_line(text: str) -> str:
    """The line of a command's output that names its error.

    The server keys pitfalls on this line, so it must name the failure and stay
    the same across runs. In order: a Python traceback's last exception; the
    first anchored error line (a pytest ``E`` line, a line opening with an error
    word or an exception name, a located compiler/linter diagnostic, a
    ``program: …`` message with an error word); the first line with an error word
    anywhere that is not a run summary; else the first non-empty line.

    The loose fallback takes the first match, not the last: tools end a failure
    with a generic line (npm's log path, make's ``*** Error 1``), so on output
    no rule here knows, the last error word would key unrelated failures alike,
    while the first one is where this failure started.
    """
    lines = text.splitlines()
    if any(raw.strip() == _TRACEBACK_HEADER for raw in lines):
        found = _last_exception(lines)
        if found:
            return found
    first = loose = ""
    for raw in lines:
        line = raw.strip()
        if not line or line == _TRACEBACK_HEADER:
            continue
        if (
            raw.startswith("E ")
            or _LEADING_ERROR_RE.match(line)
            or _ERROR_NAME_RE.match(line)
            or _LOCATED_ERROR_RE.match(line)
        ):
            return line
        if _ERROR_LINE_RE.search(line) and not _SUMMARY_LINE_RE.match(line):
            if _PROGRAM_PREFIX_RE.match(line):
                return line
            loose = loose or line
        first = first or line
    return loose or first


def _tail_lines(text: str, cap: int) -> list:
    """The last whole non-empty lines of ``text`` that fit in ``cap`` bytes."""
    kept, used = [], 0
    for line in reversed(text.splitlines()):
        if not line.strip():
            continue
        size = len(line.encode("utf-8", errors="replace")) + 1
        if used + size > cap:
            break
        kept.append(line)
        used += size
    return kept[::-1]


def call_error(call: dict) -> str:
    """The error text of a failed call, from its (already sanitized) fields.

    A command's error is its error line (see :func:`_error_line`) with
    `` (exit code N)``, then the
    tail of its output cut at a line boundary; the server keys pitfalls on that
    first line, so it is never a byte-cut fragment. A call with an explicit
    ``error`` uses it; anything else uses its output.
    """
    if not call.get("is_error"):
        return ""
    if "exit_code" in call:
        code = call.get("exit_code")
        output = _as_text(call.get("output"))
        found = _error_line(output)
        line = _truncate(found, MAX_ERROR_LINE_BYTES)
        if code not in (0, None):
            line = f"{line} (exit code {code})" if line else f"exit code {code}"
        room = MAX_ERROR_BYTES - len(line.encode("utf-8")) - 1
        tail = _tail_lines(output, room) if room > 0 else []
        if [t.strip() for t in tail] == [found]:
            tail = []  # the output is the error line alone
        return "\n".join([line, *tail]) if tail else line
    if call.get("error"):
        return _as_text(call.get("error"))
    return _as_text(call.get("output"))


def _sanitize_call(call: dict, sanitize) -> dict:
    """Sanitize every field before trace_entry slices any of them: a ``<private>``
    span cut by a byte cap would leave an unclosed opener, which is plain text."""
    clean = dict(call)
    for field in ("input", "output", "error"):
        if clean.get(field) is not None:
            clean[field] = sanitize(clean[field])
    return clean


def trace_entry(call: dict) -> dict:
    """The trace entry of a call. Pass a sanitized call (:func:`_sanitize_call`):
    the byte caps here would otherwise cut ``<private>`` spans open."""
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
        "error_message": _truncate(call_error(call), MAX_ERROR_BYTES),
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


# -- QA entries ---------------------------------------------------------------------------


def _same_text(text: str) -> str:
    """A whitespace-insensitive key, to compare the Stop payload's final message
    with the transcript's text of the same answer."""
    return _digest(" ".join(str(text or "").split()))


def _stop_answer(payload: dict) -> str:
    answer = str(payload.get("last_assistant_message") or "").strip()
    return "" if answer == "null" else answer


def _qa_entries(payload: dict, parsed: dict, data: dict, cwd: str, sanitize, final: bool) -> list:
    """One QA entry per answered request not sent yet, paired by transcript unit.

    Requests are tracked by unit id (``qa_sent_ids``), so a Stop that runs after
    the next prompt (Claude's Stop hook is async, Codex's is detached) still sends
    its own question with its own answer, and a Stop that never ran is made up at
    the next capture. A sent request whose answer changed since (the agent was
    told to go on) is sent again as a continuation, without ``prompt_time``; an
    interrupted request is sent once, flagged ``interrupted``, never continued.
    The context's ``cwd`` is the repository root (the server's repo root) and
    ``session_cwd`` the hook's directory. Updates ``data`` in place.
    """
    root = git_root(cwd) or cwd
    units = [dict(u) for u in parsed.get("units") or [] if isinstance(u, dict) and u.get("id")]
    sent_ids = [str(i) for i in data.get("qa_sent_ids") or []]
    sent_set = set(sent_ids)
    answers = data.get("qa_answers") if isinstance(data.get("qa_answers"), dict) else {}

    # The Stop payload's final message is fresher than the transcript (which may
    # not hold it yet), but only for the last request: an async Stop of request N
    # can read a transcript that already has request N+1.
    skip_last = False
    stop_answer = _stop_answer(payload)
    if stop_answer and units:
        last, stop_key = units[-1], _same_text(stop_answer)
        earlier = {_same_text(u["answer"]) for u in units[:-1] if u.get("answer")}
        # No Stop fires for an interrupted request: it is never the Stop's request.
        earlier_open = any(
            u.get("answer") and u["id"] not in sent_set and not u.get("interrupted")
            for u in units[:-1]
        )
        if last.get("interrupted") or (
            last.get("answer") and _same_text(last["answer"]) == stop_key
        ):
            pass
        elif stop_key in earlier or earlier_open:
            skip_last = not final  # this Stop is an earlier request's; the last is running
        else:
            last["answer"], last["answer_id"], last["answer_time"] = (
                stop_answer,
                "stop:" + stop_key,
                "",
            )

    turns = [t for t in data.get("turns") or [] if isinstance(t, dict)]
    matched = [match_turn(turns, str(u.get("request_time") or "")) for u in units]
    head = None
    out: list = []
    for index, unit in enumerate(units):
        if len(out) >= MAX_QA_PER_CAPTURE:
            break
        if skip_last and index == len(units) - 1:
            break
        answer = str(unit.get("answer") or "").strip()
        if not answer:
            continue
        unit_id, answer_key = str(unit["id"]), _same_text(answer)
        continuation = unit_id in sent_set
        if continuation and unit.get("interrupted"):
            continue
        if continuation:
            previous = answers.get(unit_id) if isinstance(answers.get(unit_id), dict) else {}
            if not previous or previous.get("answer_id") == unit.get("answer_id"):
                continue
            if previous.get("hash") == answer_key:
                continue
        turn = matched[index]
        qa = {
            "type": "qa",
            "question": str(sanitize(str(unit.get("request") or "")))[:MAX_PROMPT_CHARS],
            "answer": _truncate(sanitize(answer), MAX_ANSWER_BYTES),
            "time": _event_time(str(unit.get("answer_time") or "")) or now_iso(),
        }
        prompt_time = ""
        if not continuation:
            prompt_time = (
                _event_time(str(unit.get("request_time") or ""))
                or str(turn.get("prompt_time") or "")
                or qa["time"]
            )
            qa["prompt_time"] = prompt_time
        # The request ended where the next one started; the last one ends at HEAD.
        later = [t for t in matched[index + 1 :] if t.get("start_commit")]
        if later:
            end_commit = str(later[0]["start_commit"])
        else:
            head = git_head(cwd) if head is None else head
            end_commit = head
        started = prompt_time or _event_time(str(unit.get("request_time") or "")) or qa["time"]
        context = {
            "cwd": root,
            "session_cwd": cwd,
            "transcript_path": str(payload.get("transcript_path") or ""),
            "date": started[:10],
            "start_commit": str(turn.get("start_commit") or ""),
            "end_commit": end_commit,
            "branch": str(turn.get("branch") or "") or git_branch(cwd),
        }
        if continuation:
            context["continuation"] = True
        if unit.get("interrupted"):
            context["interrupted"] = True
        qa["context"] = json.dumps(context, ensure_ascii=False)
        out.append(qa)
        if not continuation:
            sent_ids.append(unit_id)
            sent_set.add(unit_id)
        answers[unit_id] = {"answer_id": str(unit.get("answer_id") or ""), "hash": answer_key}

    sent_ids = sent_ids[-MAX_QA_SENT_IDS:]
    kept = set(sent_ids)
    data["qa_sent_ids"] = sent_ids
    data["qa_answers"] = {k: v for k, v in answers.items() if k in kept}
    data.pop("turn", None)  # the single slot of earlier versions
    data.pop("last_answer_hash", None)
    return out


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
                # Sanitize whole values first, then again after the byte caps.
                entries.append(sanitize(trace_entry(_sanitize_call(call, sanitize))))
        except CapturePatternError as exc:
            # Fail closed, but say so: a broken COGNEE_CAPTURE_TOOLS pattern.
            hook_log("coding_capture_pattern_invalid", {"error": str(exc)[:200]})
            entries, handled = [], []
        summary["traces"] = len(entries)

        qa_entries = _qa_entries(payload, parsed, data, cwd, sanitize, final)
        entries.extend(qa_entries)
        summary["qa"] = len(qa_entries)
        summary["continuation"] = sum(
            1 for qa in qa_entries if json.loads(qa["context"]).get("continuation")
        )

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

            # The worker counts the entry when it delivers it (one bump per
            # entry) and, for a coding item, requests no every-N improve.
            enqueue(dataset, session_id, entry, kind, coding=True)
        else:
            append_warmup_entry(dataset, session_id, entry)
            # Keeps the improve gate's "no new entries" check truthful; the
            # every-N improve this counter used to trigger is off in coding mode.
            bump_turn_counter(session_id)
        bump_save_counter(session_id, kind, buffered=True)


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


def _fetch_order(missing: list) -> list:
    """``(row, metadata, data_id)`` items without a cached body, in download order.

    The listing carries each lesson's metadata but not its text, so the bodies
    the block always shows go first: the newest ``test-baseline``, then the
    pitfalls (newest of each state key first), then the rest, newest first.
    """

    def newest(item) -> tuple:
        row, metadata, _ = item
        created = str(row.get("createdAt") or row.get("created_at") or "")
        return (str(metadata.get("session_date") or ""), created)

    ordered = sorted(missing, key=newest, reverse=True)
    baseline = [i for i in ordered if i[1].get("state_key") == "test-baseline"][:1]
    pitfalls, later, keys = [], [], set()
    for item in ordered:
        if baseline and item is baseline[0]:
            continue
        key = str(item[1].get("state_key") or "")
        if item[1].get("lesson_type") == "pitfall" and not (key and key in keys):
            if key:
                keys.add(key)
            pitfalls.append(item)
        else:
            later.append(item)
    return baseline + pitfalls + later


def list_lessons(
    dataset: str, project: list, deadline: float, *, max_fetches: int = MAX_BODY_FETCHES_ON_PROMPT
) -> tuple:
    """(dataset id, lessons) of this project, newest first, bodies from the cache.

    A row is a lesson when its ``external_metadata`` has ``lesson_type`` (cognee
    #6568). Bodies missing from the cache are fetched until ``max_fetches`` or the
    deadline, those the block always shows first (``_fetch_order``); lessons
    still without a body are left out this time.
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
    candidates = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        metadata = row.get("externalMetadata", row.get("external_metadata"))
        metadata = metadata if isinstance(metadata, dict) else {}
        if not metadata.get("lesson_type") or not _project_ok(metadata, project):
            continue
        data_id = str(row.get("id") or "")
        if data_id:
            candidates.append((row, metadata, data_id))
    bodies = {data_id: _cached_body(data_id) for _, _, data_id in candidates}
    missing = [item for item in candidates if bodies[item[2]] is None]
    fetched = 0
    for _, _, data_id in _fetch_order(missing):
        if fetched >= max_fetches or time.monotonic() >= deadline:
            break
        fetched += 1
        try:
            bodies[data_id] = _raw_text(dataset_id, data_id, _remaining(deadline))
            _store_body(data_id, bodies[data_id])
        except Exception as exc:
            hook_log("coding_lesson_fetch_failed", {"data_id": data_id, "error": str(exc)[:200]})
    lessons = []
    for row, metadata, data_id in candidates:
        text = bodies.get(data_id)
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


def recalled_passages(rows, query: str = "") -> list:
    """The ``## Relevant passages`` of the graph items of a recall reply, in the
    server's order (its relevance order).

    Read as the per-prompt recall reads them (session-context-lookup.py): the
    item's ``text`` (or ``content``), cut down to the retrieved context when it
    is the server's full prompt, split into sections and passages. The
    preamble, entities and facts are left out: an entity line may quote a
    lesson without the lesson having been recalled.
    """
    from _recall_text import split_only_context, split_passages, split_sections

    rows = [rows] if isinstance(rows, dict) else rows if isinstance(rows, list) else []
    passages = []
    for row in rows:
        if not isinstance(row, dict) or row.get("source") not in ("graph", "graph_context"):
            continue
        text = str(row.get("text") or row.get("content") or "")
        context = split_only_context(text, query)["context"]
        block = split_sections(context).get("passages")
        if block:
            passages.extend(split_passages(block))
    return passages


def recall_passages(query: str, dataset: str, dataset_id: str, deadline: float) -> list:
    """The passages the graph recall found over the lessons, most relevant first;
    [] on any error (404 = empty)."""
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
        return []
    return recalled_passages(rows, query)


def _match_key(body: str) -> str:
    return _norm(body)[:RECALL_MATCH_PREFIX]


def _passage_body(passage: str) -> str:
    """The lesson statement of a recalled passage.

    The server may open a passage with a ``source: <document>`` line (and
    ``key: value`` metadata lines) above the chunk's text; those are skipped
    when a lesson header or title follows within the first lines.
    """
    lines = str(passage or "").splitlines()
    for index, line in enumerate(lines[:12]):
        if _HEADER_RE.match(line.strip()) or _TITLE_RE.match(line.strip()):
            return parse_lesson_text("\n".join(lines[index:]))["body"]
    return parse_lesson_text(passage)["body"]


def select_lessons(
    lessons: list,
    recalled: list,
    budget: int = LESSON_BUDGET,
    *,
    line_for=None,
    max_candidates: int | None = None,
    lines_cap: int = LESSON_LINES_CAP,
) -> list:
    """The newest ``test-baseline``, the two newest pitfalls, then recalled lessons.

    ``lessons`` is newest first; ``recalled`` the recall's passages, most
    relevant first. A passage recalls the lesson whose statement starts the
    same (``RECALL_MATCH_PREFIX`` characters, ignoring case and whitespace).
    Only the newest lesson of a state key counts; duplicates are skipped. Each
    candidate is measured twice: its line without a git tag against ``budget``
    (what the experiment measured), and the line it renders to (``line_for``,
    default without a tag) against ``lines_cap``. One that does not fit either
    is skipped and a later, shorter one may still fit. At most
    ``max_candidates`` are measured (each measure may run git).
    """
    if line_for is None:

        def line_for(lesson):
            return lesson_line(lesson, "", False)

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
    by_key: dict = {}
    for lesson in current:
        by_key.setdefault(_match_key(lesson["body"]), lesson)
    for passage in recalled or []:
        key = _match_key(_passage_body(passage))
        if key and key in by_key:
            ordered.append(by_key[key])

    chosen, used, used_lines, seen = [], 0, 0, set()
    for lesson in ordered:
        normalized = _norm(lesson["body"])
        if normalized in seen:
            continue
        if max_candidates is not None and len(seen) >= max_candidates:
            break
        seen.add(normalized)
        size = len(lesson_line(lesson, "", False)) + 1
        if used + size > budget:
            continue
        line_size = len(line_for(lesson)) + 1
        if used_lines + line_size > lines_cap:
            continue
        chosen.append(lesson)
        used += size
        used_lines += line_size
    return chosen


# -- Git tags -----------------------------------------------------------------------------


def _match(name: str, paths: list) -> list:
    """Paths equal to ``name`` or ending with ``/name``, case-insensitive."""
    lowered = name.lower()
    while lowered.startswith("./"):
        lowered = lowered.removeprefix("./")
    if not lowered:
        return []
    return [p for p in paths if p.lower() == lowered or p.lower().endswith("/" + lowered)]


def _kind(names: list, label: str) -> str:
    if not names:
        return ""
    shown = ", ".join(names[:2])
    more = len(names) - 2
    return f"{shown} +{more} more {label}" if more > 0 else f"{shown} {label}"


class _GitView:
    """Git answers for one block build: ``ls-files`` runs once, and each commit's
    short hash, diff and tree once, however many lessons cite it."""

    def __init__(self, cwd: str):
        self.cwd = cwd
        self._now: list | None = None
        self._commits: dict = {}

    def now_paths(self) -> list:
        if self._now is None:
            self._now = [p for p in _git(self.cwd, "ls-files").splitlines() if p]
        return self._now

    def short(self, commit: str) -> str:
        """The commit's short hash, or "" when it is unknown here."""
        key = ("short", commit)
        if key not in self._commits:
            cwd = self.cwd
            known = _git(cwd, "rev-parse", "--verify", "--quiet", commit + "^{commit}").strip()
            short = _git(cwd, "rev-parse", "--short", commit).strip() if known else ""
            self._commits[key] = (short or commit[:7]) if known else ""
        return self._commits[key]

    def changes(self, commit: str) -> tuple:
        """``(paths changed since commit, paths in commit)``."""
        key = ("changes", commit)
        if key not in self._commits:
            cwd = self.cwd
            changed = [p for p in _git(cwd, "diff", "--name-only", commit).splitlines() if p]
            then = [p for p in _git(cwd, "ls-tree", "-r", "--name-only", commit).splitlines() if p]
            self._commits[key] = (changed, then)
        return self._commits[key]


def lesson_tag(lesson: dict, cwd: str, view: _GitView | None = None) -> str:
    """``(session <date>, commit <short>; <files> changed since; <files> no longer exists)``.

    Empty outside git, without a commit, or when the commit is unknown here.
    ``view`` shares git answers across the lessons of one block.
    """
    commit = str(lesson.get("git_commit") or "").strip()
    if not commit or not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit):
        return ""
    view = view or _GitView(cwd)
    short = view.short(commit)
    if not short:
        return ""
    changed_paths, then_paths, now_paths = [], [], []
    if lesson.get("files"):
        changed_paths, then_paths = view.changes(commit)
        now_paths = view.now_paths()
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


def lesson_line(lesson: dict, cwd: str, in_git: bool, view: _GitView | None = None) -> str:
    """``- <statement> <git tag>``: the statement on one line, the tag only in git."""
    body = " ".join(str(lesson.get("body") or "").split())
    tag = lesson_tag(lesson, cwd, view) if in_git else ""
    return f"- {body} {tag}".rstrip()


def _line_maker(cwd: str, in_git: bool):
    """``lesson_line`` for one block build: each lesson's line made once, git
    answers shared, so selection measures the line that is rendered."""
    view = _GitView(cwd) if in_git else None
    made: dict = {}

    def line_for(lesson: dict) -> str:
        key = id(lesson)
        if key not in made:
            made[key] = lesson_line(lesson, cwd, in_git, view)
        return made[key]

    return line_for


def render_block(
    dataset: str, sessions: int, lessons: list, cwd: str, in_git: bool, line_for=None
) -> str:
    """The block for the lessons ``select_lessons`` chose; it prints them all
    (the budget was spent at selection) and caps only the commit list."""
    if not lessons:
        return ""
    line_for = line_for or _line_maker(cwd, in_git)
    noun = "1 earlier session" if sessions == 1 else f"{sessions} earlier sessions"
    framing = FRAMING.format(sessions=noun, dataset=dataset)[:FRAMING_CAP]
    parts = [framing, "Lessons:", *(line_for(lesson) for lesson in lessons)]
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
    recalled = recall_passages(query, dataset, dataset_id, deadline) if query.strip() else []
    # The repository root, from this call's cwd: git prints root-relative paths
    # from there, as lessons name them (cwd can change mid-session: no cache).
    root = git_root(cwd)
    in_git = bool(root)
    root = root or cwd
    line_for = _line_maker(root, in_git)
    chosen = select_lessons(
        lessons,
        recalled,
        line_for=line_for,
        max_candidates=MAX_TAGGED_CANDIDATES if in_git else None,
    )
    sessions = len(
        {
            item["session_id"]
            for item in lessons
            if item["session_id"] and item["session_id"] != session_id
        }
    )
    return render_block(dataset, max(1, sessions), chosen, root, in_git, line_for)


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
    """The newest request's sanitized prompt, as the prompt hook recorded it
    (the lessons block is given again for it after a compaction)."""
    state = read_state(session_key)
    turns = [t for t in state.get("turns") or [] if isinstance(t, dict)]
    turn = turns[-1] if turns else state.get("turn")  # "turn": the earlier single slot
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
