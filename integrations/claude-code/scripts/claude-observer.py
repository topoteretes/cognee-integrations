#!/usr/bin/env python3
"""OpenAI-compatible shim that answers chat completions with the ``claude`` CLI.

The local Cognee server needs an LLM. This process lets it use the Claude
subscription the user already has for Claude Code: it listens on loopback,
accepts OpenAI ``/v1/chat/completions`` requests (what cognee's ``custom``
provider emits through litellm), and answers each by running

    claude -p --safe-mode --no-session-persistence --output-format json ...

``--safe-mode`` disables every customization (hooks, plugins, CLAUDE.md, MCP,
skills) so a call from here can never re-enter this plugin's hooks, while
auth still works normally — unlike ``--bare``, which refuses OAuth. Every
child also carries ``COGNEE_OBSERVER_CHILD=1``, which the hook scripts treat
as "exit immediately", as a second fence. Structured output goes through the
CLI's ``--json-schema`` when the request carries a JSON schema (instructor's
``json_schema_mode``, which the plugin configures for cognee), so the JSON
cognee parses is validated by Claude Code itself rather than hoped for.

Stdlib only; it must run on the same host python the hooks use.

Usage:
    claude-observer.py serve [--port N] [--cognee-url URL]
    claude-observer.py start | stop | status | probe

Endpoints (all but ``/health`` need ``Authorization: Bearer <token>``, the
token in ``~/.cognee-plugin/observer/token`` that cognee gets as its
``LLM_API_KEY``; any request carrying an ``Origin`` header is refused, so a web
page cannot drive the shim):
    GET  /health                  liveness + a few counters
    GET  /v1/models
    POST /v1/chat/completions     (stream: true answered as a single SSE chunk)
    POST /v1/embeddings           501 — embeddings run on fastembed, not here
    GET  /v1/observer/probe       auth verdict (cached; ?force=1 re-runs)
    POST /v1/observer/shutdown

Self-retiring: with ``--cognee-url`` it polls that server's ``/health`` and
exits once the server has been gone for a few minutes (the server itself exits
when its last agent disconnects), so nothing lingers after the last session.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _observer  # noqa: E402
from _logfiles import append_line as _append_log_line  # noqa: E402

_LOG = _observer.LOG_FILE
_EVENTS = _observer._STATE_DIR / "observer-events.log"

#: Concurrent ``claude`` children. Each is a Node process; two keep cognify's
#: fan-out moving without turning the machine into a fan.
_CONCURRENCY = max(1, int(os.environ.get("COGNEE_OBSERVER_CONCURRENCY", "") or 2))
#: Wall clock per call. Extraction prompts run long on big chunks; the plugin's
#: improve submit timeout is 420s, so a call must give up well inside that.
_CALL_TIMEOUT = float(os.environ.get("COGNEE_OBSERVER_TIMEOUT", "") or 240.0)
#: How long the auth probe verdict is reused.
_PROBE_TTL = float(os.environ.get("COGNEE_OBSERVER_PROBE_TTL", "") or 300.0)
#: Retire once the served cognee server has been absent this long.
_RETIRE_AFTER = float(os.environ.get("COGNEE_OBSERVER_RETIRE_AFTER", "") or 180.0)
#: ...or was never seen at all within this long after start.
_NEVER_SEEN_GRACE = 900.0
#: Beyond this the schema goes into the prompt instead of argv.
_MAX_ARGV_SCHEMA = 60_000

_AUTH_RE = re.compile(
    r"(?i)(not logged in|please (?:run )?/?login|invalid api key|authentication|unauthori[sz]ed|"
    r"oauth|token (?:has )?expired|401)"
)
_RATE_RE = re.compile(r"(?i)(rate limit|usage limit|too many requests|overloaded|429|529)")
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.S)


def _log(event: str, **detail) -> None:
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pid": os.getpid(),
        "event": event,
        "event_name": "observer." + event,
        "event_schema": 2,
    }
    if detail:
        line["detail"] = detail
    _append_log_line(_EVENTS, json.dumps(line, default=str)[:1500])


# --- request translation -----------------------------------------------------


def _text_of(content) -> str:
    """OpenAI message content: a string, or a list of typed parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict):
                if part.get("type") in (None, "text", "input_text") and part.get("text"):
                    parts.append(str(part["text"]))
            elif isinstance(part, str):
                parts.append(part)
        return "\n".join(parts)
    return "" if content is None else str(content)


def split_messages(messages: list) -> tuple[str, str]:
    """(system prompt, user prompt) from an OpenAI messages array.

    System/developer messages join into the system prompt. A single user turn
    is passed verbatim; a longer conversation is flattened with role labels so
    the model sees the whole exchange in one prompt (the CLI takes one turn).
    """
    system_parts: list[str] = []
    turns: list[tuple[str, str]] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        text = _text_of(message.get("content"))
        if role in ("system", "developer"):
            if text:
                system_parts.append(text)
        elif role in ("user", "assistant", "tool", "function"):
            if text or role != "assistant":
                turns.append((role, text))
    if len(turns) == 1:
        prompt = turns[0][1]
    else:
        labels = {
            "user": "User",
            "assistant": "Assistant",
            "tool": "Tool result",
            "function": "Tool result",
        }
        prompt = "\n\n".join(f"{labels.get(role, role)}: {text}" for role, text in turns)
        if turns and turns[-1][0] != "user":
            prompt += "\n\nAssistant:"
    return "\n\n".join(system_parts), prompt


def extract_schema(body: dict) -> tuple[dict | None, bool]:
    """(schema, json_wanted) from ``response_format``.

    ``json_schema`` carries the schema itself; ``json_object`` only says "reply
    in JSON". A forced single tool (``tool_choice`` naming it) is treated as a
    schema request for that tool's parameters — the closest OpenAI-tools
    equivalent — and the reply is then returned as that tool's call.
    """
    fmt = body.get("response_format")
    if isinstance(fmt, dict):
        kind = str(fmt.get("type") or "")
        if kind == "json_schema":
            spec = fmt.get("json_schema")
            schema = spec.get("schema") if isinstance(spec, dict) else None
            if isinstance(schema, dict):
                return schema, True
            return None, True
        if kind == "json_object":
            return None, True
    return None, False


def forced_tool(body: dict) -> dict | None:
    """The single tool the caller insists on, or None."""
    choice = body.get("tool_choice")
    tools = body.get("tools") or []
    name = ""
    if isinstance(choice, dict):
        name = str(((choice.get("function") or {}).get("name")) or "")
    elif choice == "required" and len(tools) == 1:
        name = str(((tools[0].get("function") or {}).get("name")) or "")
    if not name:
        return None
    for tool in tools:
        fn = tool.get("function") if isinstance(tool, dict) else None
        if isinstance(fn, dict) and fn.get("name") == name:
            return fn
    return None


def resolve_model(requested: str) -> str:
    """Map cognee's alias to the configured Claude model; pass real names through."""
    name = str(requested or "").strip()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    # A name that cannot be a model (see _observer._MODEL_RE) never reaches
    # `claude --model`; it gets the configured model instead.
    if not name or name == _observer.MODEL_ALIAS or not _observer.valid_model(name):
        return _observer.claude_model()
    return name


def strip_fences(text: str) -> str:
    match = _FENCE_RE.match(text or "")
    return match.group(1) if match else (text or "")


def classify_failure(text: str) -> int:
    """HTTP status for a failed CLI run, from its error text."""
    if _AUTH_RE.search(text or ""):
        return 401
    if _RATE_RE.search(text or ""):
        return 429
    return 502


#: Variables that tie a process to the Claude Code session that launched it. A
#: child inheriting them may refuse to start ("cannot run inside Claude Code"),
#: attach to the parent's session or resolve the parent's plugin root.
_PARENT_SESSION_ENV = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_PID",
        "CLAUDE_EFFORT",
        "CLAUDE_ENV_FILE",
        "CLAUDE_PROJECT_DIR",
        "CLAUDE_PLUGIN_ROOT",
        "CLAUDE_PLUGIN_DATA",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_CHILD_SESSION",
    }
)
_PARENT_SESSION_PREFIXES = ("CLAUDE_CODE_SESSION_", "CLAUDE_CODE_MESSAGING_")
#: Would make the child bill an API key instead of the subscription the
#: observer promises.
_BILLING_ENV = frozenset({"ANTHROPIC_API_KEY"})


def child_env() -> dict:
    """Environment for the ``claude`` child: no trace of the parent Claude session.

    Only the parent-session variables are dropped. Everything else Claude Code
    needs to authenticate stays: ``CLAUDE_CONFIG_DIR`` (where the credentials
    live), ``CLAUDE_CODE_OAUTH_TOKEN`` (``claude setup-token`` logins) and the
    Bedrock/Vertex switches. ``ANTHROPIC_API_KEY`` is dropped so a key in the
    user's shell cannot quietly move the bill off the subscription.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _PARENT_SESSION_ENV
        and key not in _BILLING_ENV
        and not key.startswith(_PARENT_SESSION_PREFIXES)
    }
    env[_observer.CHILD_ENV_FLAG] = "1"
    return env


# --- the CLI call ------------------------------------------------------------


class ObserverError(Exception):
    def __init__(self, status: int, message: str, kind: str = "observer_error"):
        super().__init__(message)
        self.status = status
        self.kind = kind


def _run_claude(
    claude: str,
    *,
    model: str,
    system_prompt: str,
    prompt: str,
    schema: dict | None,
    timeout: float,
) -> dict:
    """One headless call. Returns the CLI's JSON result document."""
    args = [
        claude,
        "-p",
        "--safe-mode",
        "--no-session-persistence",
        "--output-format",
        "json",
        "--model",
        model,
        "--tools",
        "",
    ]
    tmp_path = ""
    if system_prompt:
        fd, tmp_path = tempfile.mkstemp(
            prefix="cognee-observer-", suffix=".txt", dir=str(_observer._STATE_DIR)
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(system_prompt)
        args += ["--system-prompt-file", tmp_path]
    if schema is not None:
        encoded = json.dumps(schema, separators=(",", ":"))
        if len(encoded) <= _MAX_ARGV_SCHEMA:
            args += ["--json-schema", encoded]
        else:
            prompt = (
                f"{prompt}\n\nReply with exactly one JSON document matching this schema:\n{encoded}"
            )
    try:
        proc = subprocess.run(
            args,
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=child_env(),
            cwd=str(_observer._STATE_DIR),
        )
    except subprocess.TimeoutExpired as exc:
        raise ObserverError(504, f"claude did not answer within {timeout:.0f}s", "timeout") from exc
    except FileNotFoundError as exc:
        raise ObserverError(
            500, f"claude executable not found at {claude}", "claude_missing"
        ) from exc
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    stdout = proc.stdout or ""
    stderr = (proc.stderr or "").strip()
    doc: dict = {}
    if stdout.strip():
        try:
            parsed = json.loads(stdout)
            if isinstance(parsed, dict):
                doc = parsed
            elif isinstance(parsed, list):
                # stream-json shaped output: take the result record.
                for item in parsed:
                    if isinstance(item, dict) and item.get("type") == "result":
                        doc = item
        except ValueError:
            doc = {}
    if proc.returncode != 0 and not doc:
        text = stderr or stdout.strip()[:400] or f"claude exited {proc.returncode}"
        raise ObserverError(classify_failure(text), text[:600], "cli_failed")
    if doc.get("is_error"):
        text = str(doc.get("result") or doc.get("error") or stderr or "claude reported an error")
        raise ObserverError(
            classify_failure(text), text[:600], str(doc.get("subtype") or "is_error")
        )
    if not doc:
        text = stderr or "claude produced no JSON result"
        raise ObserverError(classify_failure(text), text[:600], "no_result")
    return doc


def _usage_of(doc: dict) -> dict:
    usage = doc.get("usage") if isinstance(doc.get("usage"), dict) else {}
    prompt_tokens = 0
    for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
        try:
            prompt_tokens += int(usage.get(key) or 0)
        except (TypeError, ValueError):
            pass
    try:
        completion_tokens = int(usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        completion_tokens = 0
    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


def complete(body: dict, *, claude: str, timeout: float = _CALL_TIMEOUT) -> dict:
    """Translate one chat-completions request; return the OpenAI response object."""
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ObserverError(400, "messages[] is required", "bad_request")
    system_prompt, prompt = split_messages(messages)
    if not prompt.strip() and not system_prompt.strip():
        raise ObserverError(400, "empty prompt", "bad_request")
    schema, json_wanted = extract_schema(body)
    tool = None
    if schema is None:
        tool = forced_tool(body)
        if tool is not None and isinstance(tool.get("parameters"), dict):
            schema = tool["parameters"]
            json_wanted = True
    if json_wanted and schema is None:
        system_prompt = (system_prompt + "\n\n" if system_prompt else "") + (
            "Reply with a single JSON document and nothing else — no prose, no code fences."
        )
    model = resolve_model(str(body.get("model") or ""))
    started = time.monotonic()
    try:
        doc = _run_claude(
            claude,
            model=model,
            system_prompt=system_prompt,
            prompt=prompt or "(see system prompt)",
            schema=schema,
            timeout=timeout,
        )
    except ObserverError as exc:
        # A schema the CLI rejects (unsupported keyword) is not the caller's
        # fault: retry once with the schema folded into the prompt.
        if schema is not None and exc.status == 502 and "schema" in str(exc).lower():
            encoded = json.dumps(schema, separators=(",", ":"))
            doc = _run_claude(
                claude,
                model=model,
                system_prompt=system_prompt,
                prompt=(
                    f"{prompt}\n\nReply with exactly one JSON document matching this schema:\n"
                    f"{encoded}"
                ),
                schema=None,
                timeout=max(5.0, timeout - (time.monotonic() - started)),
            )
        else:
            raise
    structured = doc.get("structured_output")
    if structured is not None and schema is not None:
        content = json.dumps(structured, ensure_ascii=False)
    else:
        content = str(doc.get("result") or "")
        if json_wanted:
            content = strip_fences(content)
    message: dict = {"role": "assistant", "content": content}
    finish = "stop"
    if tool is not None:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_" + uuid.uuid4().hex[:24],
                    "type": "function",
                    "function": {"name": str(tool.get("name") or ""), "arguments": content},
                }
            ],
        }
        finish = "tool_calls"
    used_model = ""
    model_usage = doc.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        used_model = next(iter(model_usage))
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": str(body.get("model") or _observer.COGNEE_MODEL),
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": _usage_of(doc),
        "system_fingerprint": "cognee-claude-observer",
        "cognee_observer": {
            "claude_model": used_model or model,
            "duration_ms": int((time.monotonic() - started) * 1000),
            "cost_usd": doc.get("total_cost_usd"),
        },
    }


# --- server ------------------------------------------------------------------


class State:
    def __init__(self, claude: str, cognee_url: str, token: str = ""):
        self.claude = claude
        self.token = token
        self.cognee_url = cognee_url.rstrip("/")
        self.started = time.time()
        self.gate = threading.BoundedSemaphore(_CONCURRENCY)
        self.lock = threading.Lock()
        self.inflight = 0
        self.calls = 0
        self.failures = 0
        self.last_error = ""
        self.probe: dict = {}
        self.probe_lock = threading.Lock()
        self.stop = threading.Event()
        self.server_seen_at = 0.0

    def record(self, ok: bool, error: str = "") -> None:
        with self.lock:
            self.calls += 1
            if not ok:
                self.failures += 1
                self.last_error = error[:300]

    def run_probe(self, force: bool = False) -> dict:
        """Auth verdict: one tiny call, cached for _PROBE_TTL. A completion that
        just succeeded counts as a fresh 'ok' too (``note_success``)."""
        with self.probe_lock:
            cached = self.probe
            if (
                not force
                and cached
                and time.time() - float(cached.get("checked_at") or 0) < _PROBE_TTL
            ):
                return cached
            verdict = {"auth": "unknown", "detail": "", "checked_at": time.time()}
            try:
                doc = _run_claude(
                    self.claude,
                    model=_observer.claude_model(),
                    system_prompt="Reply with the single word OK.",
                    prompt="ping",
                    schema=None,
                    timeout=min(60.0, _CALL_TIMEOUT),
                )
                verdict["auth"] = "ok"
                verdict["detail"] = str(doc.get("result") or "")[:80]
            except ObserverError as exc:
                verdict["auth"] = "failed" if exc.status == 401 else "unknown"
                verdict["detail"] = str(exc)[:300]
                verdict["status"] = exc.status
            except Exception as exc:  # pragma: no cover - defensive
                verdict["detail"] = str(exc)[:300]
            self.probe = verdict
            _log("probe", **{k: v for k, v in verdict.items() if k != "checked_at"})
            return verdict

    def note_success(self) -> None:
        with self.probe_lock:
            self.probe = {"auth": "ok", "detail": "completion succeeded", "checked_at": time.time()}

    def note_auth_failure(self, detail: str) -> None:
        with self.probe_lock:
            self.probe = {
                "auth": "failed",
                "detail": detail[:300],
                "checked_at": time.time(),
                "status": 401,
            }


# How much of a refused (403/401) request body is read and discarded so the
# client receives the refusal; a larger one only gets the connection closed.
_REFUSED_BODY_LIMIT = 1 << 20


class Handler(BaseHTTPRequestHandler):
    server_version = "cognee-claude-observer/1"
    state: State  # set on the class by serve()
    _body: bytes | None = None

    def log_message(self, fmt, *args):  # silence the default stderr access log
        return

    # -- helpers
    def _send_json(self, status: int, payload: dict) -> None:
        data = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _send_error(self, status: int, message: str, kind: str) -> None:
        self._send_json(
            status,
            {"error": {"message": message, "type": kind, "code": status, "param": None}},
        )

    def _content_length(self) -> int:
        try:
            return max(0, int(self.headers.get("Content-Length") or 0))
        except ValueError:
            return 0

    def _consume_body(self) -> None:
        """Read the request body off the socket, once, before any response.

        Answering without reading it leaves bytes in the receive buffer, and on
        Windows closing such a socket sends a TCP reset: the client gets
        WinError 10053/10054 instead of the status it was sent (a 401 for a
        wrong token read as a dropped connection). Refused requests are
        drained only up to _REFUSED_BODY_LIMIT; past that the connection is
        closed without reading, which is the correct cost for an oversized
        unauthenticated body.
        """
        if getattr(self, "_body", None) is not None:
            return
        length = self._content_length()
        self._body = self.rfile.read(length) if length else b""

    def _discard_refused_body(self) -> None:
        length = self._content_length()
        if length > _REFUSED_BODY_LIMIT:
            self.close_connection = True
            return
        self._consume_body()

    def _read_body(self) -> dict:
        self._consume_body()
        raw = self._body
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise ObserverError(400, "request body is not JSON", "bad_request") from exc
        return parsed if isinstance(parsed, dict) else {}

    def _path(self) -> tuple[str, dict]:
        path, _, query = self.path.partition("?")
        params = {}
        for part in query.split("&"):
            if "=" in part:
                key, value = part.split("=", 1)
                params[key] = value
            elif part:
                params[part] = "1"
        return path.rstrip("/") or "/", params

    def _authorized(self, path: str) -> bool:
        """Refuse browser-originated requests outright, and anything but ``/health``
        without the bearer token. Sends the error response when refusing."""
        if self.headers.get("Origin"):
            self._discard_refused_body()
            self._send_error(403, "cross-origin requests are not accepted", "forbidden")
            return False
        if path == "/health":
            return True
        header = str(self.headers.get("Authorization") or "")
        scheme, _, supplied = header.partition(" ")
        token = self.state.token
        if token and scheme.lower() == "bearer" and hmac.compare_digest(supplied.strip(), token):
            return True
        self._discard_refused_body()
        self._send_error(401, "missing or invalid observer token", "unauthorized")
        return False

    # -- routes
    def do_GET(self):
        path, params = self._path()
        state = self.state
        if not self._authorized(path):
            return
        if path == "/health":
            with state.lock:
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "cognee-claude-observer",
                        "claude": state.claude,
                        "model": _observer.claude_model(),
                        "cognee_url": state.cognee_url,
                        "uptime_s": int(time.time() - state.started),
                        "inflight": state.inflight,
                        "calls": state.calls,
                        "failures": state.failures,
                        "last_error": state.last_error,
                        "auth": state.probe.get("auth", "unknown"),
                        "pid": os.getpid(),
                    },
                )
            return
        if path == "/v1/models":
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": _observer.MODEL_ALIAS,
                            "object": "model",
                            "created": int(state.started),
                            "owned_by": "cognee-claude-observer",
                        }
                    ],
                },
            )
            return
        if path == "/v1/observer/probe":
            verdict = state.run_probe(force=params.get("force") in ("1", "true"))
            status = {"ok": 200, "failed": 401}.get(str(verdict.get("auth")), 503)
            self._send_json(status, {k: v for k, v in verdict.items() if k != "checked_at"})
            return
        self._send_error(404, f"no route for GET {path}", "not_found")

    def do_POST(self):
        path, _ = self._path()
        state = self.state
        if not self._authorized(path):
            return
        # Every route below may answer without parsing the body (shutdown,
        # 404, 501); read it first so the reply is not lost to a reset.
        self._consume_body()
        try:
            if path == "/v1/observer/shutdown":
                self._send_json(200, {"status": "stopping"})
                state.stop.set()
                return
            if path == "/v1/embeddings":
                raise ObserverError(
                    501,
                    "the Claude observer serves chat completions only; embeddings run on "
                    "fastembed (EMBEDDING_PROVIDER=fastembed)",
                    "not_implemented",
                )
            if path != "/v1/chat/completions":
                raise ObserverError(404, f"no route for POST {path}", "not_found")
            body = self._read_body()
            with state.lock:
                state.inflight += 1
            try:
                with state.gate:
                    response = complete(body, claude=state.claude)
            finally:
                with state.lock:
                    state.inflight -= 1
            state.record(True)
            state.note_success()
            meta = response.get("cognee_observer") or {}
            _log(
                "completion",
                model=meta.get("claude_model"),
                duration_ms=meta.get("duration_ms"),
                tokens=response.get("usage", {}).get("total_tokens"),
                structured=bool(extract_schema(body)[0] is not None),
            )
            if body.get("stream"):
                self._send_stream(response)
            else:
                self._send_json(200, response)
        except ObserverError as exc:
            if path == "/v1/chat/completions":
                state.record(False, str(exc))
                if exc.status == 401:
                    state.note_auth_failure(str(exc))
                _log("completion_failed", status=exc.status, kind=exc.kind, error=str(exc)[:300])
            self._send_error(exc.status, str(exc), exc.kind)
        except Exception as exc:  # pragma: no cover - defensive
            state.record(False, str(exc))
            _log("handler_exception", error=str(exc)[:300])
            self._send_error(500, str(exc)[:300], "internal_error")

    def _send_stream(self, response: dict) -> None:
        """The whole answer as one SSE chunk, then [DONE]. Clients that asked for
        streaming get the OpenAI wire shape; the CLI itself does not stream here."""
        choice = response["choices"][0]
        chunk = {
            "id": response["id"],
            "object": "chat.completion.chunk",
            "created": response["created"],
            "model": response["model"],
            "choices": [{"index": 0, "delta": choice["message"], "finish_reason": None}],
        }
        final = {
            **chunk,
            "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
            "usage": response.get("usage"),
        }
        payload = (
            f"data: {json.dumps(chunk)}\n\ndata: {json.dumps(final)}\n\ndata: [DONE]\n\n"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _cognee_alive(url: str) -> bool:
    if not url:
        return False
    try:
        with urllib.request.urlopen(url + "/health", timeout=2.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def _retire_loop(state: State) -> None:
    """Exit once the cognee server we serve is gone (or never showed up)."""
    while not state.stop.wait(30.0):
        if not state.cognee_url:
            continue
        now = time.time()
        if _cognee_alive(state.cognee_url):
            state.server_seen_at = now
            continue
        with state.lock:
            busy = state.inflight > 0
        if busy:
            continue
        if state.server_seen_at and now - state.server_seen_at > _RETIRE_AFTER:
            _log("retire", reason="cognee_server_gone", idle_s=int(now - state.server_seen_at))
            state.stop.set()
        elif not state.server_seen_at and now - state.started > _NEVER_SEEN_GRACE:
            _log("retire", reason="cognee_server_never_seen")
            state.stop.set()


def serve(port: int, cognee_url: str) -> int:
    claude = _observer.find_claude()
    if not claude:
        print("claude-observer: no `claude` executable found", file=sys.stderr)
        return 2
    _observer._STATE_DIR.mkdir(parents=True, exist_ok=True)
    token = _observer.observer_token()
    if not token:
        print("claude-observer: cannot create the observer token", file=sys.stderr)
        return 1
    state = State(claude, cognee_url, token)
    Handler.state = state
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as exc:
        if _observer.observer_alive(port):
            print(f"claude-observer: already serving on {port}", file=sys.stderr)
            return 0
        print(f"claude-observer: cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        return 1
    httpd.daemon_threads = True
    try:
        _observer.PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass

    def _on_signal(signum, _frame):
        _log("signal", signum=signum)
        state.stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _on_signal)
        except (ValueError, OSError):  # not the main thread / unsupported
            pass
    threading.Thread(
        target=_retire_loop, args=(state,), name="observer-retire", daemon=True
    ).start()
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.5}, daemon=True).start()
    _log("started", port=port, claude=claude, model=_observer.claude_model(), cognee_url=cognee_url)
    print(f"claude-observer: serving http://127.0.0.1:{port}/v1 via {claude}", file=sys.stderr)
    try:
        while not state.stop.wait(1.0):
            pass
    finally:
        httpd.shutdown()
        httpd.server_close()
        try:
            if _observer._read_pid() == os.getpid():
                _observer.PIDFILE.unlink()
        except OSError:
            pass
        _log("stopped", calls=state.calls, failures=state.failures)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command")
    p_serve = sub.add_parser("serve", help="run the shim in the foreground")
    p_serve.add_argument("--port", type=int, default=_observer.port())
    p_serve.add_argument("--cognee-url", default=os.environ.get("COGNEE_BASE_URL", ""))
    p_start = sub.add_parser("start", help="start the shim detached (idempotent)")
    p_start.add_argument("--cognee-url", default=os.environ.get("COGNEE_BASE_URL", ""))
    sub.add_parser("stop", help="stop a running shim")
    sub.add_parser("status", help="print the shim's /health")
    p_probe = sub.add_parser("probe", help="ask the shim whether claude can answer")
    p_probe.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)

    if args.command == "serve":
        return serve(args.port, args.cognee_url)
    if args.command == "start":
        decision = _observer.resolve_observer()
        if not decision["active"]:
            # `start` is an explicit request: only the CLI's absence stops it.
            if not decision["claude"]:
                print("claude-observer: " + _observer.describe(decision), file=sys.stderr)
                return 2
            decision["active"] = True
        ok = _observer.ensure_observer_running(decision, cognee_url=args.cognee_url)
        print(json.dumps(_observer.observer_health() or {"status": "failed"}))
        return 0 if ok else 1
    if args.command == "stop":
        return 0 if _observer.stop_observer() else 1
    if args.command == "status":
        health = _observer.observer_health()
        print(json.dumps(health or {"status": "down", "port": _observer.port()}))
        return 0 if health else 1
    if args.command == "probe":
        verdict = _observer.observer_probe(force=args.force)
        print(json.dumps(verdict))
        return 0 if verdict.get("auth") == "ok" else 1
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
