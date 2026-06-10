"""Shared helpers across plugin hook scripts.

Kept deliberately small: user resolution, runtime-state read, a
single log-to-disk helper. Hook scripts shouldn't grow heavy because
they run on every user prompt / tool call.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
_HOOK_LOG = _PLUGIN_DIR / "hook.log"
_COUNTER_FILE = _PLUGIN_DIR / "counter.json"
_ACTIVITY_FILE = _PLUGIN_DIR / "activity.ts"
_ACTIVITY_LOG = _PLUGIN_DIR / "activity.log"
_SAVE_COUNTER = _PLUGIN_DIR / "save_counter.json"
_SERVER_READY_MARKER = Path.home() / ".cognee-plugin" / "server-ready.json"
_SERVER_READY_TTL_SECONDS = 30
_SYNC_LOCK = _PLUGIN_DIR / "sync.lock"
_HTTP_BRIDGE_CACHE = _PLUGIN_DIR / "http_bridge_cache.json"
_HTTP_BRIDGE_STATE = _PLUGIN_DIR / "http_bridge_state.json"
_PENDING_PROMPTS = _PLUGIN_DIR / "pending_prompts.json"
_SUBPROCESS_LOG = _PLUGIN_DIR / "subprocess.log"
_AGENT_KEYS_CACHE = _PLUGIN_DIR / "agent_keys.json"

# Save-kinds tracked per turn. Keep this tuple in sync with bump_save_counter callers.
SAVE_KINDS = ("prompt", "trace", "answer")

# Cap the per-line log size so a noisy tool output doesn't bloat the file.
_LOG_LINE_CAP = 600

# Default auto-improve threshold (tool calls + stops). Env override.
AUTO_IMPROVE_EVERY_DEFAULT = 30
SYNC_LOCK_STALE_SECONDS = 15 * 60
_DEFAULT_LOCAL_SERVICE_URL = "http://localhost:8011"


def _sanitize_session_key(value: str) -> str:
    safe = []
    for ch in str(value or ""):
        if ch.isalnum() or ch in ("-", "_", "."):
            safe.append(ch)
        else:
            safe.append("_")
    return "".join(safe).strip("._")[:120]


def get_session_key() -> str:
    candidates = [
        os.environ.get("COGNEE_SESSION_KEY"),
    ]
    for value in candidates:
        text = _sanitize_session_key(str(value or "").strip())
        if text:
            return text
    return ""


def set_session_key(session_key: str) -> str:
    normalized = _sanitize_session_key(session_key)
    if normalized:
        os.environ["COGNEE_SESSION_KEY"] = normalized
    return normalized


def resolve_session_key_from_payload(payload: dict) -> tuple[str, str]:
    """Resolve session key from a hook payload using known host variants."""
    if not isinstance(payload, dict):
        return "", "missing_payload"

    def _read_path(obj: dict, path: list[str]) -> str:
        cur = obj
        for key in path[:-1]:
            nxt = cur.get(key)
            if not isinstance(nxt, dict):
                return ""
            cur = nxt
        value = cur.get(path[-1])
        return str(value or "").strip() if value is not None else ""

    candidates: list[tuple[str, list[str]]] = [
        ("payload.session_id", ["session_id"]),
        ("payload.sessionId", ["sessionId"]),
        ("payload.session.id", ["session", "id"]),
        ("payload.conversation_id", ["conversation_id"]),
        ("payload.conversationId", ["conversationId"]),
        ("payload.conversation.id", ["conversation", "id"]),
        ("payload.chat_id", ["chat_id"]),
        ("payload.chatId", ["chatId"]),
        ("payload.thread_id", ["thread_id"]),
        ("payload.threadId", ["threadId"]),
        ("payload.transcript.session_id", ["transcript", "session_id"]),
        ("payload.transcript.sessionId", ["transcript", "sessionId"]),
    ]
    for source, path in candidates:
        value = _read_path(payload, path)
        if value:
            return value, source
    return "", "not_found"


def _resolve_agent_name() -> str:
    def _normalize(name: str) -> str:
        raw = str(name or "").strip()
        if raw.endswith("@cognee.agent"):
            raw = raw[: -len("@cognee.agent")]
        suffix = "_codex"
        if raw.endswith(suffix):
            return raw
        return f"{raw}{suffix}"

    env_name = str(os.environ.get("COGNEE_AGENT_NAME") or "").strip()
    if env_name:
        return _normalize(env_name)
    try:
        from config import load_config  # type: ignore

        configured = str(load_config().get("agent_name") or "").strip()
        if configured:
            normalized = _normalize(configured)
            os.environ["COGNEE_AGENT_NAME"] = normalized
            return normalized
    except Exception:
        pass
    return _normalize("codex-agent")


def load_resolved(session_key: str = "") -> dict:
    """Load runtime state from Cognee HTTP endpoints (no file cache)."""
    resolved: dict = {}

    active_session_key = _sanitize_session_key(session_key) or get_session_key()
    if active_session_key:
        resolved["session_key"] = active_session_key

    service_url = _local_api_url().strip()
    if service_url:
        resolved["service_url"] = service_url

    api_key = _api_key().strip()
    if api_key:
        resolved["api_key"] = api_key

    # Resolve caller identity.
    try:
        me = _json_http_request("/api/v1/users/me", method="GET", timeout=10.0)
        if isinstance(me, dict):
            user_id = str(me.get("id") or "").strip()
            if user_id:
                resolved["user_id"] = user_id
    except Exception as exc:
        hook_log("runtime_state_users_me_failed", {"error": str(exc)[:200]})

    # Resolve active connection details for this session when possible.
    try:
        query = ""
        if active_session_key:
            query = f"?agent_session_name={urllib.parse.quote(active_session_key, safe='')}"
        conn = _json_http_request(
            f"/api/v1/agents/connections/me{query}",
            method="GET",
            timeout=10.0,
        )
        if isinstance(conn, dict):
            agent = conn.get("agent") if isinstance(conn.get("agent"), dict) else {}
            if isinstance(agent, dict):
                session_id = str(agent.get("session_id") or "").strip()
                if session_id:
                    resolved["session_id"] = session_id
                agent_session_name = str(agent.get("agent_session_name") or "").strip()
                if agent_session_name:
                    resolved["agent_session_name"] = agent_session_name
                agent_user_id = str(agent.get("user_id") or "").strip()
                if agent_user_id and not resolved.get("user_id"):
                    resolved["user_id"] = agent_user_id
                status = str(agent.get("status") or "").strip().lower()
                resolved["registered"] = status == "active"
                datasets = agent.get("datasets") if isinstance(agent.get("datasets"), list) else []
                for item in datasets:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            resolved["dataset"] = name
                            break
    except Exception as exc:
        hook_log("runtime_state_connection_lookup_failed", {"error": str(exc)[:200]})

    return resolved


def write_resolved(data: dict, session_key: str = "", *, mirror_global: bool = True) -> None:
    # Runtime state now comes from API endpoints, not local resolved files.
    _ = (data, session_key, mirror_global)


def _load_json_file(path: Path) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            hook_log("json_load_failed", {"path": str(path), "error": str(exc)[:200]})
    return {}


def _write_json_file(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        hook_log("json_write_failed", {"path": str(path), "error": str(exc)[:200]})


def _bridge_cache_key(dataset: str, session_id: str) -> str:
    # Keyed by (dataset, session_id) only — deliberately independent of user_id.
    # During lazy-bootstrap warmup the agent isn't registered yet, so user_id is
    # empty at write time but resolves to a real id by drain time; embedding it
    # would strand warmup-buffered entries under a key the drain never reads.
    # session_id already scopes the local bridge buffer, and the graph write
    # still targets the resolved dataset. Avoiding user_id also removes a
    # blocking load_resolved() HTTP call from this hot path.
    return f"{dataset}:{session_id}"


def append_http_bridge_entry(
    dataset: str,
    session_id: str,
    *,
    question: str = "",
    answer: str = "",
    trace: str = "",
) -> None:
    """Keep a tiny local shadow of API-mode session text for graph bridging.

    Local SDK mode already reads Cognee's session cache directly. In API
    mode the cache lives behind the server, so this mirrors the same text
    locally without affecting local mode.
    """
    if not dataset or not session_id:
        return
    if not (question or answer or trace):
        return

    cache = _load_json_file(_HTTP_BRIDGE_CACHE)
    key = _bridge_cache_key(dataset, session_id)
    session_cache = cache.setdefault(key, {"qa": [], "trace": []})
    if question or answer:
        session_cache.setdefault("qa", []).append({"question": question, "answer": answer})
    if trace:
        session_cache.setdefault("trace", []).append(trace)
    _write_json_file(_HTTP_BRIDGE_CACHE, cache)


async def resolve_user(user_id: str):
    """Resolve cached user ID to a User object, or fall back to default."""
    if user_id:
        try:
            from uuid import UUID

            from cognee.modules.users.methods import get_user

            user = await get_user(UUID(user_id))
            if user:
                return user
        except Exception as exc:
            hook_log("resolve_user_failed", {"user_id": user_id, "error": str(exc)[:200]})
    from cognee.modules.users.methods import get_default_user

    return await get_default_user()


def hook_log(event: str, detail: Optional[dict] = None) -> None:
    """Append one structured line to ~/.cognee-plugin/codex/hook.log.

    Safe to call silently — never raises. Use for forensic debugging
    of why a hook did (or did not) write something to memory.
    """
    try:
        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
        }
        if detail:
            line["detail"] = detail
        serialized = json.dumps(line, default=str)
        if len(serialized) > _LOG_LINE_CAP:
            serialized = serialized[: _LOG_LINE_CAP - 3] + "..."
        with _HOOK_LOG.open("a", encoding="utf-8") as fh:
            fh.write(serialized + "\n")
    except Exception:
        pass


def _verbose_enabled() -> bool:
    return os.environ.get("COGNEE_PLUGIN_VERBOSE", "").lower() in ("1", "true", "yes")


def notify(msg: str) -> None:
    """Print a status line to stderr (shown under the hook's status indicator).

    When ``COGNEE_PLUGIN_VERBOSE=1`` is set, also append a timestamped
    line to ``~/.cognee-plugin/codex/activity.log`` so saves that happen
    in async hooks are ``tail -f``-visible.
    """
    line = f"cognee-plugin: {msg}"
    print(line, file=sys.stderr)
    if _verbose_enabled():
        try:
            _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            with _ACTIVITY_LOG.open("a", encoding="utf-8") as fh:
                fh.write(f"{ts} {line}\n")
        except Exception as exc:
            hook_log("activity_log_write_failed", {"error": str(exc)[:200]})


@contextmanager
def quiet_hook_output(label: str):
    """Redirect stdout/stderr to a plugin log while a hook does Cognee work.

    Codex parses stdout for JSON on hooks such as UserPromptSubmit. Some
    Cognee dependencies write directly to file descriptors, so redirect at
    the OS fd level instead of relying only on Python's redirect_stdout.
    """
    _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    saved_stdout_fd = os.dup(1)
    saved_stderr_fd = os.dup(2)
    log_fd = os.open(_SUBPROCESS_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        marker = (
            f"\n--- {datetime.now(timezone.utc).isoformat(timespec='seconds')} "
            f"{label} pid={os.getpid()} ---\n"
        )
        os.write(
            log_fd,
            marker.encode("utf-8"),
        )
        os.dup2(log_fd, 1)
        os.dup2(log_fd, 2)
        yield
    finally:
        os.dup2(saved_stdout_fd, 1)
        os.dup2(saved_stderr_fd, 2)
        os.close(saved_stdout_fd)
        os.close(saved_stderr_fd)
        os.close(log_fd)


def bump_save_counter(session_id: str, kind: str) -> None:
    """Record a save of ``kind`` (one of ``SAVE_KINDS``) for this session.

    Used to surface per-turn save volume back to the user through the
    next UserPromptSubmit's injected context. Cheap, best-effort file IO —
    never raises.
    """
    if not session_id or kind not in SAVE_KINDS:
        return
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8")) if _SAVE_COUNTER.exists() else {}
        )
    except Exception as exc:
        hook_log("save_counter_read_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]})
        data = {}
    sess = data.get(session_id) or {k: 0 for k in SAVE_KINDS}
    sess[kind] = int(sess.get(kind, 0)) + 1
    data[session_id] = sess
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _SAVE_COUNTER.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        hook_log("save_counter_write_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]})


def read_and_reset_save_counter(session_id: str) -> dict:
    """Return the save-kind counts accumulated since the last reset, then zero them."""
    zero = {k: 0 for k in SAVE_KINDS}
    if not session_id:
        return zero
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8")) if _SAVE_COUNTER.exists() else {}
        )
    except Exception as exc:
        hook_log(
            "save_counter_reset_read_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]}
        )
        return zero
    sess = data.get(session_id) or zero
    data[session_id] = dict(zero)
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _SAVE_COUNTER.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        hook_log(
            "save_counter_reset_write_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]}
        )
    return {k: int(sess.get(k, 0)) for k in SAVE_KINDS}


def _pending_keys(session_id: str, turn_id: str = "") -> tuple[str, str]:
    session_key = f"{session_id}:"
    turn_key = f"{session_id}:{turn_id}" if turn_id else session_key
    return turn_key, session_key


def remember_pending_prompt(
    session_id: str, prompt: str, *, turn_id: str = "", context: str = ""
) -> None:
    """Store the current prompt until Codex Stop provides the assistant answer."""
    if not session_id or not prompt.strip():
        return
    data = _load_json_file(_PENDING_PROMPTS)
    turn_key, session_key = _pending_keys(session_id, turn_id)
    entry = {
        "prompt": prompt[:8000],
        "context": context[:2000],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data[turn_key] = entry
    data[session_key] = entry
    _write_json_file(_PENDING_PROMPTS, data)


def pop_pending_prompt(session_id: str, *, turn_id: str = "") -> dict:
    """Return and remove the prompt saved for this Codex turn."""
    if not session_id:
        return {"prompt": "", "context": ""}
    data = _load_json_file(_PENDING_PROMPTS)
    turn_key, session_key = _pending_keys(session_id, turn_id)
    entry = data.pop(turn_key, None) or data.get(session_key) or {}
    data.pop(session_key, None)
    _write_json_file(_PENDING_PROMPTS, data)
    if not isinstance(entry, dict):
        return {"prompt": "", "context": ""}
    return {
        "prompt": str(entry.get("prompt") or ""),
        "context": str(entry.get("context") or ""),
    }


def _auto_improve_threshold() -> int:
    raw = os.environ.get("COGNEE_AUTO_IMPROVE_EVERY", "")
    if raw.isdigit() and int(raw) > 0:
        return int(raw)
    return AUTO_IMPROVE_EVERY_DEFAULT


def bump_turn_counter(session_id: str) -> tuple[int, bool]:
    """Increment the per-session tool-call counter.

    Returns (new_count, should_improve). ``should_improve`` is True when
    the count crossed a multiple of the configured threshold — the
    caller is expected to fire ``improve()`` and proceed.

    Counter survives across hook invocations via a tiny JSON file.
    Concurrent writes: we accept rare off-by-one drift under heavy
    parallel tool use — this is a heartbeat, not a ledger.
    """
    if not session_id:
        return 0, False

    threshold = _auto_improve_threshold()

    data: dict = {}
    if _COUNTER_FILE.exists():
        try:
            data = json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    count = int(data.get(session_id, 0)) + 1
    data[session_id] = count

    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _COUNTER_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        hook_log("turn_counter_write_failed", {"path": str(_COUNTER_FILE), "error": str(exc)[:200]})

    should_improve = threshold > 0 and count % threshold == 0
    return count, should_improve


def touch_activity() -> None:
    """Update the last-activity timestamp for the idle watcher."""
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _ACTIVITY_FILE.write_text(str(datetime.now(timezone.utc).timestamp()), encoding="utf-8")
    except Exception as exc:
        hook_log("activity_touch_failed", {"path": str(_ACTIVITY_FILE), "error": str(exc)[:200]})


@contextmanager
def sync_lock(owner: str):
    """Best-effort cross-hook lock for graph sync/improve work."""
    acquired = False
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).timestamp()
        if _SYNC_LOCK.exists():
            try:
                current = json.loads(_SYNC_LOCK.read_text(encoding="utf-8"))
                created_at = float(current.get("created_at", 0))
                pid = int(current.get("pid", 0))
            except Exception as exc:
                hook_log("sync_lock_read_failed", {"owner": owner, "error": str(exc)[:200]})
                created_at = 0
                pid = 0
            pid_alive = False
            if pid > 0:
                try:
                    os.kill(pid, 0)
                    pid_alive = True
                except PermissionError:
                    pid_alive = True
                except OSError:
                    pid_alive = False
            if not pid_alive or now - created_at > SYNC_LOCK_STALE_SECONDS:
                try:
                    _SYNC_LOCK.unlink()
                except Exception as exc:
                    hook_log("sync_lock_unlink_failed", {"owner": owner, "error": str(exc)[:200]})
        try:
            fd = os.open(str(_SYNC_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
            acquired = True
            yield True
        except FileExistsError:
            hook_log("sync_lock_busy", {"owner": owner})
            yield False
    finally:
        if acquired:
            try:
                _SYNC_LOCK.unlink()
            except Exception as exc:
                hook_log("sync_lock_release_failed", {"owner": owner, "error": str(exc)[:200]})


def _local_api_url() -> str:
    direct = (
        os.environ.get("COGNEE_LOCAL_API_URL") or os.environ.get("COGNEE_SERVICE_URL") or ""
    ).strip()
    if direct:
        return direct
    return _DEFAULT_LOCAL_SERVICE_URL


def _normalize_service_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def _api_key() -> str:
    service_url = _normalize_service_url(_local_api_url())
    agent_name = _resolve_agent_name()
    if not service_url:
        return str(os.environ.get("COGNEE_API_KEY", "") or "").strip()

    cache = _load_json_file(_AGENT_KEYS_CACHE)
    entries = cache.get("entries", {}) if isinstance(cache, dict) else {}
    if isinstance(entries, dict) and agent_name:
        selected: dict | None = None
        cache_key = f"{service_url}::{agent_name}"
        entry = entries.get(cache_key)
        if isinstance(entry, dict):
            selected = entry
        else:
            # Backward compatibility for older cache formats where keys may differ.
            for value in entries.values():
                if not isinstance(value, dict):
                    continue
                url = _normalize_service_url(str(value.get("service_url") or ""))
                name = str(value.get("agent_name") or "").strip()
                if url == service_url and name == agent_name:
                    selected = value
                    break
        if isinstance(selected, dict):
            key = str(selected.get("api_key") or "").strip()
            if key:
                os.environ["COGNEE_API_KEY"] = key
                return key

    # Fallback for bootstrap paths before an agent key is available.
    return str(os.environ.get("COGNEE_API_KEY", "") or "").strip()


def resolved_http_endpoint_auth() -> tuple[str, str]:
    """Return (service_url, api_key) for runtime HTTP calls.

    Service URL always falls back to localhost. API key is resolved from env
    first, then from agent_keys cache by (service_url, agent_name).
    """
    service_url = _normalize_service_url(_local_api_url())
    api_key = _api_key().strip()
    if service_url:
        os.environ["COGNEE_SERVICE_URL"] = service_url
    if api_key:
        os.environ["COGNEE_API_KEY"] = api_key
    return service_url, api_key


def http_api_ready() -> bool:
    service_url, api_key = resolved_http_endpoint_auth()
    return bool(service_url and api_key)


def server_health_ok(service_url: str = "", timeout: float = 1.0) -> bool:
    """Return True iff GET {service_url}/health responds 200 (server serving).

    The Cognee server runs migrations in its FastAPI lifespan *before* it
    serves, so a 200 here reliably means migrations are done and the DBs are
    reachable.
    """
    base = _normalize_service_url(service_url or _local_api_url())
    if not base:
        return False
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as resp:
            return resp.status == 200
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def mark_server_ready(service_url: str, version: str = "") -> None:
    """Record that the local Cognee server is healthy and serving.

    Global (not namespaced) because Claude and Codex share one server on the
    same port. Read by hot-path hooks via ``server_ready_hint`` to decide
    whether to attempt recall without paying a network probe.
    """
    try:
        _SERVER_READY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "service_url": _normalize_service_url(service_url),
            "ready_at": datetime.now(timezone.utc).timestamp(),
            "version": str(version or ""),
        }
        tmp = _SERVER_READY_MARKER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _SERVER_READY_MARKER)
    except Exception as exc:
        hook_log("server_ready_mark_failed", {"error": str(exc)[:200]})


def clear_server_ready() -> None:
    """Drop the readiness marker (e.g. after a failed health re-probe)."""
    try:
        _SERVER_READY_MARKER.unlink()
    except FileNotFoundError:
        return
    except Exception as exc:
        hook_log("server_ready_clear_failed", {"error": str(exc)[:200]})


def server_ready_hint(service_url: str = "") -> bool:
    """Zero-network readiness check for the hot path.

    True iff a readiness marker exists, is within TTL, and (if given) matches
    the service URL. A stale/missing marker returns False so recall fast-skips
    while the server is still warming.
    """
    try:
        raw = json.loads(_SERVER_READY_MARKER.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return False
    except Exception:
        return False
    ready_at = float(raw.get("ready_at", 0) or 0)
    if datetime.now(timezone.utc).timestamp() - ready_at > _SERVER_READY_TTL_SECONDS:
        return False
    if service_url:
        marked = _normalize_service_url(raw.get("service_url", ""))
        if marked and marked != _normalize_service_url(service_url):
            return False
    return True


def resolve_runtime_mode() -> dict:
    """Resolve hook runtime mode from effective endpoint auth."""
    service_url, api_key = resolved_http_endpoint_auth()
    mode = "http" if (service_url and api_key) else "local_sdk"
    return {
        "mode": mode,
        "service_url": service_url,
        "api_key_present": bool(api_key),
    }


def set_agent_registration(registered: bool, session_key: str = "") -> None:
    # No local resolved cache to patch.
    _ = (registered, session_key)


def _json_http_request(
    path: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = 30.0,
):
    base_url = _local_api_url().rstrip("/")
    api_key = _api_key()
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key

    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")

    req = urllib.request.Request(
        f"{base_url}{path}",
        data=data,
        headers=headers,
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
        if not body:
            return None
        return json.loads(body)


def remember_entry_via_http(
    dataset: str,
    session_id: str,
    entry: dict,
    *,
    timeout: float = 30.0,
) -> dict | None:
    """Store a typed QA/trace entry through the backend API.

    API-mode hooks use this instead of importing Cognee's Python client,
    so they don't initialize local databases while talking to a backend.
    """
    if not dataset or not session_id:
        return None
    return _json_http_request(
        "/api/v1/remember/entry",
        {
            "entry": entry,
            "dataset_name": dataset,
            "session_id": session_id,
        },
        timeout=timeout,
    )


def register_agent_via_http(
    *,
    agent_session_name: str,
    session_id: str = "",
    dataset_names: list[str] | None = None,
    timeout: float = 15.0,
) -> tuple[bool, dict]:
    payload = {
        "agent_session_name": agent_session_name,
        "type": "api",
        "memory_mode": "hybrid",
        "source": "api",
    }
    if session_id:
        payload["session_id"] = session_id
    if dataset_names:
        payload["dataset_names"] = [str(name) for name in dataset_names if str(name).strip()]

    try:
        result = _json_http_request(
            "/api/v1/agents/register", payload, method="POST", timeout=timeout
        )
        if isinstance(result, dict):
            return True, result
        return True, {}
    except Exception as exc:
        hook_log("agent_register_failed", {"error": str(exc)[:200]})
        return False, {}


def unregister_agent_via_http(
    *, agent_session_name: str, timeout: float = 15.0
) -> tuple[bool, int]:
    try:
        result = _json_http_request(
            "/api/v1/agents/unregister",
            {"agent_session_name": agent_session_name},
            method="POST",
            timeout=timeout,
        )
        if isinstance(result, dict):
            count = int(result.get("activeAgents", 0) or result.get("active_agents", 0) or 0)
            return True, count
        return True, 0
    except Exception as exc:
        hook_log("agent_unregister_failed", {"error": str(exc)[:200]})
        return False, 0


def recall_via_http(
    query: str,
    *,
    session_id: str,
    top_k: int,
    scope: list[str],
    only_context: bool = True,
    search_type: str | None = None,
    timeout: float = 10.0,
) -> list:
    payload = {
        "query": query,
        "session_id": session_id,
        "top_k": top_k,
        "scope": scope,
        "only_context": only_context,
    }
    if search_type:
        payload["search_type"] = search_type
    result = _json_http_request("/api/v1/recall", payload, timeout=timeout)
    return result if isinstance(result, list) else []


def _backend_reachable(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url.rstrip('/')}/docs", timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def _multipart_body(
    fields: dict[str, str], files: list[tuple[str, str, bytes]]
) -> tuple[bytes, str]:
    boundary = f"----cogneePlugin{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for field_name, filename, content in files:
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{field_name}"; filename="{filename}"\r\n'
                "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(content)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(chunks), boundary


def _format_cached_bridge_document(dataset: str, session_id: str) -> tuple[str, str]:
    cache = _load_json_file(_HTTP_BRIDGE_CACHE)
    key = _bridge_cache_key(dataset, session_id)
    session_cache = cache.get(key, {})

    qa_lines: list[str] = []
    for entry in session_cache.get("qa", []) or []:
        question = str(entry.get("question") or "").strip()
        answer = str(entry.get("answer") or "").strip()
        if question:
            qa_lines.append(f"Question: {question}")
        if answer:
            qa_lines.append(f"Answer: {answer}")
        if question or answer:
            qa_lines.append("")

    trace_lines = [str(value).strip() for value in session_cache.get("trace", []) or []]
    trace_lines = [value for value in trace_lines if value]

    qa_doc = "\n".join(qa_lines).strip()
    trace_doc = "\n\n".join(trace_lines).strip()
    if qa_doc:
        qa_doc = f"Session ID: {session_id}\n\n{qa_doc}"
    if trace_doc:
        trace_doc = f"Session ID: {session_id}\n\n{trace_doc}"
    return qa_doc, trace_doc


def _post_remember_document(
    base_url: str,
    api_key: str,
    dataset: str,
    document: str,
    node_set: str,
    timeout: float,
) -> bool:
    body, boundary = _multipart_body(
        {
            "datasetName": dataset,
            "node_set": node_set,
            "run_in_background": "false",
        },
        [("data", f"{node_set}.txt", document.encode("utf-8"))],
    )
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/v1/remember",
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "X-Api-Key": api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return 200 <= resp.status < 300


def persist_session_cache_to_graph_via_http(
    dataset: str,
    session_id: str,
    timeout: float = 600.0,
) -> bool:
    """API-mode equivalent of the local SDK session-cache bridge.

    Local mode reads Cognee's in-process session cache and calls
    ``cognee.remember(..., self_improvement=False)``. API mode cannot
    read the server cache directly, so the hooks maintain a small local
    shadow and this function posts that text to the backend remember
    endpoint as permanent graph data.
    """
    base_url = _local_api_url()
    if not _backend_reachable(base_url):
        return False
    api_key = _api_key()
    if not api_key:
        hook_log("http_bridge_skipped_no_api_key", {"dataset": dataset, "session": session_id})
        return False

    qa_doc, trace_doc = _format_cached_bridge_document(dataset, session_id)
    if not qa_doc and not trace_doc:
        hook_log("http_bridge_skipped_empty_cache", {"dataset": dataset, "session": session_id})
        return False

    state = _load_json_file(_HTTP_BRIDGE_STATE)
    wrote = False
    try:
        for kind, node_set, document in (
            ("qa", "user_sessions_from_cache", qa_doc),
            ("trace", "agent_trace_feedbacks", trace_doc),
        ):
            if not document:
                continue
            state_key = f"{_bridge_cache_key(dataset, session_id)}:{kind}"
            digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
            if state.get(state_key) == digest:
                continue
            if _post_remember_document(base_url, api_key, dataset, document, node_set, timeout):
                state[state_key] = digest
                wrote = True
        _write_json_file(_HTTP_BRIDGE_STATE, state)
        hook_log(
            "http_bridge_done",
            {"dataset": dataset, "session": session_id, "wrote": wrote},
        )
        return wrote
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        hook_log(
            "http_bridge_failed",
            {"error": str(exc)[:200], "dataset": dataset, "session": session_id},
        )
        return False
