"""Shared helpers across plugin hook scripts.

Kept deliberately small: user resolution, runtime-state read, a
single log-to-disk helper. Hook scripts shouldn't grow heavy because
they run on every user prompt / tool call.
"""

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
_SHARED_PLUGIN_ROOT = Path.home() / ".cognee-plugin"
_HOOK_LOG = _PLUGIN_DIR / "hook.log"
_COUNTER_FILE = _PLUGIN_DIR / "counter.json"
_ACTIVITY_FILE = _PLUGIN_DIR / "activity.ts"
_ACTIVITY_LOG = _PLUGIN_DIR / "activity.log"
_SAVE_COUNTER = _PLUGIN_DIR / "save_counter.json"
_SERVER_READY_MARKER = _SHARED_PLUGIN_ROOT / "server-ready.json"
_SERVER_READY_TTL_SECONDS = 30
_SYNC_LOCK = _PLUGIN_DIR / "sync.lock"
# Per-agent-session buffer dirs. Each agent session (one Claude/Codex terminal)
# owns its own file under these dirs, so two concurrent agents never
# read-modify-write the same file — no locks needed, no lost-update races.
_BRIDGE_DIR = _PLUGIN_DIR / "bridge"
_PENDING_DIR = _PLUGIN_DIR / "pending"
_SUBPROCESS_LOG = _PLUGIN_DIR / "subprocess.log"
# Single-principal model: one API key (user-provided COGNEE_API_KEY or one minted
# from the default user) is cached here. Replaces the old per-agent agent_keys.json.
_API_KEY_CACHE = _SHARED_PLUGIN_ROOT / "api_key.json"
# Host-session-id -> generated Cognee session-id map. The host (Claude/Codex)
# session id is used ONLY as a local correlation key so every hook process of a
# single launch resolves the SAME Cognee session id; it is never sent to Cognee
# as an identity. A genuinely new launch gets a new host id -> new Cognee session;
# a `resume` reuses the host id -> continues the same Cognee session.
_SESSIONS_MAP_DIR = _PLUGIN_DIR / "sessions"

# Save-kinds tracked per turn. Keep this tuple in sync with bump_save_counter callers.
SAVE_KINDS = ("prompt", "trace", "answer")

# Cap the per-line log size so a noisy tool output doesn't bloat the file.
_LOG_LINE_CAP = 600

# Default auto-improve threshold (tool calls + stops). Env override.
AUTO_IMPROVE_EVERY_DEFAULT = 150
SYNC_LOCK_STALE_SECONDS = 15 * 60
_DEFAULT_LOCAL_SERVICE_URL = "http://localhost:8011"

# --- Self-managed cognee runtime (SHARED with the Claude Code plugin) --------
# Deliberately NOT namespaced under ~/.cognee-plugin/codex: the venv, the local
# cognee server, and the data store are shared with the Claude Code plugin so
# cognee is installed once and a single server serves both. Only per-plugin
# state (logs, buffers) stays under _PLUGIN_DIR; the runtime lives at the root.
_VENV_DIR = _SHARED_PLUGIN_ROOT / "venv"
_VENV_PYTHON = _VENV_DIR / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
_VENV_READY_MARKER = _SHARED_PLUGIN_ROOT / "venv-ready.json"

# cognee's own default puts its databases INSIDE the install dir (the venv), so
# they would be wiped on every venv rebuild/upgrade. Pin them to ~/.cognee.
_COGNEE_HOME = Path.home() / ".cognee"
_COGNEE_SYSTEM_DIR = _COGNEE_HOME / "system"
_COGNEE_DATA_DIR = _COGNEE_HOME / "data"
_COGNEE_CACHE_DIR = _COGNEE_HOME / "cache"


def venv_python() -> Path:
    """Path to the shared plugin-owned venv interpreter (may not exist yet)."""
    return _VENV_PYTHON


def apply_cognee_env() -> None:
    """Pin cognee's data dirs + caching into the environment.

    Uses setdefault so an explicit user/env override always wins. Called on
    import so any process that spawns the cognee server (via os.environ.copy())
    inherits a stable, upgrade-safe data location. CACHING and AUTO_FEEDBACK are
    already cognee's defaults but are set explicitly so a future default change
    can't silently disable session-context distillation.
    """
    os.environ.setdefault("SYSTEM_ROOT_DIRECTORY", str(_COGNEE_SYSTEM_DIR))
    os.environ.setdefault("DATA_ROOT_DIRECTORY", str(_COGNEE_DATA_DIR))
    os.environ.setdefault("CACHE_ROOT_DIRECTORY", str(_COGNEE_CACHE_DIR))
    os.environ.setdefault("CACHING", "true")
    os.environ.setdefault("AUTO_FEEDBACK", "true")


apply_cognee_env()


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


def _generate_session_id(cwd: str = "", host_key: str = "") -> str:
    """Mint the Cognee session id for a launch: ``{agent}_{host_session_id}``.

    The host (Codex) session id maps 1:1 to the conversation, so embedding it
    makes the Cognee session id deterministic per conversation and self-describing
    in the Cognee dashboard. Falls back to ``{agent}_{dirname}_{token}`` only when
    no host session id is available.
    """
    agent = _sanitize_session_key(os.environ.get("COGNEE_SESSION_PREFIX", "") or "codex") or "codex"
    host = _sanitize_session_key(host_key)
    if host:
        return f"{agent}_{host}"
    cwd = cwd or os.environ.get("CODEX_CWD") or os.getcwd()
    dir_name = _sanitize_session_key(Path(cwd).name) or "session"
    return f"{agent}_{dir_name}_{uuid.uuid4().hex[:12]}"


def _new_conn_uuid() -> str:
    """A per-launch connection handle (liveness/counting), independent of session."""
    return f"conn_{uuid.uuid4().hex}"


def _session_map_path(host_key: str) -> Path:
    return _SESSIONS_MAP_DIR / f"{_sanitize_session_key(host_key)}.json"


def _read_map_record(host_key: str) -> dict:
    """Return the launch record for a host session id, or {}.

    Record shape: ``{conn_uuid, session_id, host_key, created_at, touched: [...]}``.
    ``session_id`` = current Cognee session (switchable); ``conn_uuid`` = the
    per-launch liveness handle used for registration/counting (never switched).
    """
    if not host_key:
        return {}
    try:
        path = _session_map_path(host_key)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        hook_log("session_map_read_failed", {"error": str(exc)[:200]})
    return {}


def _write_map_record(host_key: str, record: dict) -> None:
    if not host_key or not isinstance(record, dict):
        return
    _write_json_file(_session_map_path(host_key), record)


def _create_map_record_if_absent(host_key: str, record: dict) -> dict:
    """Atomically create the launch record, first-writer-wins.

    Uses O_CREAT|O_EXCL so exactly one concurrent creator wins; losers read back
    the winner's record instead of clobbering it. This is what makes concurrent
    launches/hooks for the same host_key converge on a single session id rather
    than diverge. Returns the record now on disk.
    """
    if not host_key:
        return record
    path = _session_map_path(host_key)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, sort_keys=True)
        return record
    except FileExistsError:
        return _read_map_record(host_key) or record
    except Exception as exc:
        hook_log("map_create_failed", {"error": str(exc)[:200]})
        # Best-effort fallback: plain write, then read back whatever landed.
        _write_map_record(host_key, record)
        return _read_map_record(host_key) or record


def resolve_cognee_session_id(host_key: str = "", cwd: str = "") -> str:
    """Resolve the Cognee session id that scopes all saves/recalls this launch.

    Precedence:
      1. ``COGNEE_SESSION_ID`` env — explicit launch-time override.
      2. host-keyed map record — the current session for this launch (stable
         across the launch's separate hook processes; updated by the picker).
      3. freshly generated id (new launch), persisted to the map.
    """
    explicit = _sanitize_session_key(str(os.environ.get("COGNEE_SESSION_ID", "") or "").strip())
    if explicit:
        return explicit

    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    if rec.get("session_id"):
        return _sanitize_session_key(str(rec["session_id"]))

    new_id = _generate_session_id(cwd, host_key)
    if not host_key:
        return new_id
    winner = _create_map_record_if_absent(
        host_key,
        {
            "session_id": new_id,
            "host_key": host_key,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "touched": [new_id],
        },
    )
    return str(winner.get("session_id") or new_id)


def ensure_launch_record(host_key: str = "", cwd: str = "") -> tuple[str, str]:
    """Create (first-writer-wins) and return this launch's (session_id, conn_uuid).

    Called by SessionStart. The session id honors an explicit ``COGNEE_SESSION_ID``
    override, else the existing/generated id; the conn_uuid is minted once.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    if rec.get("session_id") and rec.get("conn_uuid"):
        return str(rec["session_id"]), str(rec["conn_uuid"])

    explicit = _sanitize_session_key(str(os.environ.get("COGNEE_SESSION_ID", "") or "").strip())
    session_id = explicit or str(rec.get("session_id") or "") or _generate_session_id(cwd, host_key)
    conn_uuid = str(rec.get("conn_uuid") or "") or _new_conn_uuid()
    record = {
        "session_id": session_id,
        "conn_uuid": conn_uuid,
        "host_key": host_key,
        "created_at": rec.get("created_at")
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "touched": rec.get("touched") or [session_id],
    }
    if not host_key:
        return session_id, conn_uuid
    winner = _create_map_record_if_absent(host_key, record)
    # If a prior resolve() created a session-only record (no handle), graft our
    # conn_uuid onto it. SessionStart is the sole writer of conn_uuid, so this
    # merge isn't contended in practice.
    if not winner.get("conn_uuid"):
        merged = dict(winner)
        merged["conn_uuid"] = conn_uuid
        merged.setdefault("host_key", host_key)
        _write_map_record(host_key, merged)
        winner = _read_map_record(host_key) or merged
    return str(winner.get("session_id") or session_id), str(winner.get("conn_uuid") or conn_uuid)


def resolve_conn_uuid(host_key: str = "") -> str:
    """Return this launch's connection handle, minting+persisting one if absent."""
    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    cu = str(rec.get("conn_uuid") or "")
    if cu:
        return cu
    cu = _new_conn_uuid()
    if host_key:
        rec = _read_map_record(host_key)
        if not rec.get("conn_uuid"):
            rec["conn_uuid"] = cu
            rec.setdefault("host_key", host_key)
            _write_map_record(host_key, rec)
        return str(_read_map_record(host_key).get("conn_uuid") or cu)
    return cu


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

    # session_id = data scoping key (switchable); conn_uuid = registration handle.
    cognee_session_id = resolve_cognee_session_id(active_session_key)
    if cognee_session_id:
        resolved["session_id"] = cognee_session_id
    conn_uuid = resolve_conn_uuid(active_session_key)
    if conn_uuid:
        resolved["agent_session_name"] = conn_uuid

    service_url = _local_api_url().strip()
    if service_url:
        resolved["base_url"] = service_url

    api_key = _api_key().strip()
    if api_key:
        resolved["api_key"] = api_key

    # Resolve active connection details FIRST — it doubles as the primary
    # identity source. The connection is registered under the per-launch
    # conn_uuid handle, so query by that — not the session id (which can change
    # on a switch) and not the host correlation key. Its agent.user_id is
    # served by both OSS servers and cloud tenants, whereas /users/me is absent
    # on some tenants (404 on every hook), so the users/me probe below runs
    # only when identity is still unresolved.
    try:
        query = ""
        if conn_uuid:
            query = f"?agent_session_name={urllib.parse.quote(conn_uuid, safe='')}"
        conn = _json_http_request(
            f"/api/v1/agents/connections/me{query}",
            method="GET",
            timeout=10.0,
        )
        if isinstance(conn, dict):
            agent = conn.get("agent") if isinstance(conn.get("agent"), dict) else {}
            if isinstance(agent, dict):
                # Do not overwrite resolved["session_id"] from the connection: the
                # local map is authoritative for the *current* session (post-switch).
                agent_session_name = str(agent.get("agent_session_name") or "").strip()
                if agent_session_name:
                    resolved["agent_session_name"] = agent_session_name
                agent_user_id = str(agent.get("user_id") or "").strip()
                if agent_user_id:
                    resolved["user_id"] = agent_user_id
                status = str(agent.get("status") or "").strip().lower()
                resolved["registered"] = status == "active"
    except Exception as exc:
        hook_log("runtime_state_connection_lookup_failed", {"error": str(exc)[:200]})

    # Fallback identity probe — only when the connection lookup yielded none
    # (e.g. before registration on a fresh launch).
    if not resolved.get("user_id"):
        try:
            me = _json_http_request("/api/v1/users/me", method="GET", timeout=10.0)
            if isinstance(me, dict):
                user_id = str(me.get("id") or "").strip()
                if user_id:
                    resolved["user_id"] = user_id
        except Exception as exc:
            hook_log("runtime_state_users_me_failed", {"error": str(exc)[:200]})

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
        # Atomic write: a concurrent reader never sees a half-written file.
        # Per-pid tmp name so two writers can't collide on the tmp path.
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
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


def _agent_session_scope(fallback: str = "") -> str:
    """Filesystem-safe identity of the current agent session.

    Each agent session (one Claude/Codex terminal) owns its own pending and
    bridge files keyed by this scope, so concurrent agents never share a file
    (no locks, no lost-update races). Falls back to the cognee session_id, then
    a constant, so the path is always defined.
    """
    scope = _sanitize_session_key(get_session_key()) or _sanitize_session_key(fallback)
    return scope or "default"


def _pending_file(session_id: str = "") -> Path:
    return _PENDING_DIR / f"{_agent_session_scope(session_id)}.json"


def _bridge_file(session_id: str = "") -> Path:
    return _BRIDGE_DIR / f"{_agent_session_scope(session_id)}.json"


# Short mutex for read-modify-write of the per-session buffer file. Appends
# from concurrent async hooks (and the drain's trim write-back) would otherwise
# clobber each other: os.replace keeps the file valid but last-writer-wins,
# silently dropping the other writer's entry. Critical sections are
# milliseconds, so waiting is cheap.
_BUFFER_LOCK = _PLUGIN_DIR / "buffer.lock"
_BUFFER_LOCK_STALE_SECONDS = 15.0
_BUFFER_LOCK_TIMEOUT_SECONDS = 1.0
_BUFFER_LOCK_POLL_SECONDS = 0.02


@contextmanager
def _buffer_lock():
    """Acquire the buffer-file mutex, waiting briefly; fail open on timeout.

    Yields True when the lock was acquired. On timeout/error the caller
    proceeds WITHOUT the lock — a rare lost update beats a hook that hangs.
    """
    deadline = time.monotonic() + _BUFFER_LOCK_TIMEOUT_SECONDS
    acquired = False
    while True:
        try:
            _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
            if _BUFFER_LOCK.exists():
                try:
                    if time.time() - _BUFFER_LOCK.stat().st_mtime > _BUFFER_LOCK_STALE_SECONDS:
                        _BUFFER_LOCK.unlink()
                except FileNotFoundError:
                    pass
            fd = os.open(str(_BUFFER_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            acquired = True
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                hook_log("buffer_lock_timeout", {})
                break
            time.sleep(_BUFFER_LOCK_POLL_SECONDS)
        except Exception as exc:
            hook_log("buffer_lock_error", {"error": str(exc)[:200]})
            break
    try:
        yield acquired
    finally:
        if acquired:
            try:
                _BUFFER_LOCK.unlink()
            except FileNotFoundError:
                pass
            except Exception as exc:
                hook_log("buffer_lock_release_failed", {"error": str(exc)[:200]})


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

    with _buffer_lock():
        cache = _load_json_file(_bridge_file(session_id))
        key = _bridge_cache_key(dataset, session_id)
        session_cache = cache.setdefault(key, {"qa": [], "trace": []})
        if question or answer:
            session_cache.setdefault("qa", []).append({"question": question, "answer": answer})
        if trace:
            session_cache.setdefault("trace", []).append(trace)
        _write_json_file(_bridge_file(session_id), cache)


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


def _reexec_into_venv() -> None:
    """Re-exec the current hook under the shared plugin-owned venv interpreter.

    Hooks are launched by the host as ``python3 <script>`` using whatever
    python3 is on PATH — which has neither cognee nor aiohttp. The runtime
    lives in ``~/.cognee-plugin/venv``. Once that venv exists, re-exec into it
    so every import resolves there. No-op before the venv exists (cold start,
    pre-install) or when already running inside it.
    """
    if os.environ.get("COGNEE_PLUGIN_IN_VENV") == "1":
        return  # loop guard: this process already re-execed (or opted out)
    if not sys.argv or not os.path.isfile(sys.argv[0]):
        return  # not a `python script.py` launch (e.g. -c/-m/stdin) — don't rebuild argv
    vpy = _VENV_PYTHON
    if not vpy.exists():
        return  # cold start — install hasn't built the venv yet
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        if os.path.samefile(str(vpy), sys.executable):
            return  # the host python3 already *is* the venv interpreter
    except OSError:
        pass
    try:
        # execv inherits os.environ (incl. the loop guard just set above).
        os.execv(str(vpy), [str(vpy), *sys.argv])
    except OSError as exc:
        # Better to run degraded under the host interpreter than to die.
        hook_log("venv_reexec_failed", {"error": str(exc)[:200]})


# Fired on import: every cognee-touching hook imports this module before any
# aiohttp/cognee import, so this is the single chokepoint that pins all hooks
# to the venv runtime once it exists.
_reexec_into_venv()


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
    # Scope by the host-provided session key (COGNEE_SESSION_KEY, unique per
    # Claude/Codex session) rather than the cwd-derived cognee session_id, so
    # two concurrent agents in the same project don't collide on one pending
    # slot and scramble each other's prompts. Falls back to session_id.
    scope = get_session_key() or session_id
    session_key = f"{scope}:"
    turn_key = f"{scope}:{turn_id}" if turn_id else session_key
    return turn_key, session_key


def remember_pending_prompt(
    session_id: str, prompt: str, *, turn_id: str = "", context: str = ""
) -> None:
    """Store the current prompt until Codex Stop provides the assistant answer."""
    if not session_id or not prompt.strip():
        return
    data = _load_json_file(_pending_file(session_id))
    turn_key, session_key = _pending_keys(session_id, turn_id)
    entry = {
        "prompt": prompt[:8000],
        "context": context[:2000],
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    data[turn_key] = entry
    data[session_key] = entry
    _write_json_file(_pending_file(session_id), data)


def pop_pending_prompt(session_id: str, *, turn_id: str = "") -> dict:
    """Return and remove the prompt saved for this Codex turn."""
    if not session_id:
        return {"prompt": "", "context": ""}
    data = _load_json_file(_pending_file(session_id))
    turn_key, session_key = _pending_keys(session_id, turn_id)
    entry = data.pop(turn_key, None) or data.get(session_key) or {}
    data.pop(session_key, None)
    _write_json_file(_pending_file(session_id), data)
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
        os.environ.get("COGNEE_LOCAL_API_URL") or os.environ.get("COGNEE_BASE_URL") or ""
    ).strip()
    if direct:
        return direct
    return _DEFAULT_LOCAL_SERVICE_URL


def _normalize_service_url(value: str) -> str:
    return str(value or "").strip().rstrip("/")


def load_cached_api_key(service_url: str = "") -> str:
    """Return the single cached principal key (matching service_url if recorded)."""
    data = _load_json_file(_API_KEY_CACHE)
    if not isinstance(data, dict):
        return ""
    key = str(data.get("api_key") or "").strip()
    if not key:
        return ""
    cached_url = _normalize_service_url(str(data.get("base_url") or ""))
    wanted = _normalize_service_url(service_url)
    if wanted and cached_url and cached_url != wanted:
        return ""
    return key


def save_cached_api_key(service_url: str, key: str) -> None:
    """Persist the single principal key (env key takes precedence at read time)."""
    if not str(key or "").strip():
        return
    _write_json_file(
        _API_KEY_CACHE,
        {
            "base_url": _normalize_service_url(service_url),
            "api_key": str(key).strip(),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def _api_key() -> str:
    """Resolve the single principal API key.

    Single-principal model: one key for everything. Order:
      1. ``COGNEE_API_KEY`` env (user-provided, or set in-process after minting).
      2. The single cached key (``api_key.json``), minted once from the default
         user by SessionStart when no key was provided.
    No per-agent keys, no agent-name keying.
    """
    env_key = str(os.environ.get("COGNEE_API_KEY", "") or "").strip()
    if env_key:
        return env_key

    service_url = _normalize_service_url(_local_api_url())
    cached = load_cached_api_key(service_url)
    if cached:
        os.environ["COGNEE_API_KEY"] = cached
        return cached

    return ""


def resolved_http_endpoint_auth() -> tuple[str, str]:
    """Return (service_url, api_key) for runtime HTTP calls.

    Service URL always falls back to localhost. API key is the single principal
    key: env first, then the single cached key.
    """
    service_url = _normalize_service_url(_local_api_url())
    api_key = _api_key().strip()
    if service_url:
        os.environ["COGNEE_BASE_URL"] = service_url
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
            "base_url": _normalize_service_url(service_url),
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
        marked = _normalize_service_url(raw.get("base_url", ""))
        if marked and marked != _normalize_service_url(service_url):
            return False
    return True


def resolve_runtime_mode() -> dict:
    """Resolve hook runtime mode from effective endpoint auth."""
    service_url, api_key = resolved_http_endpoint_auth()
    # A configured service URL alone selects HTTP mode; an API key is no longer
    # required to decide whether to talk to a server (it's still sent when present).
    mode = "http" if service_url else "local_sdk"
    return {
        "mode": mode,
        "base_url": service_url,
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
    context_profile: str | None = None,
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
    if context_profile:
        payload["context_profile"] = context_profile
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


def _format_bridge_docs(session_cache: dict, session_id: str) -> tuple[str, str]:
    """Build the (qa_doc, trace_doc) pair for one bridge-cache key.

    Shared by the drain (which hashes + posts the docs) and
    ``pending_capture_counts`` (which hashes them to detect not-yet-cognified
    content), so both always compute identical digests for identical content.
    """
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


def _format_cached_bridge_document(dataset: str, session_id: str) -> tuple[str, str]:
    cache = _load_json_file(_bridge_file(session_id))
    key = _bridge_cache_key(dataset, session_id)
    return _format_bridge_docs(cache.get(key, {}), session_id)


def pending_capture_counts(session_id: str) -> dict:
    """Count bridge entries captured this session but not yet confirmed cognified.

    The drain (``persist_session_cache_to_graph_via_http``) marks each qa/trace
    document as cognified by storing its sha256 digest under
    ``_state["{dataset}:{session_id}:{kind}"]``. A kind whose current digest is
    absent or stale is captured-but-not-yet-queryable, which lets callers tell
    "nothing stored" apart from "stored, cognify still pending" on empty recall.

    Scans every file in ``_BRIDGE_DIR`` rather than only ``_bridge_file(session_id)``:
    bridge filenames are scoped by COGNEE_SESSION_KEY, which hook processes set but
    the ``cognee-search.sh`` shell environment may not, so matching by the
    ``:{session_id}`` key suffix works from both paths (session ids are sanitized
    to ``[alnum-_.]``, so the suffix match cannot split a session id).

    Best-effort, never raises: any read/parse failure counts as zero pending.
    """
    pending = {"qa": 0, "trace": 0}
    if not session_id:
        return pending
    try:
        bridge_paths = sorted(_BRIDGE_DIR.glob("*.json")) if _BRIDGE_DIR.exists() else []
    except OSError as exc:
        hook_log("pending_capture_scan_failed", {"error": str(exc)[:200]})
        return pending
    suffix = f":{session_id}"
    for path in bridge_paths:
        # Per-file guard: a bridge file with a corrupt SHAPE (valid JSON, wrong
        # entry types) must skip that file, not abort the caller's hook output.
        try:
            cache = _load_json_file(path)
            if not isinstance(cache, dict):
                continue
            state = cache.get("_state") if isinstance(cache.get("_state"), dict) else {}
            for key, session_cache in cache.items():
                if (
                    key == "_state"
                    or not key.endswith(suffix)
                    or not isinstance(session_cache, dict)
                ):
                    continue
                qa_doc, trace_doc = _format_bridge_docs(session_cache, session_id)
                for kind, document in (("qa", qa_doc), ("trace", trace_doc)):
                    if not document:
                        continue
                    digest = hashlib.sha256(document.encode("utf-8")).hexdigest()
                    if state.get(f"{key}:{kind}") != digest:
                        pending[kind] += len(session_cache.get(kind) or [])
        except Exception as exc:
            hook_log("pending_capture_read_failed", {"path": str(path), "error": str(exc)[:200]})
    return pending


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

    bridge_path = _bridge_file(session_id)
    bridge_cache = _load_json_file(bridge_path)
    state = bridge_cache.get("_state", {}) if isinstance(bridge_cache, dict) else {}
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
        if isinstance(bridge_cache, dict):
            bridge_cache["_state"] = state
            _write_json_file(bridge_path, bridge_cache)
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


# --- Session improve (server-side session->graph bridge) ----------------------
# The hooks write every turn into the SERVER session cache via /remember/entry,
# so the server can bridge a session itself: POST /api/v1/improve runs feedback
# weights, QA persist, trace-feedback persist, distillation, and enrichment over
# that cache. This replaces the legacy full-document bridge above, which re-sent
# the whole accumulated session text (raw tool outputs included) for a full
# re-cognify on every sync. The legacy path is kept only as a fallback for
# servers without session-aware improve.
_IMPROVE_UNSUPPORTED_MARKER = _SHARED_PLUGIN_ROOT / "improve-unsupported.json"
_IMPROVE_UNSUPPORTED_TTL_SECONDS = 24 * 3600


def _improve_float_env(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _improve_submit_timeout() -> float:
    return _improve_float_env("COGNEE_IMPROVE_SUBMIT_TIMEOUT", 180.0)


def mark_improve_unsupported(base_url: str) -> None:
    """Record that this server lacks the session-aware improve endpoint."""
    _write_json_file(
        _IMPROVE_UNSUPPORTED_MARKER,
        {
            "base_url": _normalize_service_url(base_url),
            "marked_at": datetime.now(timezone.utc).timestamp(),
        },
    )


def improve_unsupported(base_url: str) -> bool:
    """True if this server recently rejected the improve endpoint (TTL-bounded)."""
    data = _load_json_file(_IMPROVE_UNSUPPORTED_MARKER)
    if not data:
        return False
    marked_url = _normalize_service_url(str(data.get("base_url") or ""))
    if marked_url and marked_url != _normalize_service_url(base_url):
        return False
    marked_at = float(data.get("marked_at", 0) or 0)
    return datetime.now(timezone.utc).timestamp() - marked_at < _IMPROVE_UNSUPPORTED_TTL_SECONDS


def append_warmup_entry(dataset: str, session_id: str, entry: dict) -> None:
    """Buffer a typed QA/trace entry while the server is still warming.

    Per-turn stores go to the server session cache via /remember/entry; before
    the server serves, those writes would be lost — and improve() bridges only
    what the server cache holds. Buffered entries are replayed in order by
    ``drain_warmup_entries`` once the server is ready.
    """
    if not dataset or not session_id or not isinstance(entry, dict):
        return
    with _buffer_lock():
        cache = _load_json_file(_bridge_file(session_id))
        key = _bridge_cache_key(dataset, session_id)
        session_cache = cache.setdefault(key, {"qa": [], "trace": []})
        session_cache.setdefault("pending_entries", []).append(entry)
        _write_json_file(_bridge_file(session_id), cache)


_DRAIN_LOCK = _PLUGIN_DIR / "drain.lock"
_DRAIN_LOCK_STALE_SECONDS = 60.0
# Pause before the one in-place drain retry in run_session_improve: long enough
# for a momentary server blip to pass, short enough not to hold up a sync.
_DRAIN_RETRY_PAUSE_SECONDS = 2.0


def _try_acquire_drain_lock() -> bool:
    """Single-drainer guard: concurrent drains would double-replay entries into
    the server cache and clobber each other's buffer write-backs."""
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        if _DRAIN_LOCK.exists():
            try:
                if time.time() - _DRAIN_LOCK.stat().st_mtime > _DRAIN_LOCK_STALE_SECONDS:
                    _DRAIN_LOCK.unlink()
            except FileNotFoundError:
                pass
        fd = os.open(str(_DRAIN_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception as exc:
        # Fail open: a rare double replay (deduped server-side per entry write)
        # is better than a buffer that can never drain.
        hook_log("drain_lock_error", {"error": str(exc)[:200]})
        return True


def _release_drain_lock() -> None:
    try:
        _DRAIN_LOCK.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        hook_log("drain_lock_release_failed", {"error": str(exc)[:200]})


def drain_warmup_entries(dataset: str, session_id: str) -> tuple:
    """Replay warmup-buffered entries into the server session cache, in order.

    Returns ``(drained, remaining)``. Stops at the first replay failure so the
    unreplayed tail stays buffered (order preserved). Guarded by a single-drainer
    lock, and the buffer trim is computed against a FRESH re-read of the file so
    entries appended while the replay was in flight are never lost — the replay
    is N sequential HTTP calls, a wide window for concurrent async hooks.
    """
    if not dataset or not session_id:
        return 0, 0
    path = _bridge_file(session_id)
    key = _bridge_cache_key(dataset, session_id)
    snapshot = list((_load_json_file(path).get(key) or {}).get("pending_entries") or [])
    if not snapshot:
        return 0, 0
    if not _try_acquire_drain_lock():
        hook_log("warmup_drain_skipped_locked", {"session": session_id, "pending": len(snapshot)})
        return 0, len(snapshot)
    try:
        drained = 0
        for entry in snapshot:
            try:
                remember_entry_via_http(dataset, session_id, entry)
                drained += 1
            except Exception as exc:
                hook_log("warmup_drain_error", {"error": str(exc)[:200], "drained": drained})
                break
        remaining = len(snapshot) - drained
        if drained:
            # Re-read before trimming, under the buffer mutex: hooks may append
            # new pending entries (or qa/trace mirror text) during the replay,
            # and writing back a stale snapshot would silently delete them.
            with _buffer_lock():
                cache = _load_json_file(path)
                session_cache = cache.get(key) or {}
                fresh = list(session_cache.get("pending_entries") or [])
                if fresh[:drained] == snapshot[:drained]:
                    fresh = fresh[drained:]
                else:
                    # Unexpected interleaving — remove the replayed entries by value.
                    for entry in snapshot[:drained]:
                        try:
                            fresh.remove(entry)
                        except ValueError:
                            pass
                session_cache["pending_entries"] = fresh
                cache[key] = session_cache
                _write_json_file(path, cache)
            remaining = len(fresh)
            hook_log(
                "warmup_drained",
                {"session": session_id, "count": drained, "left": remaining},
            )
        return drained, remaining
    finally:
        _release_drain_lock()


def improve_session_via_http(dataset: str, session_id: str, *, timeout: float = None) -> dict:
    """Bridge one session into the graph via POST /api/v1/improve.

    The server reads its own session cache (feedback weights, QA persist,
    trace-feedback persist, distillation, enrichment), so no session text is
    sent. ``run_in_background=true`` backgrounds the cognify-heavy pipelines,
    but the agent-context and distillation stages still run inside the request,
    so the submit timeout must stay generous — this must only ever be called
    from detached workers/async hooks, never a synchronous hook window.

    A 2xx submit counts as success: improve is idempotent (unchanged session
    content dedups server-side by content hash, and a per-session improve lock
    makes a concurrent run a no-op).
    """
    if not dataset or not session_id:
        return {"ok": False, "error": "missing dataset/session"}
    submit_timeout = timeout if timeout is not None else _improve_submit_timeout()
    try:
        result = _json_http_request(
            "/api/v1/improve",
            {
                "dataset_name": dataset,
                "session_ids": [session_id],
                "run_in_background": True,
            },
            timeout=submit_timeout,
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (404, 405, 422):
            # Older server without session-aware improve: remember it (TTL'd)
            # so callers fall back to the legacy document bridge.
            mark_improve_unsupported(_local_api_url())
            return {"ok": False, "unsupported": True, "status": exc.code}
        return {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}: {exc.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"ok": False, "status": 0, "error": str(exc)[:200]}

    if isinstance(result, dict) and not result:
        # The server's per-session improve lock skipped this run ({} response):
        # another improve is in flight. That run may have extracted the session
        # cache BEFORE the latest turns landed, so a skip is NOT success — the
        # caller must retry once the lock frees.
        return {"ok": False, "busy": True}

    return {"ok": True, "result": result if isinstance(result, dict) else {}}


def ensure_dataset_via_http(dataset: str) -> None:
    """Best-effort create/authorize the dataset before an improve.

    improve() resolves *existing* authorized datasets and fails NON-FATALLY
    (returning 2xx) when there are none — unlike the legacy /remember bridge,
    whose add() implicitly created the dataset. Creating here (idempotent
    POST) means one skipped SessionStart ensure can never silently strand a
    whole session's sync. Failures are logged and never block the improve —
    if the dataset truly cannot be created, the improve outcome reports it.
    """
    if not dataset:
        return
    try:
        _json_http_request("/api/v1/datasets", {"name": dataset}, timeout=15.0)
        hook_log("dataset_ensured", {"dataset": dataset})
        return
    except urllib.error.HTTPError as exc:
        # Some deployments route the collection at /datasets/ and answer the
        # non-slash path with a 307/308, which urllib refuses to follow for a
        # POST body. Re-issue the POST at the (same-origin) redirect target.
        if exc.code in (301, 302, 307, 308):
            base_url = _local_api_url().rstrip("/")
            target = urllib.parse.urljoin(
                f"{base_url}/api/v1/datasets", str(exc.headers.get("Location") or "")
            )
            if urllib.parse.urlparse(target).netloc == urllib.parse.urlparse(base_url).netloc:
                headers = {"Content-Type": "application/json"}
                api_key = _api_key()
                if api_key:
                    headers["X-Api-Key"] = api_key
                req = urllib.request.Request(
                    target,
                    data=json.dumps({"name": dataset}).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )
                try:
                    with urllib.request.urlopen(req, timeout=15.0) as resp:
                        resp.read()
                    hook_log("dataset_ensured", {"dataset": dataset, "via": "redirect"})
                    return
                except Exception as exc2:
                    hook_log(
                        "dataset_ensure_redirect_failed",
                        {"dataset": dataset, "target": target[:120], "error": str(exc2)[:200]},
                    )
                    return
        # An already-existing dataset may come back 4xx on some servers; log
        # and proceed rather than blocking the sync on a pre-flight.
        hook_log("dataset_ensure_http_status", {"dataset": dataset, "status": exc.code})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        hook_log("dataset_ensure_failed", {"dataset": dataset, "error": str(exc)[:200]})


def run_session_improve(dataset: str, session_id: str) -> bool:
    """API-mode session->graph sync: drain warmup entries, then improve.

    Falls back to the legacy full-document bridge when the server does not
    support session-aware improve. Returns True when a sync ran successfully.
    """
    base_url = _local_api_url()
    if not _backend_reachable(base_url):
        return False
    _, remaining = drain_warmup_entries(dataset, session_id)
    if remaining:
        # One bounded retry after a short pause: the tail usually failed on a
        # momentary blip, and improve reads only what reached the server cache.
        time.sleep(_DRAIN_RETRY_PAUSE_SECONDS)
        _, remaining = drain_warmup_entries(dataset, session_id)
    if improve_unsupported(base_url):
        return persist_session_cache_to_graph_via_http(dataset, session_id)
    ensure_dataset_via_http(dataset)
    outcome = improve_session_via_http(dataset, session_id)
    if outcome.get("unsupported"):
        hook_log("improve_unsupported_fallback", {"dataset": dataset, "session": session_id})
        return persist_session_cache_to_graph_via_http(dataset, session_id)
    # Busy = another improve holds the session lock (e.g. an idle-watcher run
    # racing the SessionEnd sync). That run's snapshot may predate the latest
    # turns, so wait for the lock to free and re-submit; the retried improve
    # dedups unchanged content server-side, so this never double-processes.
    busy_deadline = time.monotonic() + _improve_float_env("COGNEE_IMPROVE_BUSY_DEADLINE", 600.0)
    busy_interval = max(0.1, _improve_float_env("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", 15.0))
    while outcome.get("busy") and time.monotonic() < busy_deadline:
        hook_log("improve_busy_retry", {"dataset": dataset, "session": session_id})
        time.sleep(busy_interval)
        outcome = improve_session_via_http(dataset, session_id)
    hook_log(
        "improve_fired",
        {
            "dataset": dataset,
            "session": session_id,
            "ok": bool(outcome.get("ok")),
            "busy": bool(outcome.get("busy")),
            "error": str(outcome.get("error") or "")[:120],
        },
    )
    if remaining:
        # Buffered entries never reached the server cache, so the improve above
        # persisted an incomplete session. Partial persist beats none (hence the
        # improve still ran), but report not-synced so the caller's retry loop
        # re-drives the whole drain+improve — dedup makes the re-run cheap.
        hook_log(
            "improve_incomplete_drain",
            {
                "dataset": dataset,
                "session": session_id,
                "remaining": remaining,
                "improve_ok": bool(outcome.get("ok")),
            },
        )
        return False
    return bool(outcome.get("ok"))
