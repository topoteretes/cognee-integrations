"""Shared helpers across plugin hook scripts.

Kept deliberately small: user resolution, runtime-state read, a
single log-to-disk helper. Hook scripts shouldn't grow heavy because
they run on every user prompt / tool call.
"""

import asyncio
import errno
import hashlib
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import _proc
from _env_file import load_env_file
from _logfiles import append_line as _append_log_line
from _logfiles import rotate_if_oversized as _rotate_log_if_oversized
from _recall_http import DOWN, SLOW, UNKNOWN, classify_transport_exception

# One-time config: ~/.cognee/.env acts like shell exports (setdefault — a real
# export still wins). Loaded before any env read below or in importers.
load_env_file()

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
# One lock file per session (see improve_session_lock): the idle watcher, the
# store hook and the SessionEnd sync all bridge sessions, and only one of them
# may have an improve in flight for a given session at a time.
_IMPROVE_LOCK_DIR = _PLUGIN_DIR / "improve-locks"
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

    Record shape::

        {conn_uuid, session_id, dataset, host_key, host_pid, cwd, created_at,
         switched_at, touched: [{session_id, dataset, conn_uuid, from, to}, ...]}

    ``session_id`` / ``dataset`` / ``conn_uuid`` describe the CURRENT Cognee
    session of this launch. A dataset switch (``switch-dataset.py``) replaces all
    three at once — a Cognee session never spans two datasets, and each session
    is registered under its own connection handle — and appends the retired
    triple to ``touched`` so the final sync/unregister still covers it.
    Legacy records store ``touched`` as a list of session-id strings; see
    ``touched_pairs``.
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
      1. host-keyed map record AFTER a dataset switch (``switched_at`` set) —
         the user explicitly moved this launch, which beats a shell export
         that would otherwise pin every hook to the pre-switch session.
      2. ``COGNEE_SESSION_ID`` env — explicit launch-time override.
      3. host-keyed map record — the current session for this launch (stable
         across the launch's separate hook processes).
      4. freshly generated id (new launch), persisted to the map.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    if rec.get("switched_at") and rec.get("session_id"):
        return _sanitize_session_key(str(rec["session_id"]))

    explicit = _sanitize_session_key(str(os.environ.get("COGNEE_SESSION_ID", "") or "").strip())
    if explicit:
        return explicit

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


def ensure_launch_record(
    host_key: str = "",
    cwd: str = "",
    *,
    dataset: str = "",
    host_pid: int = 0,
) -> tuple[str, str]:
    """Create (first-writer-wins) and return this launch's (session_id, conn_uuid).

    Called by SessionStart. The session id honors an explicit ``COGNEE_SESSION_ID``
    override, else the existing/generated id; the conn_uuid is minted once.

    ``dataset`` seeds the record's active dataset (from the env/default at launch)
    the first time it is seen; a record that already carries one — a resume, or
    a launch that was switched — keeps it, so the switch survives hook restarts
    and is not undone by the shell's ``COGNEE_PLUGIN_DATASET``. ``cwd`` and
    ``host_pid`` are stored so a process that has no hook payload (the switch
    command running under the host's shell tool) can find its own record.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    if rec.get("session_id") and rec.get("conn_uuid"):
        _backfill_launch_record(host_key, rec, dataset=dataset, cwd=cwd, host_pid=host_pid)
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
    if dataset:
        record["dataset"] = str(rec.get("dataset") or dataset)
    if cwd:
        record["cwd"] = str(rec.get("cwd") or cwd)
    if host_pid:
        record["host_pid"] = int(rec.get("host_pid") or host_pid)
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
    _backfill_launch_record(host_key, winner, dataset=dataset, cwd=cwd, host_pid=host_pid)
    return str(winner.get("session_id") or session_id), str(winner.get("conn_uuid") or conn_uuid)


def _backfill_launch_record(
    host_key: str, rec: dict, *, dataset: str = "", cwd: str = "", host_pid: int = 0
) -> None:
    """Add launch metadata a pre-existing record lacks (never overwrites)."""
    if not host_key or not isinstance(rec, dict):
        return
    updates = {}
    if dataset and not rec.get("dataset"):
        updates["dataset"] = dataset
    if cwd and not rec.get("cwd"):
        updates["cwd"] = cwd
    if host_pid and not rec.get("host_pid"):
        updates["host_pid"] = int(host_pid)
    if not updates:
        return
    merged = dict(_read_map_record(host_key) or rec)
    for key, value in updates.items():
        merged.setdefault(key, value)
    _write_map_record(host_key, merged)


# ── Dataset switching ──────────────────────────────────────────────────────
#
# A launch's active dataset lives in its launch record. Every hook reads it from
# there (via config.get_dataset -> resolve_active_dataset); the shell's
# COGNEE_PLUGIN_DATASET only seeds the record at SessionStart. The switch command
# (switch-dataset.py) rewrites session_id + dataset + conn_uuid atomically and
# retires the previous triple into ``touched``.

_DEFAULT_DATASET_NAME = "agent_sessions"


def resolve_active_dataset(host_key: str = "") -> str:
    """The dataset this launch writes to: launch record → env → default.

    Without a host key (a process outside any launch, e.g. a bare CLI call) the
    env/default rule applies unchanged.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if host_key:
        rec = _read_map_record(host_key)
        ds = str(rec.get("dataset") or "").strip()
        if ds:
            return ds
    return str(os.environ.get("COGNEE_PLUGIN_DATASET", "") or "").strip() or _DEFAULT_DATASET_NAME


def touched_pairs(host_key: str = "") -> list[dict]:
    """Every (session_id, dataset, conn_uuid) this launch has used, oldest first.

    The current triple is always last. Legacy records hold ``touched`` as plain
    session-id strings — those are paired with the record's current dataset (a
    pre-switch record only ever had one).
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    rec = _read_map_record(host_key)
    if not rec:
        return []
    current = {
        "session_id": str(rec.get("session_id") or ""),
        "dataset": str(rec.get("dataset") or "") or resolve_active_dataset(host_key),
        "conn_uuid": str(rec.get("conn_uuid") or ""),
    }
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in rec.get("touched") or []:
        if isinstance(item, dict):
            entry = {
                "session_id": str(item.get("session_id") or ""),
                "dataset": str(item.get("dataset") or current["dataset"]),
                "conn_uuid": str(item.get("conn_uuid") or ""),
            }
        else:
            entry = {
                "session_id": str(item or ""),
                "dataset": current["dataset"],
                "conn_uuid": "",
            }
        key = (entry["session_id"], entry["dataset"])
        if (
            not entry["session_id"]
            or key in seen
            or key == (current["session_id"], current["dataset"])
        ):
            continue
        seen.add(key)
        out.append(entry)
    if current["session_id"]:
        out.append(current)
    return out


def mint_switch_session_id(host_key: str = "") -> str:
    """A new, self-describing Cognee session id for a switched launch.

    ``_generate_session_id`` is deterministic per host session (``{agent}_{host}``),
    so a switch appends an ordinal: ``{agent}_{host}__2``, ``__3``, ... — never
    colliding with the pre-switch id while staying readable in the dashboard.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    base = _generate_session_id("", host_key)
    used = {p["session_id"] for p in touched_pairs(host_key)}
    n = max(2, len(used) + 1)
    candidate = f"{base}__{n}"
    while candidate in used:
        n += 1
        candidate = f"{base}__{n}"
    return candidate


def switch_launch_record(
    host_key: str,
    *,
    session_id: str,
    dataset: str,
    conn_uuid: str,
) -> dict:
    """Atomically point the launch at a new (session, dataset, connection).

    The previous triple is appended to ``touched`` (with ``to`` stamped) so the
    final sync and unregister still cover it; ``switched_at`` marks the record as
    user-moved (see ``resolve_cognee_session_id`` precedence). Returns the record
    now on disk.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if not host_key:
        raise ValueError("switch_launch_record: no host session key")
    rec = _read_map_record(host_key)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    touched = touched_pairs(host_key)
    # touched_pairs() returns the current triple last; stamp it as retired.
    if touched:
        touched[-1] = {**touched[-1], "to": now}
        touched[-1].setdefault("from", str(rec.get("switched_at") or rec.get("created_at") or ""))
    touched.append(
        {"session_id": session_id, "dataset": dataset, "conn_uuid": conn_uuid, "from": now}
    )
    merged = dict(rec)
    merged.update(
        {
            "host_key": host_key,
            "session_id": _sanitize_session_key(session_id),
            "dataset": str(dataset).strip(),
            "conn_uuid": str(conn_uuid),
            "switched_at": now,
            "touched": touched,
        }
    )
    merged.setdefault("created_at", now)
    _write_map_record(host_key, merged)
    hook_log(
        "dataset_switched",
        {
            "host_key": host_key,
            "session_id": merged["session_id"],
            "dataset": merged["dataset"],
            "conn_uuid": conn_uuid,
            "touched": len(touched),
        },
    )
    return _read_map_record(host_key) or merged


def resolve_host_key_outside_hook(cwd: str = "") -> tuple[str, str]:
    """Find this launch's host session key from a process that got no hook payload.

    The switch command runs under the host's shell tool, which has no hook stdin.
    Resolution, in order — returns ``(host_key, source)``, ``("", reason)`` when
    nothing matched:
      1. ``COGNEE_SESSION_KEY`` — already inside a hook.
      2. The host's own session-id export (Claude Code: ``CLAUDE_CODE_SESSION_ID``).
      3. The host's pid export or our process ancestry, matched against the
         ``host_pid`` each SessionStart stores in its record.
      4. A single live record whose ``cwd`` equals ours.
    """
    key = get_session_key()
    if key:
        return key, "env_session_key"

    for var in ("CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID"):
        val = _sanitize_session_key(str(os.environ.get(var, "") or "").strip())
        if val and _session_map_path(val).exists():
            return val, var

    records = _live_launch_records()
    pids = _candidate_host_pids()
    if pids:
        by_pid = [r for r in records if int(r.get("host_pid") or 0) in pids]
        if len(by_pid) == 1:
            return str(by_pid[0].get("host_key") or ""), "host_pid"

    cwd = str(cwd or os.getcwd())
    by_cwd = [r for r in records if str(r.get("cwd") or "") == cwd]
    if len(by_cwd) == 1:
        return str(by_cwd[0].get("host_key") or ""), "cwd"
    if len(by_cwd) > 1:
        return "", "ambiguous_cwd"
    return "", "not_found"


def _live_launch_records() -> list[dict]:
    """Launch records whose host process is still alive (or whose pid is unknown)."""
    from _proc import pid_alive

    out: list[dict] = []
    try:
        paths = sorted(_SESSIONS_MAP_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime)
    except Exception:
        return out
    for path in paths:
        rec = _load_json_file(path)
        if not rec or not rec.get("session_id"):
            continue
        rec.setdefault("host_key", path.stem)
        pid = int(rec.get("host_pid") or 0)
        if pid and not pid_alive(pid):
            continue
        out.append(rec)
    return out


def _candidate_host_pids() -> set[int]:
    """Pids that could be this process's host: the host's pid export + ancestry."""
    pids: set[int] = set()
    for var in ("CLAUDE_PID", "CODEX_PID"):
        try:
            v = int(str(os.environ.get(var, "") or "0").strip() or 0)
        except ValueError:
            v = 0
        if v > 1:
            pids.add(v)
    if sys.platform != "win32":
        try:
            raw = subprocess.check_output(
                ["ps", "-axo", "pid=,ppid="], text=True, stderr=subprocess.DEVNULL
            )
            table: dict[int, int] = {}
            for line in raw.splitlines():
                parts = line.split()
                if len(parts) == 2:
                    try:
                        table[int(parts[0])] = int(parts[1])
                    except ValueError:
                        continue
            pid = os.getppid()
            seen: set[int] = set()
            while pid > 1 and pid not in seen:
                seen.add(pid)
                pids.add(pid)
                pid = table.get(pid, 0)
        except Exception:
            pass
    return pids


def list_writable_datasets(user_id: str = "", *, timeout: float = 15.0) -> dict:
    """Datasets this principal can switch to, from ``GET /api/v1/datasets``.

    The endpoint lists datasets the caller can READ; only those it OWNS are
    guaranteed writable (creation grants read/write/share/delete). Returns::

        {"datasets": [{"name", "id", "owner_id", "writable": True|None}],
         "hidden_readonly": N, "filtered": bool}

    A dataset owned by someone else is dropped (counted in ``hidden_readonly``).
    ``writable`` is None — and the row kept — when ownership cannot be judged:
    no ``user_id`` to compare against, or a server whose DTO carries no owner
    (pre-1.6 releases). ``filtered`` is True only when every row was judged, so
    the caller can say whether the list is proven-writable or merely readable;
    the switch itself still rejects a non-writable dataset loudly.
    """
    raw = _json_http_request("/api/v1/datasets", method="GET", timeout=timeout)
    items = raw if isinstance(raw, list) else []
    rows = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        # OutDTO serialises camelCase on the wire (ownerId); accept both spellings.
        owner = str(item.get("owner_id") or item.get("ownerId") or "").strip()
        writable = None
        if owner and user_id:
            writable = owner == str(user_id)
        rows.append(
            {"name": name, "id": str(item.get("id") or ""), "owner_id": owner, "writable": writable}
        )
    rows.sort(key=lambda r: r["name"].lower())
    kept = [r for r in rows if r["writable"] is not False]
    return {
        "datasets": kept,
        "readonly": [r["name"] for r in rows if r["writable"] is False],
        "hidden_readonly": len(rows) - len(kept),
        "filtered": bool(rows) and all(r["writable"] is not None for r in rows),
    }


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
    # The launch's active dataset (switchable) — read from the record so every
    # hook and worker follows a switch, not the shell it was launched from.
    resolved["dataset"] = resolve_active_dataset(active_session_key)
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
                # Which cloud tenant this connection belongs to (null on local
                # single-user servers). The credits display keys its balance
                # entries on this, so multi-tenant machines track each tenant
                # separately (SDK-355).
                tenant_id = str(agent.get("tenant_id") or "").strip()
                if tenant_id:
                    resolved["tenant_id"] = tenant_id
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


def _strip_surrogates(text: str) -> str:
    """Remove lone UTF-16 surrogate codepoints (U+D800-U+DFFF).

    A legitimate supplementary-plane character (emoji, etc.) is always ONE code
    point in Python's str, never a surrogate. Any char in this range in a real
    str is therefore always broken/unpaired (a bad UTF-16<->UTF-8 boundary
    upstream -- Windows console/clipboard, mis-decoded tool output), never a
    valid character. It round-trips silently through json.dumps/loads
    (ensure_ascii escapes it, loads() reconstitutes it) -- only a raw UTF-8
    encode downstream (embedding tokenizer, LLM adapter, cognify) catches it,
    by which point the entry is already persisted. Strip (don't replace) to keep
    surrounding text readable with no placeholder glyph.
    """
    if not text or not any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
        return text
    return "".join(ch for ch in text if not (0xD800 <= ord(ch) <= 0xDFFF))


def _sanitize_value(value):
    """Recursively strip surrogates from every string leaf in a JSON-shaped value.
    Only string VALUES are touched -- dict keys, ints, bools, None pass through."""
    if isinstance(value, str):
        return _strip_surrogates(value)
    if isinstance(value, dict):
        return {k: _sanitize_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(v) for v in value]
    return value


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
    question = _strip_surrogates(question)
    answer = _strip_surrogates(answer)
    trace = _strip_surrogates(trace)

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


# --- Embedding-dimension mismatch detection ---------------------------------
# When the embedding model changes between writing and reading, stored vectors
# and fresh query vectors have different dimensions, so recall silently matches
# nothing. These helpers turn that silent miss into a one-line actionable error
# naming both dimensions and the active embedder. Strictly best-effort and
# fail-safe: any uncertainty returns None, preserving the normal "no matches"
# behavior. Only valid against a *local* store this process can introspect
# (gate callers with ``service_url_is_local``); a remote/cloud store is owned
# by the server and isn't reflected by the in-process engine here.


def service_url_is_local(url: str = "") -> bool:
    """True when the resolved service URL points at this machine (loopback)."""
    host = (urllib.parse.urlparse(url or _local_api_url()).hostname or "").lower()
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


async def _sample_stored_vector_dim(engine) -> Optional[int]:
    """Sample the dimension of a stored vector from any populated collection, or None.

    Enumerates the store's actual collections via the vector interface's
    ``get_connection().table_names()`` (the same path cognee's own ``has_collection``
    uses) rather than assuming fixed collection names, so it also covers custom
    pipelines. Never raises: each collection is probed independently and any
    unreadable one is skipped. Covers cognee's default local backend (LanceDB); other
    backends whose connection can't enumerate return None and fall back to the normal
    empty-recall path.
    """
    try:
        connection = await engine.get_connection()
        names = await connection.table_names()
    except Exception:
        return None
    for name in names:
        try:
            collection = await engine.get_collection(name)
            rows = await collection.query().limit(1).to_list()
            if rows:
                vector = rows[0].get("vector")
                if vector is not None:
                    return len(vector)
        except Exception:
            continue
    return None


async def embedding_dimension_mismatch_hint(engine=None) -> Optional[str]:
    """One-line diagnostic when the stored vectors differ in size from the active
    embedder's query vectors (so recall can never match), else None.

    Best-effort and fail-safe: any error, or an indeterminate/matching dimension,
    returns None so the caller keeps the normal empty-recall behavior. ``engine``
    is injectable for testing.
    """
    try:
        if engine is None:
            from cognee.infrastructure.databases.vector import get_vector_engine

            engine = get_vector_engine()
        embed = getattr(engine, "embedding_engine", None)
        if embed is None:
            return None
        query_dim = int(embed.get_vector_size())
        stored_dim = await _sample_stored_vector_dim(engine)
        if not stored_dim or not query_dim or stored_dim == query_dim:
            return None
        model = getattr(embed, "model", None) or "unknown-model"
        provider = getattr(embed, "provider", None) or "unknown-provider"
        return (
            "Cognee recall found nothing because the embedder changed: stored vectors are "
            f"{stored_dim}-d but the active embedder '{model}' (provider '{provider}') produces "
            f"{query_dim}-d queries. Re-index this data with the current embedder, or set "
            f"EMBEDDING_MODEL/EMBEDDING_DIMENSIONS back to the {stored_dim}-d model that wrote it."
        )
    except Exception:
        return None


_DIM_MEMO_FILE = _PLUGIN_DIR / "dim_check.json"
_DIM_MEMO_TTL = 300.0  # seconds; re-probe at most this often per embedder signature


def _embedder_signature() -> str:
    """Cheap identity of the active embedder, read from env WITHOUT importing cognee
    — the only query-side input to the mismatch check. A change here (model, dimension,
    or provider) invalidates any cached probe result."""
    return "|".join(
        os.getenv(k, "") for k in ("EMBEDDING_MODEL", "EMBEDDING_DIMENSIONS", "EMBEDDING_PROVIDER")
    )


def _read_dim_memo(sig: str) -> Optional[dict]:
    """Return the cached probe result for ``sig`` if present and fresh, else None.
    Never raises."""
    try:
        data = json.loads(_DIM_MEMO_FILE.read_text(encoding="utf-8"))
        if data.get("sig") == sig and (time.time() - float(data.get("ts", 0))) < _DIM_MEMO_TTL:
            return data
    except Exception:
        pass
    return None


def _write_dim_memo(sig: str, message: Optional[str]) -> None:
    """Persist a completed probe result keyed by embedder signature. Never raises."""
    try:
        _DIM_MEMO_FILE.parent.mkdir(parents=True, exist_ok=True)
        _DIM_MEMO_FILE.write_text(
            json.dumps({"sig": sig, "message": message, "ts": time.time()}),
            encoding="utf-8",
        )
    except Exception:
        pass


async def bounded_dim_mismatch_hint(timeout: float = 2.0) -> Optional[str]:
    """``embedding_dimension_mismatch_hint`` made safe for the per-prompt hook path.

    The probe's first step is a synchronous ``import cognee`` + ``get_vector_engine()``.
    In the plugin's default http/local-server mode cognee is not otherwise imported, so
    that is a cold ~1s import running *before the first await* — which a plain
    ``asyncio.wait_for`` cannot bound (it blocks the event loop). So we run the whole
    probe in a daemon thread and bound the *wait*: on timeout we return None and abandon
    the daemon, so a slow import can never stall the hook or delay its process exit. The
    completed result is memoized on disk per embedder signature (TTL-bounded) so repeated
    empty recalls in a session don't each pay the import.

    Fail-safe: any error, timeout, or indeterminate result returns None, so the caller
    keeps the normal empty-recall behavior.
    """
    sig = _embedder_signature()
    cached = _read_dim_memo(sig)
    if cached is not None:
        return cached.get("message")

    loop = asyncio.get_running_loop()
    future: asyncio.Future = loop.create_future()

    def _settle(value: Optional[str]) -> None:
        if not future.done():
            future.set_result(value)

    def _worker() -> None:
        message: Optional[str] = None
        try:
            message = asyncio.run(embedding_dimension_mismatch_hint())
        except Exception:
            message = None
        try:
            loop.call_soon_threadsafe(_settle, message)
        except Exception:
            pass  # loop already closed (we timed out); the daemon's result is discarded

    threading.Thread(target=_worker, name="cognee-dim-probe", daemon=True).start()
    try:
        message = await asyncio.wait_for(future, timeout=timeout)
    except Exception:
        return None
    _write_dim_memo(sig, message)
    return message


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
        _append_log_line(_HOOK_LOG, serialized)
    except Exception:
        pass


_SSL_CONTEXT: "ssl.SSLContext | None" = None


def _https_context() -> ssl.SSLContext:
    """Shared TLS context for every urllib HTTPS call (cloud/remote mode).

    macOS Python builds often ship without root CA certs in the default
    context, so HTTPS verification against Cognee Cloud fails with
    CERTIFICATE_VERIFY_FAILED. Mirror the recall path's resolution once, here,
    so all HTTPS traffic shares it: prefer certifi, else walk SSL_CERT_FILE and
    known system cert bundles. Built once and cached. Passing this to urlopen
    for an http:// (localhost) URL is harmless — urllib ignores the context for
    non-HTTPS requests.
    """
    global _SSL_CONTEXT
    if _SSL_CONTEXT is not None:
        return _SSL_CONTEXT
    try:
        import certifi

        ctx = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        ctx = ssl.create_default_context()
        loaded = False
        for path in filter(
            None,
            [
                os.environ.get("SSL_CERT_FILE"),
                "/etc/ssl/cert.pem",
                "/etc/ssl/certs/ca-certificates.crt",
            ],
        ):
            if os.path.exists(path):
                try:
                    ctx.load_verify_locations(path)
                    loaded = True
                    break
                except Exception:
                    pass
        if not loaded:
            hook_log("https_context_no_ca_bundle", {})
    _SSL_CONTEXT = ctx
    return ctx


def _reexec_into_venv() -> None:
    """Re-exec the current hook under the shared plugin-owned venv interpreter.

    Hooks are launched by the host as ``python3 <script>`` using whatever
    python3 is on PATH — which has neither cognee nor aiohttp. The runtime
    lives in ``~/.cognee-plugin/venv``. Once that venv exists, re-exec into it
    so every import resolves there. No-op before the venv exists (cold start,
    pre-install) or when already running inside it.

    "Already inside" is judged by ``sys.prefix``, never by comparing
    interpreter files: a venv's ``bin/python`` is a symlink to its base
    interpreter, so ``os.path.samefile(venv_python, sys.executable)`` is also
    true when running under that base directly (e.g. CI, where setup-python's
    3.12 is both the ``python3`` that launches hooks and the base uv built the
    venv from) — which has no cognee. ``sys.prefix`` only equals the venv dir
    when the process was launched through the venv's own path.
    """
    if os.environ.get("COGNEE_PLUGIN_IN_VENV") == "1":
        return  # loop guard: this process already re-execed (or opted out)
    if not sys.argv or not os.path.isfile(sys.argv[0]):
        return  # not a `python script.py` launch (e.g. -c/-m/stdin) — don't rebuild argv
    vpy = _VENV_PYTHON
    if not vpy.exists():
        return  # cold start — install hasn't built the venv yet
    try:
        if Path(sys.prefix).resolve() == _VENV_DIR.resolve():
            return  # already running inside the plugin venv
    except OSError:
        pass
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
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
            ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _append_log_line(_ACTIVITY_LOG, f"{ts} {line}")
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
    # The child writes this fd itself, so the cap can only be applied here.
    _rotate_log_if_oversized(_SUBPROCESS_LOG)
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
    prompt = _strip_surrogates(prompt)
    context = _strip_surrogates(context)
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
def improve_session_lock(session_id: str, owner: str):
    """Admit exactly one in-flight improve per session, machine-wide.

    Three paths bridge the same session — the idle watcher, ``store-to-session``,
    and the SessionEnd sync — and the outer ``sync_lock`` is bypassed in API mode
    (``nullcontext(True) if api_mode``), so in HTTP/cloud mode nothing stopped two
    of them submitting the same session concurrently. The server's own per-session
    lock then answered the loser with ``{}`` (busy), which drove a 15s retry loop
    for up to ten minutes; concurrent writers also collide on the single-writer
    graph/vector store ("Could not set lock on file"), leaving pipeline runs stuck
    and the graph unwritten.

    So claim locally BEFORE submitting: the loser skips entirely rather than
    waiting, because the winner is already bridging the very same session — the
    work is not lost, it is in flight. Yields True when claimed, False when
    another process owns it.

    Mirrors ``sync_lock``'s stale handling (dead pid or older than
    ``SYNC_LOCK_STALE_SECONDS``) so a crashed worker cannot wedge a session, and
    fails OPEN on unexpected errors — a lock we cannot manage must never be the
    reason a session goes unsynced.
    """
    if not session_id:
        yield True
        return

    digest = hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()
    lock_path = _IMPROVE_LOCK_DIR / f"{digest}.lock"
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).timestamp()
        if lock_path.exists():
            try:
                current = json.loads(lock_path.read_text(encoding="utf-8"))
                created_at = float(current.get("created_at", 0))
                pid = int(current.get("pid", 0))
            except Exception:
                created_at, pid = 0.0, 0
            if not (pid > 0 and _proc.pid_alive(pid)) or now - created_at > SYNC_LOCK_STALE_SECONDS:
                try:
                    lock_path.unlink()
                    hook_log(
                        "improve_lock_stale_cleared",
                        {"session": session_id, "owner": owner, "stale_pid": pid},
                    )
                except FileNotFoundError:
                    pass  # another process cleared the same stale lock
                except Exception as exc:
                    hook_log("improve_lock_unlink_failed", {"error": str(exc)[:200]})
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"owner": owner, "pid": os.getpid(), "created_at": now}, fh)
            acquired = True
            yield True
        except FileExistsError:
            hook_log("improve_skipped_concurrent", {"session": session_id, "owner": owner})
            yield False
    except Exception as exc:
        # Fail open: never let lock bookkeeping cost a session its sync.
        hook_log("improve_lock_failed_open", {"session": session_id, "error": str(exc)[:200]})
        yield True
    finally:
        if acquired:
            try:
                lock_path.unlink()
            except Exception as exc:
                hook_log("improve_lock_release_failed", {"error": str(exc)[:200]})


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
                pid_alive = _proc.pid_alive(pid)
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


def _local_api_url_with_source() -> tuple[str, str]:
    """Resolve the runtime endpoint without assuming hook env propagation."""
    local_env = str(os.environ.get("COGNEE_LOCAL_API_URL", "") or "").strip()
    if local_env:
        return local_env, "env_local_api_url"
    service_env = str(os.environ.get("COGNEE_BASE_URL", "") or "").strip()
    if service_env:
        return service_env, "env_service_url"

    return _DEFAULT_LOCAL_SERVICE_URL, "default_local"


def _local_api_url() -> str:
    return _local_api_url_with_source()[0]


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

    Service URL falls back through plugin config before localhost. API key is
    the single principal key: env first, then the single cached key.
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


def probe_health(service_url: str = "", timeout: float = 1.0) -> str:
    """Classified GET {service_url}/health probe.

    Returns:
      "ready"   — 200 (the server runs migrations in its FastAPI lifespan
                  *before* it serves, so this reliably means migrations are
                  done and the DBs are reachable)
      "down"    — connection refused / DNS / unroutable: positively absent
      "slow"    — timed out: NO verdict (a busy server times out; a dead one
                  refuses in milliseconds). Callers must keep prior state.
      "unknown" — non-200 status, SSL trouble, resets, or no URL: no verdict
    """
    base = _normalize_service_url(service_url or _local_api_url())
    if not base:
        return UNKNOWN
    try:
        with urllib.request.urlopen(
            f"{base}/health", timeout=timeout, context=_https_context()
        ) as resp:
            return "ready" if resp.status == 200 else UNKNOWN
    except Exception as exc:
        verdict = classify_transport_exception(exc)
        return verdict if verdict in (DOWN, SLOW) else UNKNOWN


def server_health_ok(service_url: str = "", timeout: float = 1.0) -> bool:
    """Return True iff /health responds 200. Boolean face of ``probe_health``.

    Callers that must react to *failures* should use ``probe_health`` instead:
    this bool cannot distinguish "down" (write a failure state) from "slow"
    (no verdict — keep prior state).
    """
    return probe_health(service_url, timeout=timeout) == "ready"


# --- Server presence (boot-point evidence) -------------------------------------
# probe_health cannot tell a BUSY server from an ABSENT one: both miss the HTTP
# deadline, but only one of them may be installed/booted over. A server that is
# busy (event loop saturated by a pipeline) misses a 2s probe exactly like a
# dead one — and treating that as absence lets a boot point upgrade the venv
# and run migrations UNDER a live server that still holds the graph store's
# file lock. Presence is therefore judged from three evidence sources:
#
#   * HTTP probe    — probe_health; only a 200 is a self-sufficient verdict.
#   * TCP listener  — a busy server still completes the TCP handshake in
#     microseconds even when it cannot serve HTTP; a dead one refuses the
#     connection. This is the busy-vs-dead discriminator.
#   * server pidfile — written at uvicorn spawn; covers the window between
#     spawn and port bind, when neither probe nor listener sees the server.
#
# The asymmetry is deliberate: extra evidence only ever ADDS presence (vetoing
# a boot), and absence is only concluded from a positively refused port with no
# live server pid — never from a timeout. A wrong "busy" delays a boot until
# the next boot point; a wrong "absent" corrupts databases.

PRESENCE_READY = "ready"  # HTTP 200: serving (lifespan migrations are done)
PRESENCE_BUSY = "busy"  # evidence of a live server that is not serving
PRESENCE_ABSENT = "absent"  # positively absent — the only install/boot license
PRESENCE_UNKNOWN = "unknown"  # conflicting/insufficient evidence: treat as busy

# Second-chance probe budget when confirming absence (see server_presence).
_PRESENCE_REPROBE_TIMEOUT_SECONDS = 5.0


def _presence_reprobe_delay() -> float:
    """Pause before the absence-confirming re-probe. Read per call so tests
    (and unusual deployments) can shrink it without re-importing the module."""
    try:
        return float(os.environ.get("COGNEE_PRESENCE_REPROBE_DELAY", "") or 3.0)
    except ValueError:
        return 3.0


def _server_pidfile(port: int) -> Path:
    # Shared root, not the per-integration dir: the server itself is
    # machine-wide (one per port), whichever integration's boot point spawned it.
    return _SHARED_PLUGIN_ROOT / f"server-{int(port)}.pid"


def write_server_pidfile(port: int, pid: int, version: str = "") -> None:
    """Record the uvicorn server spawned on ``port`` (presence evidence)."""
    try:
        _write_json_file(
            _server_pidfile(port),
            {
                "pid": int(pid),
                "port": int(port),
                "version": version,
                "created_at": datetime.now(timezone.utc).timestamp(),
            },
        )
    except Exception as exc:
        hook_log("server_pidfile_write_failed", {"error": str(exc)[:200]})


def clear_server_pidfile(port: int) -> None:
    try:
        _server_pidfile(port).unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        hook_log("server_pidfile_clear_failed", {"error": str(exc)[:200]})


def _pid_looks_like_server(pid: int) -> bool:
    """Best-effort check that ``pid``'s command line still looks like the
    cognee server (guards against OS pid reuse). When the command line cannot
    be inspected (no ``ps``, permission trouble) err toward presence: pidfile
    evidence is veto-only, so the cost of a wrong True is a delayed boot, and
    it self-heals when the reused pid exits (``pid_alive`` gates before this)."""
    try:
        out = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=5,
        )
        command = (out.stdout or "").strip().lower()
        if not command:
            return False
        return "uvicorn" in command or "cognee" in command
    except Exception:
        return True


def _live_server_pid(port: int) -> int:
    """PID from the port's pidfile iff that process is alive and still looks
    like the server; 0 otherwise. Stale records (dead or reused pid) are
    reaped here so they can never veto boots forever."""
    path = _server_pidfile(port)
    try:
        pid = int(json.loads(path.read_text(encoding="utf-8")).get("pid", 0) or 0)
    except FileNotFoundError:
        return 0
    except Exception:
        pid = 0
    if pid > 0 and _proc.pid_alive(pid) and _pid_looks_like_server(pid):
        return pid
    try:
        path.unlink()
    except Exception:
        pass
    return 0


def _windows_listening_verdict(port: int) -> str:
    """'listening' | 'refused' | 'no_verdict' from the OS TCP table (Windows).

    Windows Firewall stealth mode drops the SYN to a closed port instead of
    answering RST — loopback included — so a refused connect there just times
    out and the positive "refused" signal can never be observed from a connect
    attempt, at any budget. The listening table is the authority instead: a
    port with no LISTEN row is positively free.

    Rows are matched structurally — local address ends in ``:port`` and the
    remote is the unconnected ``0.0.0.0:0`` / ``[::]:0`` placeholder that only
    LISTEN rows carry — because netstat localizes the state word ("LISTENING",
    "ABHÖREN", …) but never the addresses.
    """
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=10)
    except Exception:
        return "no_verdict"
    if out.returncode != 0:
        return "no_verdict"
    suffix = f":{port}"
    for line in out.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0].upper() != "TCP":
            continue
        if parts[1].endswith(suffix) and parts[2] in ("0.0.0.0:0", "[::]:0"):
            return "listening"
    return "refused"


def tcp_probe(host: str, port: int, timeout: float = 0.5) -> str:
    """Classify the bare TCP handshake: 'listening' | 'refused' | 'no_verdict'.

    'refused' is a positive signal from the OS that nothing holds the port —
    the only transport answer that may contribute to an absence verdict.
    Timeouts and filtered/odd socket states give no verdict, same as the HTTP
    probe's rules.

    On Windows a connect cannot yield that positive signal (see
    ``_windows_listening_verdict``), so a no-verdict connect falls back to the
    OS listening table there. A live listener still answers the handshake in
    microseconds on every platform, so the connect attempt stays first.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return "listening"
    except ConnectionRefusedError:
        return "refused"
    except OSError as exc:
        if getattr(exc, "errno", None) == errno.ECONNREFUSED:
            return "refused"
        if os.name == "nt":
            return _windows_listening_verdict(port)
        return "no_verdict"
    except Exception:
        return "no_verdict"


def _is_loopback_host(host: str) -> bool:
    host = (host or "").lower()
    return host in ("localhost", "::1") or host.startswith("127.")


def server_presence(
    service_url: str = "",
    probe_timeout: float = 2.0,
    confirm_absent: bool = True,
) -> tuple[str, dict]:
    """Classify whether a server exists at ``service_url`` from all local
    evidence. Returns ``(verdict, evidence)``; the evidence dict is shaped for
    hook_log so every boot decision records WHY it was made.

    Only PRESENCE_ABSENT licenses installing or booting. ``confirm_absent``
    adds one delayed, longer-budget re-probe before concluding absence — for
    boot points about to install; pass False where a rigorous check follows
    later anyway (e.g. mode selection, whose boot path re-verifies).

    Local evidence (TCP, pidfile) only applies to loopback hosts: for remote
    URLs the HTTP probe is all there is, and a non-ready remote is UNKNOWN —
    never absent (nothing can boot a remote host anyway).
    """
    base = _normalize_service_url(service_url or _local_api_url())
    evidence: dict = {"base_url": base}
    if not base:
        return PRESENCE_UNKNOWN, evidence
    if "://" not in base:
        base = f"http://{base}"

    http_verdict = probe_health(base, timeout=probe_timeout)
    evidence["http"] = http_verdict
    if http_verdict == "ready":
        return PRESENCE_READY, evidence

    parsed = urllib.parse.urlsplit(base)
    host = parsed.hostname or "localhost"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    if not _is_loopback_host(host):
        return PRESENCE_UNKNOWN, evidence

    tcp_verdict = tcp_probe(host, port)
    evidence["tcp"] = tcp_verdict
    if tcp_verdict == "listening":
        # Alive but not serving HTTP within budget (busy, wedged, or answering
        # non-200): a server exists. Never boot over it.
        return PRESENCE_BUSY, evidence

    pid = _live_server_pid(port)
    if pid:
        # Spawned but not (yet) bound to the port — starting up or tearing
        # down. Either way a server process exists right now.
        evidence["pid"] = pid
        return PRESENCE_BUSY, evidence

    if tcp_verdict != "refused":
        return PRESENCE_UNKNOWN, evidence

    if confirm_absent:
        # Positively refused with no live pid. Give a recovering/just-starting
        # server one more chance before licensing an install: the original
        # incident had 37s between "live" and the probe that booted over it.
        time.sleep(_presence_reprobe_delay())
        retry_verdict = probe_health(base, timeout=_PRESENCE_REPROBE_TIMEOUT_SECONDS)
        evidence["http_retry"] = retry_verdict
        if retry_verdict == "ready":
            return PRESENCE_READY, evidence
        retry_tcp = tcp_probe(host, port)
        if retry_tcp == "listening":
            evidence["tcp_retry"] = retry_tcp
            return PRESENCE_BUSY, evidence
    return PRESENCE_ABSENT, evidence


# Connection states recorded in the (shared) server-ready marker. "ready" means
# the server is up AND authenticated; the failure states carry the reason shown
# in the status line as "✕ (<state>)". Any non-"ready" state makes
# server_ready_hint return False so recall does not attempt against a bad backend.
# "unreachable" is reserved for POSITIVE absence (connection refused / DNS /
# unroutable). "not_responding" is deliberately distinct: the server exists
# (connections are not refused) but has not answered within budget for N
# consecutive prompts — written only by the slow-streak escalation (see
# record_slow_probe), never by a lone timeout.
CONNECTION_STATES = ("ready", "auth_failed", "unreachable", "server_error", "not_responding")


# Per-session copies of the status markers. The shared files above are
# COORDINATION state (is the server up, should recall run) and are deliberately
# machine-wide; these are DISPLAY state, answering "what did THIS terminal
# experience". They have to be separate: two terminals can legitimately disagree
# — one exported LLM_API_KEY and the other didn't, or they hold different
# COGNEE_API_KEYs — and with a single file the last writer decided what every
# other bar showed (a keyless launch's "not_set" greying out a healthy session,
# or a healthy one's "ok" hiding a genuinely missing key).
_LLM_STATE_DIR = _PLUGIN_DIR / "llm-state"
_CONN_STATE_DIR = _PLUGIN_DIR / "conn-state"


def _session_key_path_safe(key: str) -> bool:
    """True when `key` is safe to use as a single filename component.

    Excludes every path separator (`/`, `\\`) and drive/stream punctuation (`:`), so a
    key can only ever name a file INSIDE the target directory — `..` becomes the
    literal filename `...json`, not a parent-directory hop. The status-line renderer
    keeps its own copy of this predicate (`_path_safe`) on purpose: it is standalone
    by design and must not import this module.
    """
    return bool(key) and all(c.isalnum() or c in "._-" for c in key)


def _write_session_marker(directory: Path, payload: dict) -> None:
    """Mirror a status payload into ``<directory>/<session_key>.json``.

    No-op when this process has no session key (e.g. an early bootstrap write):
    the shared marker still gets written, and readers treat an unattributed
    record as "could be mine" so nothing is lost. Best-effort, never raises.
    """
    key = get_session_key()
    if not _session_key_path_safe(key):
        return
    try:
        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / f".{os.getpid()}.json.tmp"
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, directory / f"{key}.json")
    except Exception as exc:
        hook_log("session_marker_write_failed", {"dir": directory.name, "error": str(exc)[:200]})


def write_connection_state(
    state: str, service_url: str = "", *, detail: str = "", version: str = ""
) -> None:
    """Record the last connection outcome in the shared server-ready marker.

    Global (not namespaced) because Claude and Codex share one server on the
    same port. Read by hot-path hooks via ``server_ready_hint`` (recall gate) and
    by the status-line renderer (which reads the file directly). ``state`` is one
    of ``CONNECTION_STATES``; unknown values are coerced to "unreachable".
    """
    if state not in CONNECTION_STATES:
        state = "unreachable"
    try:
        _SERVER_READY_MARKER.parent.mkdir(parents=True, exist_ok=True)
        now = datetime.now(timezone.utc).timestamp()
        payload = {
            "state": state,
            "base_url": _normalize_service_url(service_url),
            "checked_at": now,
            # ready_at kept for backward-compat with any un-upgraded reader; only
            # advanced on a successful (ready) check.
            "ready_at": now if state == "ready" else 0,
            "version": str(version or ""),
            "detail": str(detail or "")[:200],
            # Which terminal observed this. Two sessions can hold different
            # COGNEE_API_KEYs against the same base_url, so "auth_failed" is not
            # necessarily everyone's truth.
            "session_key": get_session_key(),
        }
        tmp = _SERVER_READY_MARKER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _SERVER_READY_MARKER)
        _write_session_marker(_CONN_STATE_DIR, payload)
    except Exception as exc:
        hook_log("connection_state_write_failed", {"state": state, "error": str(exc)[:200]})


def mark_server_ready(service_url: str, version: str = "") -> None:
    """Back-compat shim: record a healthy, authenticated connection ("ready")."""
    write_connection_state("ready", service_url, version=version)


# Slow-server hysteresis. A single timeout is "no verdict" and must not touch
# the connection marker — but N consecutive timeout-only prompts with no
# success in between are a pattern (wedged server, packet-dropping network),
# not a blip. This counter, keyed by base_url, is how a lone blip stays
# invisible while a persistent stall still escalates to a visible "slow" state
# (and recall backoff) instead of leaving the bar green forever.
_SLOW_STREAK_FILE = _PLUGIN_DIR / "slow-streak.json"


def slow_streak_threshold() -> int:
    """Consecutive timeout-only prompts before escalating to state "slow"."""
    try:
        return max(1, int(os.environ.get("COGNEE_SLOW_STREAK_THRESHOLD", "3") or 3))
    except (TypeError, ValueError):
        return 3


def _slow_streak_window_seconds() -> float:
    """Ticks further apart than this don't chain — a streak must be recent."""
    try:
        return float(os.environ.get("COGNEE_SLOW_STREAK_WINDOW", "600") or 600)
    except (TypeError, ValueError):
        return 600.0


def _read_slow_streaks() -> dict:
    try:
        raw = json.loads(_SLOW_STREAK_FILE.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _write_slow_streaks(state: dict) -> None:
    try:
        _SLOW_STREAK_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SLOW_STREAK_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        os.replace(tmp, _SLOW_STREAK_FILE)
    except Exception:
        pass


def record_slow_probe(service_url: str) -> int:
    """Count a consecutive no-verdict (timeout) observation; return the streak.

    The streak resets itself when the previous tick is older than the window —
    two timeouts hours apart are noise, not a pattern. The caller escalates to
    ``write_connection_state("not_responding", ...)`` once the return value
    reaches ``slow_streak_threshold()``.
    """
    url = _normalize_service_url(service_url)
    now = datetime.now(timezone.utc).timestamp()
    state = _read_slow_streaks()
    entry = state.get(url) if isinstance(state.get(url), dict) else {}
    try:
        last_at = float(entry.get("last_at") or 0)
        count = int(entry.get("count") or 0)
    except (TypeError, ValueError):
        last_at, count = 0.0, 0
    if now - last_at > _slow_streak_window_seconds():
        count = 0
    count += 1
    state[url] = {"count": count, "last_at": now}
    _write_slow_streaks(state)
    return count


def clear_slow_streak(service_url: str) -> None:
    """A definitive observation (success OR hard failure) ends the streak."""
    url = _normalize_service_url(service_url)
    state = _read_slow_streaks()
    if url in state:
        state.pop(url, None)
        _write_slow_streaks(state)


def same_connection_target(service_url: str, prior_url: str) -> bool:
    """True unless the two URLs are *provably* different servers.

    Deliberately permissive: when either side is unknown we treat a prior record as
    being about this target. That direction is chosen on purpose — the caller uses
    this to decide whether a failed probe means "the server we were talking to just
    died" (report it) or "a server we know nothing about is still warming up" (stay
    quiet). Flipping it to require both URLs would swallow a genuine death whenever a
    URL is missing, which is the case this branch exists to catch.

    The status-line renderer holds the mirror image of this predicate
    (``_url_mismatch``), and the two MUST stay equivalent:
    ``same_connection_target(a, b) == (not _url_mismatch(a, b))``. A hook that records
    a state the renderer then ignores — or vice versa — leaves the user looking at a
    stale glyph. The renderer cannot import this module (it is standalone by design),
    so ``tests/test_connection_target_match.py`` pins the equivalence instead.
    """
    active = _normalize_service_url(service_url)
    marked = _normalize_service_url(prior_url)
    return not (active and marked and active != marked)


def read_connection_state() -> dict:
    """Return the connection marker dict (with 'state'), or {} — for hook use.

    The status-line renderer does NOT use this (it stays import-free of
    ``_plugin_common`` and reads the file directly); this is for network hooks
    that need to know the prior state (e.g. warming-vs-died).
    """
    try:
        raw = json.loads(_SERVER_READY_MARKER.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    if "state" not in raw and raw.get("ready_at"):
        raw["state"] = "ready"
    return raw


def authed_liveness(service_url: str = "", api_key: str = "", timeout: float = 1.5) -> str:
    """Classify the connection via an AUTHENTICATED probe (GET /api/v1/datasets).

    Unlike ``server_health_ok`` (which hits the unauthenticated ``/health`` and so
    can't tell a bad key from a good one), this sends ``X-Api-Key`` to an endpoint
    that requires auth, so it distinguishes:
      "ready"        — 2xx (server up and the key is accepted)
      "auth_failed"  — 401/403 (server up, key rejected)
      "server_error" — 5xx
      "unreachable"  — connection refused / DNS / unroutable (positively absent)
      "slow"         — timed out: NO verdict, the server may simply be busy.
                       Callers must keep their prior state, not record a failure.
      "unknown"      — endpoint absent (404/405), no key to send, or an
                       unclassifiable transport error; caller should fall back
                       to ``probe_health`` rather than trust this
    Returns a string in ``CONNECTION_STATES``, "slow", or "unknown" — "slow"
    is a probe verdict only, never a recorded marker state. Never raises.
    """
    base = _normalize_service_url(service_url or _local_api_url())
    if not base:
        return "unknown"
    key = str(api_key or _api_key() or "").strip()
    if not key:
        # No key to authenticate with — can't classify auth; let the caller
        # fall back to an unauthenticated reachability check.
        return "unknown"
    req = urllib.request.Request(f"{base}/api/v1/datasets", method="GET")
    req.add_header("X-Api-Key", key)
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
            status = resp.status
            if 200 <= status < 300:
                return "ready"
            if status >= 500:
                return "server_error"
            return "unknown"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return "auth_failed"
        if exc.code >= 500:
            return "server_error"
        return "unknown"
    except Exception as exc:
        verdict = classify_transport_exception(exc)
        if verdict == DOWN:
            return "unreachable"
        return "slow" if verdict == SLOW else "unknown"


# LLM-key health surfaced in the status line — LOCAL mode only, since LLM_API_KEY
# is unused when talking to a remote server. Kept in its OWN marker (not
# server-ready.json) so a plain overwrite suffices — no read-modify-write merge, no
# race with the server marker. SINGLE WRITER: the idle watcher's _check_llm_key,
# which resolves the key exactly as the server does (cognee's get_llm_config) and
# validates it against the provider. Hooks deliberately do NOT write a verdict from
# their own env: a session launched without the export would flag "not_set" into
# this machine-wide marker and put a false ✕ on every other session's status line.
# States:
#   "not_set"     — no LLM key configured anywhere the server would look
#   "auth_failed" — key present but rejected by the provider (401/403)
#   "ok"          — key accepted (renders nothing)
# Readers apply a TTL (see the renderer's _LLM_STATE_STALE_SECONDS), so a verdict
# left behind by a dead session stops accusing a key the user has since fixed.
_LLM_STATE_MARKER = _PLUGIN_DIR / "llm-state.json"
LLM_STATES = ("ok", "not_set", "auth_failed")


def write_llm_state(state: str, detail: str = "") -> None:
    """Record LLM-key health (local mode). Plain atomic overwrite; never raises.

    Stamped with the writing session's host key: the key is resolved from the
    writer's OWN environment, so a session launched from a shell without the
    export legitimately sees no key — without this stamp its "not_set" would
    land in the machine-wide marker and put a false ✕ on every other session's
    status line (observed: one keyless launch clobbering a validated "ok").
    Readers show a verdict only when it is theirs, or unattributable.
    """
    if state not in LLM_STATES:
        state = "ok"
    try:
        _LLM_STATE_MARKER.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "llm_state": state,
            "checked_at": datetime.now(timezone.utc).timestamp(),
            "session_key": get_session_key(),
            "detail": str(detail or "")[:200],
        }
        tmp = _LLM_STATE_MARKER.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, _LLM_STATE_MARKER)
        _write_session_marker(_LLM_STATE_DIR, payload)
    except Exception as exc:
        hook_log("llm_state_write_failed", {"state": state, "error": str(exc)[:200]})


def read_llm_state() -> dict:
    """Return this session's LLM-state record, else the shared one, else {}.

    Prefers the per-session copy so the watcher's throttle and the status line both
    reason about THIS terminal's verdict rather than whichever session wrote last.
    """
    key = get_session_key()
    if _session_key_path_safe(key):
        try:
            raw = json.loads((_LLM_STATE_DIR / f"{key}.json").read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return raw
        except Exception:
            pass
    try:
        raw = json.loads(_LLM_STATE_MARKER.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def clear_llm_state() -> None:
    """Remove the LLM-state marker (e.g. a key is present and will be validated)."""
    try:
        _LLM_STATE_MARKER.unlink()
    except FileNotFoundError:
        return
    except Exception as exc:
        hook_log("llm_state_clear_failed", {"error": str(exc)[:200]})


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
    # Only a "ready" state counts as ready. A recorded failure (auth_failed /
    # unreachable / server_error) makes the gate skip so recall never hammers a
    # bad backend. Legacy markers (no 'state', have ready_at) are treated ready.
    state = str(raw.get("state") or ("ready" if raw.get("ready_at") else ""))
    if state != "ready":
        return False
    checked_at = float(raw.get("checked_at", 0) or raw.get("ready_at", 0) or 0)
    if datetime.now(timezone.utc).timestamp() - checked_at > _SERVER_READY_TTL_SECONDS:
        return False
    if service_url:
        marked = _normalize_service_url(raw.get("base_url", ""))
        if marked and marked != _normalize_service_url(service_url):
            return False
    return True


# A failed usability probe is memoized here so a genuinely-down server costs
# one probe per backoff window across all hooks, not one per tool call.
_PROBE_FAIL_MEMO = _PLUGIN_DIR / "probe-fail.json"
_PROBE_FAIL_BACKOFF_SECONDS = 10.0


def server_usable(service_url: str = "", probe_timeout: float = 1.0) -> bool:
    """Ready hint, refreshed by a cheap /health probe when stale.

    ``server_ready_hint`` alone conflates "marker TTL expired" with "server
    down": the marker is only refreshed on the prompt path, so during a long
    agent turn it goes stale while the server is healthy — and every write
    hook then buffers to the warmup spillway for no reason, leaving a backlog
    that some later hook has to drain (#298). On a stale hint this probes once
    (bounded by ``probe_timeout``) and re-marks ready on success, so the write
    hooks keep the marker fresh for the whole turn and the buffer only fills
    when the server is actually unreachable.
    """
    if server_ready_hint(service_url):
        return True
    now = datetime.now(timezone.utc).timestamp()
    try:
        memo = json.loads(_PROBE_FAIL_MEMO.read_text(encoding="utf-8"))
        if now - float(memo.get("failed_at", 0) or 0) < _PROBE_FAIL_BACKOFF_SECONDS:
            return False
    except Exception:
        pass
    if server_health_ok(service_url, timeout=probe_timeout):
        mark_server_ready(service_url)
        try:
            _PROBE_FAIL_MEMO.unlink()
        except Exception:
            pass
        return True
    try:
        _PROBE_FAIL_MEMO.parent.mkdir(parents=True, exist_ok=True)
        _PROBE_FAIL_MEMO.write_text(
            json.dumps({"failed_at": now, "base_url": service_url}), encoding="utf-8"
        )
    except Exception:
        pass
    return False


# --- Credits marker (status-line budget display, SDK-355) ---------------------
# The status-line renderer is pure-local by contract, so the credits balance it
# shows comes from this marker, written by hooks/watchers that are already
# allowed to touch the network. Cloud-only: a local server has no credit
# concept, so the fetch is gated on a non-loopback base URL (the renderer
# independently gates on its mode label).
_CREDITS_MARKER = _PLUGIN_DIR / "credits.json"
_PLATFORM_API_URL_DEFAULT = "https://api.aws.cognee.ai"


def _platform_api_url() -> str:
    """The cloud control-plane API host (billing/account routes).

    Distinct from the memory data plane: cloud sessions talk to a per-tenant
    host (``tenant-<id>.aws.cognee.ai``), which serves recall/remember/improve
    but has NO billing routes — asking it for the credits overview 404s. The
    billing routes live only on the platform API, which accepts the same
    tenant ``COGNEE_API_KEY``. Overridable for other cloud deployments.
    """
    return (
        str(os.environ.get("COGNEE_PLATFORM_API_URL", "") or _PLATFORM_API_URL_DEFAULT)
        .strip()
        .rstrip("/")
    )


# The marker is a MAP keyed by tenant id: several concurrent Claude sessions
# on one machine can be connected to DIFFERENT cloud tenants, and a flat
# last-writer-wins record made them clobber each other's balance. Each entry
# carries the service base_url it was observed under — that binding is how
# readers with only a URL in hand (the renderer, the Stop hook) find their
# tenant's entry.
_CREDITS_LOCK = _PLUGIN_DIR / "credits.lock"
_CREDITS_LOCK_STALE_SECONDS = 30.0
_CREDITS_ENTRY_MAX_AGE_SECONDS = 7 * 24 * 3600.0


def read_credits_marker() -> dict:
    """Return the tenant-keyed credits map, or {} — never raises."""
    try:
        raw = json.loads(_CREDITS_MARKER.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        return {}


def _credits_entry_for_url(marker: dict, service_url: str) -> tuple[str, dict]:
    """Find the (tenant_id, entry) bound to ``service_url``, or ("", {})."""
    want = _normalize_service_url(service_url)
    for key, entry in marker.items():
        if (
            isinstance(entry, dict)
            and _normalize_service_url(str(entry.get("base_url") or "")) == want
        ):
            return str(key), entry
    return "", {}


def _try_acquire_credits_lock() -> bool:
    """Guard the marker's read-modify-write; concurrent writers on different
    tenants would otherwise each write back a map missing the other's entry.
    Fail-open like the drain lock: a rare lost update beats a wedged marker."""
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        if _CREDITS_LOCK.exists():
            try:
                if time.time() - _CREDITS_LOCK.stat().st_mtime > _CREDITS_LOCK_STALE_SECONDS:
                    _CREDITS_LOCK.unlink()
            except FileNotFoundError:
                pass
        fd = os.open(str(_CREDITS_LOCK), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return True


def _release_credits_lock() -> None:
    try:
        _CREDITS_LOCK.unlink()
    except Exception:
        pass


def _select_tenant_budget(overview: dict, tenant_id: str) -> dict | None:
    """Return the budget record of OUR tenant from the credits overview, or None.

    Exact match only, on purpose: the display answers "what does the tenant I
    am connected to have left?", and no other number is a valid answer. The
    overview's account-wide ``budget`` aggregates every workspace the user
    owns, and its ``tenants`` list may contain workspaces other than the one
    this session talks to (e.g. your personal workspace while connected to a
    shared tenant someone else owns) — showing either would be wrong, so with
    no exact match the caller shows nothing at all.
    """
    tenants = overview.get("tenants")
    tenants = [t for t in tenants if isinstance(t, dict)] if isinstance(tenants, list) else []
    for t in tenants:
        if str(t.get("tenantId") or "").strip() == tenant_id:
            return {
                "remaining_usd": t.get("remainingUsd"),
                "spent_usd": t.get("spentUsd"),
                "total_usd": t.get("maxBudgetUsd"),
            }
    return None


def refresh_credits(op_label: str = "", *, tenant_id: str = "", timeout: float = 3.0) -> dict:
    """Fetch the cloud credits overview and update this tenant's marker entry.

    Best-effort by contract: any failure returns {} and leaves the existing
    marker untouched — the renderer's staleness TTL handles an aging balance,
    and a fetch problem must never propagate into the calling hook.

    ``tenant_id`` comes from ``load_resolved()`` (the connections/me lookup)
    when the caller has it; otherwise the tenant is recovered from the marker
    entry already bound to this service URL (established by the prompt-time
    refresh). Strictly the CONNECTED tenant's budget or nothing: when the
    tenant cannot be determined, or is not in the overview, no entry is
    written and the segment simply does not render.

    ``op_label`` ("turn" / "remember" / "improve") attributes the spend
    recorded since the previous reading OF THIS TENANT to the operation that
    just ran. Approximate by design — the cloud aggregates spend
    asynchronously and concurrent operations overlap, so the delta reads as
    "~cost", not an invoice. A non-positive delta (aggregation lag, or a
    top-up between readings) refreshes the balance but records no last_op:
    a negative "cost" is meaningless, and the prior last_op is kept so the
    display doesn't flicker away on every idle refresh.
    """
    service_url = _local_api_url()
    if service_url_is_local(service_url):
        return {}
    platform_url = _platform_api_url()
    try:
        marker = read_credits_marker()
        tenant_id = str(tenant_id or "").strip()
        if not tenant_id:
            tenant_id, _ = _credits_entry_for_url(marker, service_url)
        if not tenant_id:
            # Connected tenant unknown (no id from the caller, no prior URL
            # binding): show nothing rather than someone's other workspace or
            # the all-tenants aggregate. Skipped BEFORE the fetch — a doomed
            # lookup is not worth a network call.
            hook_log("credits_refresh_skipped_no_tenant", {"base_url": service_url})
            return {}
        overview = _json_http_request(
            "/api/v1/billing/credits/overview",
            None,
            method="GET",
            timeout=timeout,
            base_url=platform_url,
        )
        budget = _select_tenant_budget(overview or {}, tenant_id)
        if budget is None:
            hook_log(
                "credits_tenant_not_in_overview",
                {"tenant_id": tenant_id, "platform_url": platform_url},
            )
            return {}
        remaining = budget.get("remaining_usd")
        spent = budget.get("spent_usd")
        if remaining is None and spent is None:
            hook_log(
                "credits_fetch_empty",
                {"platform_url": platform_url, "tenant_id": tenant_id},
            )
            return {}
        now_ts = datetime.now(timezone.utc).timestamp()
        entry_key = tenant_id
        entry = {
            "remaining_usd": remaining,
            "spent_usd": spent,
            "total_usd": budget.get("total_usd"),
            # The service URL this tenant was observed under: the renderer and
            # tenantless callers look their entry up by it.
            "base_url": service_url,
            "platform_url": platform_url,
            "tenant_id": tenant_id,
            "checked_at": now_ts,
        }
        acquired = _try_acquire_credits_lock()
        try:
            # Re-read under the lock: another tenant's refresh may have
            # updated the map since the pre-fetch read.
            marker = read_credits_marker()
            prior = marker.get(entry_key)
            prior = prior if isinstance(prior, dict) else {}
            last_op = prior.get("last_op")
            if op_label:
                delta = None
                try:
                    # Prefer the spend counter; fall back to the remaining-
                    # balance drop when the API reports only one of the two.
                    if spent is not None and prior.get("spent_usd") is not None:
                        delta = float(spent) - float(prior["spent_usd"])
                    elif remaining is not None and prior.get("remaining_usd") is not None:
                        delta = float(prior["remaining_usd"]) - float(remaining)
                except (TypeError, ValueError):
                    delta = None
                if delta is not None and delta > 0:
                    last_op = {
                        "label": str(op_label)[:24],
                        "cost_usd": round(delta, 4),
                        "at": now_ts,
                    }
            if isinstance(last_op, dict):
                entry["last_op"] = last_op
            marker[entry_key] = entry
            # Prune long-dead tenants so one-off connections don't accumulate.
            for key in [
                k
                for k, v in marker.items()
                if k != entry_key
                and (
                    not isinstance(v, dict)
                    or now_ts - float(v.get("checked_at", 0) or 0) > _CREDITS_ENTRY_MAX_AGE_SECONDS
                )
            ]:
                marker.pop(key, None)
            _CREDITS_MARKER.parent.mkdir(parents=True, exist_ok=True)
            # Per-pid tmp: a shared staging name let one writer truncate the
            # file another was about to os.replace into place, and the
            # renderer briefly saw a torn marker (the "credits disappear
            # mid-search" flicker).
            tmp = _CREDITS_MARKER.with_name(f"{_CREDITS_MARKER.name}.{os.getpid()}.tmp")
            try:
                tmp.write_text(json.dumps(marker), encoding="utf-8")
                os.replace(tmp, _CREDITS_MARKER)
            finally:
                try:
                    tmp.unlink()
                except FileNotFoundError:
                    pass
        finally:
            if acquired:
                _release_credits_lock()
        return entry
    except Exception as exc:
        # platform_url in the detail: a 404 here once cost a PID-correlation
        # hunt to discover WHICH host was being asked.
        hook_log(
            "credits_fetch_failed",
            {"error": str(exc)[:200], "platform_url": platform_url},
        )
        return {}


# --- Plugin update check (Phase 2) -------------------------------------------
# Background, hourly-guarded check comparing the installed plugin version against the
# version published on the tracked git ref. The network call runs only here (in
# the background idle watcher); the hot path (in-context status via
# render_status_for_host, SessionStart nudge) merely READS the marker this
# writes. Codex tracks the marketplace's git ref (main) rather than a version
# pin, so the nudge is driven by comparing the published .codex-plugin/plugin.json
# version to the installed one. Talks only to raw.githubusercontent over the
# shared certifi TLS context. Opt out with COGNEE_UPDATE_CHECK=off.
_UPDATE_CHECK_FILE = _PLUGIN_DIR / "update-check.json"
_UPDATE_CHECK_INTERVAL_DEFAULT = 3600.0
_UPDATE_DEFAULT_REPO = "topoteretes/cognee-integrations"
_UPDATE_DEFAULT_REF = "main"
_UPDATE_MANIFEST_PATH = "integrations/codex/plugins/cognee/.codex-plugin/plugin.json"


def _update_check_enabled() -> bool:
    return os.environ.get("COGNEE_UPDATE_CHECK", "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _parse_semver(value: str):
    """Parse the numeric X.Y.Z core (ignoring any -pre/+build suffix); None if not X.Y.Z."""
    core = str(value or "").strip().lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = core.split(".")
    if len(parts) != 3:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def _semver_gt(a: str, b: str) -> bool:
    pa, pb = _parse_semver(a), _parse_semver(b)
    return bool(pa and pb and pa > pb)


def _installed_plugin_version() -> str:
    candidates = []
    root = os.environ.get("PLUGIN_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / ".codex-plugin" / "plugin.json")
    candidates.append(Path(__file__).resolve().parent.parent / ".codex-plugin" / "plugin.json")
    for path in candidates:
        try:
            version = str(json.loads(path.read_text(encoding="utf-8")).get("version") or "").strip()
            if version:
                return version
        except Exception:
            continue
    return ""


def _update_source() -> Optional[tuple]:
    """(repo, ref) to read the published version from.

    Codex stores marketplace config in ~/.codex/config.toml (not a JSON we parse
    here) and tracks the ref rather than a version pin, so we read the published
    version from the default repo/ref. The nudge is purely a version comparison,
    so a local checkout only nags when it is behind the published version.
    """
    return _UPDATE_DEFAULT_REPO, _UPDATE_DEFAULT_REF


def _fetch_published_version(repo: str, ref: str, etag: str) -> tuple:
    """GET the raw .codex-plugin/plugin.json and read its version.

    Returns (version, new_etag, error). version is '' on 304/missing/error so the
    caller keeps the previously-known latest.
    """
    url = f"https://raw.githubusercontent.com/{repo}/{ref}/{_UPDATE_MANIFEST_PATH}"
    req = urllib.request.Request(url, method="GET")
    if etag:
        req.add_header("If-None-Match", etag)
    try:
        with urllib.request.urlopen(req, timeout=5.0, context=_https_context()) as resp:
            body = resp.read().decode("utf-8")
            new_etag = resp.headers.get("ETag", "") or etag
            data = json.loads(body)
            version = str(data.get("version") or "") if isinstance(data, dict) else ""
            return version, new_etag, ("" if version else "version_missing")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            return "", etag, ""  # unchanged since last check
        return "", etag, f"http_{exc.code}"
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return "", etag, str(exc)[:120]


def maybe_check_for_update() -> None:
    """Background, ≤hourly update check. Writes the marker. Never raises.

    Call from a background process (the idle watcher) — never a synchronous hook,
    since it may make a network call (bounded to 5s, ≤ once per interval).
    """
    try:
        if not _update_check_enabled():
            return
        marker = _load_json_file(_UPDATE_CHECK_FILE)
        interval = _improve_float_env(
            "COGNEE_UPDATE_CHECK_INTERVAL", _UPDATE_CHECK_INTERVAL_DEFAULT
        )
        now = datetime.now(timezone.utc).timestamp()
        if now - float(marker.get("last_checked_at", 0) or 0) < interval:
            return  # checked recently
        source = _update_source()
        if source is None:
            return
        repo, ref = source
        installed = _installed_plugin_version()
        latest, etag, error = _fetch_published_version(repo, ref, str(marker.get("etag") or ""))
        if not latest:
            latest = str(marker.get("latest_version") or "")  # 304/error: keep prior
        update_available = bool(installed and latest and _semver_gt(latest, installed))
        _write_json_file(
            _UPDATE_CHECK_FILE,
            {
                "last_checked_at": now,
                "installed_version": installed,
                "latest_version": latest,
                "update_available": update_available,
                "etag": etag,
                "source": f"{repo}@{ref}",
                "error": error,
                "notified_version": str(marker.get("notified_version") or ""),
            },
        )
        hook_log(
            "update_check",
            {
                "installed": installed,
                "latest": latest,
                "available": update_available,
                "error": error,
            },
        )
    except Exception as exc:
        hook_log("update_check_failed", {"error": str(exc)[:200]})


def read_update_status() -> dict:
    """Zero-network read of the update marker; {} when disabled/absent/current.

    The marker is a snapshot from the last background check, so it goes stale the
    moment the plugin is updated — it would keep claiming an update is available
    until the next check (≤ an hour later). Guard against that here, at read time:
    the snapshot is only trustworthy while its ``installed_version`` is still the
    version actually running. A mismatch means the update already landed, so the
    nudge is suppressed immediately rather than after the next network check.
    Note this compares against the RUNNING version, not the newest on disk — an
    auto-update that a session has not reloaded yet correctly keeps nudging.
    """
    if not _update_check_enabled():
        return {}
    marker = _load_json_file(_UPDATE_CHECK_FILE)
    if not (
        isinstance(marker, dict)
        and marker.get("update_available")
        and marker.get("installed_version")
        and marker.get("latest_version")
    ):
        return {}
    # An undeterminable running version falls back to trusting the marker, so a
    # missing/unreadable plugin.json degrades to the previous behaviour.
    running = _installed_plugin_version()
    if running and running != marker.get("installed_version"):
        return {}
    return marker


def mark_update_notified(version: str) -> None:
    """Record that the one-time SessionStart nudge for `version` has been shown."""
    try:
        marker = _load_json_file(_UPDATE_CHECK_FILE)
        if not marker:
            return
        marker["notified_version"] = str(version or "")
        _write_json_file(_UPDATE_CHECK_FILE, marker)
    except Exception as exc:
        hook_log("update_notified_write_failed", {"error": str(exc)[:200]})


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
    base_url: str | None = None,
):
    # base_url overrides the resolved service URL for calls that target a
    # different host than the memory data plane (e.g. the cloud platform API's
    # billing routes); the same principal X-Api-Key is attached either way.
    base_url = (base_url or _local_api_url()).rstrip("/")
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
    with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
        body = resp.read().decode("utf-8")
        if not body:
            return None
        return json.loads(body)


def _float_env(name: str, default: float) -> float:
    """Read a float from the environment, falling back to default on absence/parse error."""
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def elapsed_ms(start: float) -> int:
    """Whole milliseconds elapsed since a ``time.monotonic()`` start marker.

    Monotonic-based so it is immune to wall-clock jumps / NTP drift, and rounded to
    an int so the ``elapsed_ms`` fields in hook.log stay compact and easy to query.
    """
    return round((time.monotonic() - start) * 1000)


def wait_for_cognify(
    dataset_id: str,
    *,
    deadline_seconds: float,
    interval_seconds: float = 3.0,
    pipeline: str = "cognify_pipeline",
    request_timeout: float = 10.0,
) -> str:
    """Poll GET /api/v1/datasets/status until the cognify pipeline is terminal or the deadline.

    Returns one of:
      "completed" — DATASET_PROCESSING_COMPLETED (graph queryable; safe to mark written)
      "errored"   — DATASET_PROCESSING_ERRORED (do NOT mark; a later attempt should retry)
      "timeout"   — deadline elapsed while still processing (do NOT mark; retry)
      "unknown"   — cannot poll: no dataset_id, or the status route is absent (older server)

    A background remember returns immediately with a dataset_id; this confirms the
    server-side cognify actually finished instead of fire-and-forgetting, so the bridge
    never holds one synchronous request open past the cloud's request ceiling.
    """
    if not dataset_id:
        return "unknown"
    path = (
        f"/api/v1/datasets/status?dataset={urllib.parse.quote(str(dataset_id))}"
        f"&pipeline={urllib.parse.quote(pipeline)}"
    )
    deadline = time.monotonic() + max(0.0, deadline_seconds)
    while True:
        try:
            result = _json_http_request(path, None, method="GET", timeout=request_timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Older server without the status route — can't confirm, don't loop.
                return "unknown"
            hook_log(
                "cognify_poll_transient",
                {"dataset_id": dataset_id, "error": f"HTTP {exc.code}"},
            )
            result = None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            hook_log("cognify_poll_transient", {"dataset_id": dataset_id, "error": str(exc)[:120]})
            result = None

        status = ""
        if isinstance(result, dict) and result:
            raw = result.get(str(dataset_id))
            if raw is None and len(result) == 1:
                raw = next(iter(result.values()))
            # A multi-pipeline response nests {pipeline: status}; unwrap if needed.
            if isinstance(raw, dict):
                raw = raw.get(pipeline)
            status = str(raw or "").upper()

        if status.endswith("COMPLETED"):
            return "completed"
        if status.endswith("ERRORED"):
            return "errored"

        if time.monotonic() >= deadline:
            return "timeout"
        time.sleep(max(0.1, interval_seconds))  # floor avoids a tight spin if misconfigured to 0


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
    entry = _sanitize_value(entry)
    return _json_http_request(
        "/api/v1/remember/entry",
        {
            "entry": entry,
            "dataset_name": dataset,
            "session_id": session_id,
        },
        timeout=timeout,
    )


def get_session_detail_via_http(session_id: str, *, timeout: float = 8.0) -> dict | None:
    """Fetch the server's view of a session: recent QA and trace tails.

    GET /api/v1/sessions/{id} returns the session row plus the last ~20 QA and
    trace entries. The drain's verify-before-replay pass uses it to check
    whether an ambiguous write (timed out / gateway error after the request
    was sent) actually committed. Returns None on any failure — callers must
    fail open (replay anyway) rather than block the drain on a read.
    """
    if not session_id:
        return None
    try:
        result = _json_http_request(
            f"/api/v1/sessions/{urllib.parse.quote(session_id, safe='')}",
            None,
            method="GET",
            timeout=timeout,
        )
        return result if isinstance(result, dict) else None
    except Exception as exc:
        hook_log("session_detail_error", {"error": str(exc)[:200]})
        return None


def write_outcome_ambiguous(exc: Exception) -> bool:
    """True if a failed /remember/entry write may still have been committed.

    The server has no idempotency on the entry path — every accepted write
    creates and embeds a fresh entry — so replaying a write that actually
    landed duplicates session content and inflates the next improve. Only
    failures where the request provably never reached the application are
    unambiguous:
      - connection refused / DNS failure: nothing was sent;
      - HTTP 503: the endpoint returns it before touching the cache
        ("session cache unavailable"), and a proxy 503 means it never routed.
    Everything else — timeouts, resets, SSL errors mid-exchange, 500/502/504 —
    may have committed server-side, so the buffered copy must be verified
    against the server before replay.
    """
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code != 503
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
    if isinstance(reason, (socket.gaierror, ConnectionRefusedError)):
        return False
    if isinstance(reason, OSError) and getattr(reason, "errno", None) in (
        errno.ECONNREFUSED,
        errno.EHOSTUNREACH,
        errno.ENETUNREACH,
    ):
        return False
    return True


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
    dataset: str = "",
    code_query: dict | None = None,
    timeout: float = 10.0,
) -> list:
    payload = {
        "query": query,
        "session_id": session_id,
        "top_k": top_k,
        "scope": scope,
        "only_context": only_context,
    }
    # Deterministic code-graph lane (cognee >= 1.5.3). Only meaningful when
    # the scope includes "code": the server rejects code_query without it.
    if code_query:
        payload["code_query"] = code_query
    # Always scope to the plugin's dataset. Without it the server resolves EVERY
    # readable dataset and then reconciles against the session's binding, so the
    # graph scope depends on that binding existing: an unbound session with more
    # than one readable dataset is rejected as ambiguous rather than searched.
    # The value must be the dataset the session's entries were written under — a
    # different one is a binding mismatch server-side, a real error worth surfacing.
    if dataset:
        payload["datasets"] = [dataset]
    if search_type:
        payload["search_type"] = search_type
    if context_profile:
        payload["context_profile"] = context_profile
    result = _json_http_request("/api/v1/recall", payload, timeout=timeout)
    return result if isinstance(result, list) else []


def _backend_reachable(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/docs", timeout=timeout, context=_https_context()
        ) as resp:
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
    cache = _load_json_file(_bridge_file(session_id))
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
) -> dict:
    """Submit a document to /api/v1/remember in the BACKGROUND.

    Background avoids holding one synchronous request open for the full cognify,
    which a large graph build can push past the cloud's request ceiling (the POST
    is abandoned mid-flight even though the server finishes). Returns the enqueue
    handle so the caller can poll completion:
      {"ok": True, "dataset_id": <uuid|"">, "pipeline_run_id": <uuid|"">}
    On any HTTP/network error returns {"ok": False, ...} (never raises), so the caller
    skips just this document and keeps syncing the rest; the unmarked digest retries.
    """
    body, boundary = _multipart_body(
        {
            "datasetName": dataset,
            "node_set": node_set,
            "run_in_background": "true",
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
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_https_context()) as resp:
            status_code = resp.status
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        # urlopen raises on non-2xx. Surface it as a graceful failure (not an
        # exception) so the caller skips this one document and keeps syncing the
        # others; the unmarked digest lets a later detached attempt retry.
        # Uniform shape: every failure carries both `status` and `error`.
        return {
            "ok": False,
            "dataset_id": "",
            "pipeline_run_id": "",
            "status": exc.code,
            "error": f"HTTP {exc.code}: {exc.reason}",
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        # A transient network/timeout error must also skip just this document,
        # not propagate to the outer handler and abort the whole sync. status=0
        # signals a network-level (non-HTTP) failure.
        return {
            "ok": False,
            "dataset_id": "",
            "pipeline_run_id": "",
            "status": 0,
            "error": str(exc)[:200],
        }
    result = {"ok": True, "dataset_id": "", "pipeline_run_id": ""}
    try:
        parsed = json.loads(raw) if raw else {}
    except (ValueError, TypeError) as exc:
        # A 2xx with an unparseable body (e.g. a proxy/nginx error page) is NOT a
        # trustworthy success — flag it (with the uniform status/error shape) so the
        # caller retries instead of marking done.
        parsed = {}
        result["parse_error"] = True
        result["status"] = status_code
        result["error"] = f"unparseable 2xx body: {str(exc)[:80]}"
    if isinstance(parsed, dict):
        result["dataset_id"] = str(parsed.get("dataset_id") or "")
        result["pipeline_run_id"] = str(parsed.get("pipeline_run_id") or "")
    return result


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

    # `timeout` is reinterpreted as the overall poll deadline (it used to be the
    # synchronous read timeout). The POST itself is now fast (it only enqueues), so
    # it gets a short submit budget; the wait happens by polling the status route.
    poll_deadline = _float_env("COGNEE_BRIDGE_POLL_DEADLINE", timeout)
    submit_timeout = _float_env("COGNEE_BRIDGE_SUBMIT_TIMEOUT", 30.0)
    poll_interval = _float_env("COGNEE_COGNIFY_POLL_INTERVAL", 3.0)
    status_timeout = _float_env("COGNEE_STATUS_REQUEST_TIMEOUT", 10.0)

    bridge_path = _bridge_file(session_id)
    bridge_cache = _load_json_file(bridge_path)
    state = bridge_cache.get("_state", {}) if isinstance(bridge_cache, dict) else {}
    wrote = False
    overall_start = time.monotonic()
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
            # poll_deadline is an OVERALL budget across all documents, not per-document,
            # so two documents can't compound to 2x the configured wait.
            if time.monotonic() - overall_start >= poll_deadline:
                hook_log("http_bridge_deadline_exceeded", {"dataset": dataset, "kind": kind})
                break
            # Time the POST + wait_for_cognify poll together so http_bridge_poll
            # reports the full latency the caller waited on (submit + confirm).
            doc_start = time.monotonic()
            submitted = _post_remember_document(
                base_url, api_key, dataset, document, node_set, submit_timeout
            )
            if not submitted.get("ok"):
                # Skip this document (digest stays unmarked → retried later) but keep
                # syncing the others; one bad/transient document must not abort the sync.
                # Emit elapsed_ms on the failure path too, so slow-failing submits
                # (e.g. a POST that times out) are still visible in latency logs.
                hook_log(
                    "http_bridge_post_failed",
                    {
                        "dataset": dataset,
                        "kind": kind,
                        "status": submitted.get("status"),
                        "elapsed_ms": elapsed_ms(doc_start),
                    },
                )
                continue
            dataset_id = submitted.get("dataset_id") or ""
            if not dataset_id:
                if submitted.get("parse_error"):
                    # 2xx but an unparseable body (e.g. a proxy/nginx error page): we
                    # can't trust the write landed, so leave the digest unmarked to retry.
                    hook_log(
                        "http_bridge_parse_error",
                        {"dataset": dataset, "kind": kind, "elapsed_ms": elapsed_ms(doc_start)},
                    )
                    continue
                # Valid response with no handle to poll. Mark written so we don't
                # resubmit and duplicate the cognify on every future sync.
                state[state_key] = digest
                wrote = True
                hook_log(
                    "http_bridge_no_dataset_id",
                    {"dataset": dataset, "kind": kind, "elapsed_ms": elapsed_ms(doc_start)},
                )
                continue
            remaining = poll_deadline - (time.monotonic() - overall_start)
            if remaining <= 0:
                # The POST consumed the remaining budget — don't start a poll. Time it
                # like the sibling post-POST logs so a submit slow enough to blow the
                # whole bridge budget stays visible, not just silently deadline-broken.
                # The digest stays unmarked ON PURPOSE even though the submit was
                # enqueued: marking an unconfirmed write would silently lose the
                # document if that cognify errors. The detached retry's re-submit can
                # therefore duplicate a cognify of identical content — the same
                # bounded, accepted cost as the errored/timeout poll outcomes below
                # (retry-over-loss, never loss-over-duplicate).
                hook_log(
                    "http_bridge_deadline_exceeded",
                    {"dataset": dataset, "kind": kind, "elapsed_ms": elapsed_ms(doc_start)},
                )
                break
            outcome = wait_for_cognify(
                dataset_id,
                deadline_seconds=remaining,
                interval_seconds=poll_interval,
                request_timeout=status_timeout,
            )
            # Only mark written once the graph is confirmed queryable (completed) or we
            # genuinely cannot poll (older server). errored/timeout stay unmarked so the
            # detached retry (COGNEE_SYNC_RETRIES) re-submits.
            if outcome in ("completed", "unknown"):
                state[state_key] = digest
                wrote = True
            hook_log(
                "http_bridge_poll",
                {
                    "dataset": dataset,
                    "kind": kind,
                    "outcome": outcome,
                    "dataset_id": dataset_id,
                    "elapsed_ms": elapsed_ms(doc_start),
                },
            )
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


# Buffer-internal marker on a pending entry whose original send may have
# committed server-side (see write_outcome_ambiguous). Stripped before replay;
# never sent, never read outside this module.
_AMBIGUOUS_KEY = "_replay_ambiguous"


def append_warmup_entry(
    dataset: str, session_id: str, entry: dict, *, ambiguous: bool = False
) -> None:
    """Buffer a typed QA/trace entry while the server is still warming.

    Per-turn stores go to the server session cache via /remember/entry; before
    the server serves, those writes would be lost — and improve() bridges only
    what the server cache holds. Buffered entries are replayed in order by
    ``drain_warmup_entries`` once the server is ready.

    ``ambiguous=True`` marks an entry whose original send may have committed
    (a timeout or gateway error after the request went out). The server has no
    idempotency on /remember/entry, so the drain verifies such entries against
    the server's session detail before replaying — a blind replay of a
    committed write stores, embeds, and improve()-processes the text twice.
    """
    if not dataset or not session_id or not isinstance(entry, dict):
        return
    entry = _sanitize_value(entry)
    if ambiguous:
        entry = dict(entry)
        entry[_AMBIGUOUS_KEY] = True
    with _buffer_lock():
        cache = _load_json_file(_bridge_file(session_id))
        key = _bridge_cache_key(dataset, session_id)
        session_cache = cache.setdefault(key, {"qa": [], "trace": []})
        session_cache.setdefault("pending_entries", []).append(entry)
        _write_json_file(_bridge_file(session_id), cache)


def _entry_fingerprint(entry: dict) -> tuple | None:
    """Content identity of a QA/trace entry, as the server would echo it back.

    Built from the fields the server stores verbatim and returns through
    GET /api/v1/sessions/{id} (server-generated fields — ids, time,
    session_feedback — are deliberately excluded). None for entry types the
    session detail does not expose (feedback, skill_run): those cannot be
    verified and are replayed unconditionally.
    """
    try:
        etype = str(entry.get("type") or "")
        if etype == "trace":
            return (
                "trace",
                str(entry.get("origin_function") or ""),
                str(entry.get("status") or ""),
                json.dumps(entry.get("method_params") or {}, sort_keys=True, default=str),
                json.dumps(entry.get("method_return_value"), sort_keys=True, default=str),
                str(entry.get("error_message") or ""),
            )
        if etype == "qa":
            return (
                "qa",
                str(entry.get("question") or ""),
                str(entry.get("answer") or ""),
                str(entry.get("context") or ""),
            )
    except Exception:
        return None
    return None


def _server_session_fingerprints(detail: dict) -> set:
    """Fingerprints of every QA/trace entry in a session-detail response."""
    prints: set = set()
    for row in detail.get("traces") or []:
        if isinstance(row, dict):
            fp = _entry_fingerprint({**row, "type": "trace"})
            if fp:
                prints.add(fp)
    for row in detail.get("qas") or []:
        if isinstance(row, dict):
            fp = _entry_fingerprint({**row, "type": "qa"})
            if fp:
                prints.add(fp)
    return prints


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
        # Fail open: lock bookkeeping must never be why a buffer can't drain.
        # The cost is real — /remember/entry has NO server-side idempotency,
        # so a concurrent double replay stores duplicates — but it needs two
        # drains inside the same window on a broken lock, and losing the
        # buffer forever is worse.
        hook_log("drain_lock_error", {"error": str(exc)[:200]})
        return True


def _release_drain_lock() -> None:
    try:
        _DRAIN_LOCK.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        hook_log("drain_lock_release_failed", {"error": str(exc)[:200]})


# Drain hardening (#298): a session whose replay keeps failing with an HTTP
# status (a poisoned entry, broken auth, a server-side 503 loop) must not be
# retried on every trigger forever — one real incident ground a SessionEnd
# worker against a 503-ing session for 6.5 hours. Consecutive HTTP-status
# failures back the session's drain off exponentially (doubling from the base,
# capped). Network errors deliberately do NOT count: the server_usable /
# ready-marker gates already cover a down server, and backing off on them
# would delay recovery after a restart.
_DRAIN_BACKOFF_BASE_SECONDS = 60.0
_DRAIN_BACKOFF_CAP_SECONDS = 3600.0


def _drain_budget_default() -> float:
    try:
        return float(os.environ.get("COGNEE_DRAIN_BUDGET", "") or 20.0)
    except (TypeError, ValueError):
        return 20.0


def drain_warmup_entries(
    dataset: str, session_id: str, budget_seconds: float | None = None
) -> tuple:
    """Replay warmup-buffered entries into the server session cache, in order.

    Returns ``(drained, remaining)``. Stops at the first replay failure so the
    unreplayed tail stays buffered (order preserved). Guarded by a single-drainer
    lock, and the buffer trim is computed against a FRESH re-read of the file so
    entries appended while the replay was in flight are never lost — the replay
    is N sequential HTTP calls, a wide window for concurrent async hooks.

    Time-boxed (#298): the whole replay stops once ``budget_seconds`` is spent
    (default ``COGNEE_DRAIN_BUDGET``, 20s), and each entry's socket timeout is
    clamped to the remaining budget so one hung call cannot eat it all. A
    session whose replay failed with an HTTP status recently is skipped
    entirely until its backoff window passes (see ``_DRAIN_BACKOFF_*``).

    Verify-before-replay: entries buffered from an ambiguous send (marked by
    ``append_warmup_entry(..., ambiguous=True)``) may already exist server-side
    — /remember/entry has no idempotency, so replaying one blind would store
    and embed the content twice and feed the duplicate to the next improve.
    When any are pending, one GET of the session detail supplies the server's
    recent entries; an ambiguous entry whose content is already there is
    consumed without being re-sent (logged in ``warmup_drained`` as
    ``deduped``). If the detail read fails, everything replays as before: a
    rare duplicate beats a lost turn.
    """
    if not dataset or not session_id:
        return 0, 0
    path = _bridge_file(session_id)
    key = _bridge_cache_key(dataset, session_id)
    state = _load_json_file(path).get(key) or {}
    snapshot = list(state.get("pending_entries") or [])
    if not snapshot:
        return 0, 0
    fail_count = int(state.get("drain_fail_count") or 0)
    if fail_count > 0:
        wait = min(
            _DRAIN_BACKOFF_CAP_SECONDS,
            _DRAIN_BACKOFF_BASE_SECONDS * (2 ** (fail_count - 1)),
        )
        elapsed = time.time() - float(state.get("drain_fail_at") or 0)
        if elapsed < wait:
            hook_log(
                "warmup_drain_backoff",
                {
                    "session": session_id,
                    "fail_count": fail_count,
                    "retry_in": round(wait - elapsed, 1),
                    "pending": len(snapshot),
                },
            )
            return 0, len(snapshot)
    if not _try_acquire_drain_lock():
        hook_log("warmup_drain_skipped_locked", {"session": session_id, "pending": len(snapshot)})
        return 0, len(snapshot)
    try:
        if budget_seconds is None:
            budget_seconds = _drain_budget_default()
        deadline = time.monotonic() + max(0.0, float(budget_seconds))
        # One session-detail read serves the whole drain, and only when an
        # ambiguous entry is actually pending. A failed read degrades to the
        # pre-verify behavior (replay everything), never to a blocked drain.
        server_prints: set = set()
        verified = False
        if any(isinstance(e, dict) and e.get(_AMBIGUOUS_KEY) for e in snapshot):
            budget_left = deadline - time.monotonic()
            detail = get_session_detail_via_http(
                session_id, timeout=min(8.0, max(1.0, budget_left))
            )
            if detail is not None:
                server_prints = _server_session_fingerprints(detail)
                verified = True
            else:
                hook_log("warmup_verify_unavailable", {"session": session_id})
        drained = 0
        deduped = 0
        http_failure = False
        for entry in snapshot:
            budget_left = deadline - time.monotonic()
            if budget_left <= 0:
                hook_log(
                    "warmup_drain_budget_exceeded",
                    {
                        "session": session_id,
                        "drained": drained,
                        "left": len(snapshot) - drained - deduped,
                    },
                )
                break
            send_entry = entry
            ambiguous = False
            if isinstance(entry, dict) and _AMBIGUOUS_KEY in entry:
                send_entry = {k: v for k, v in entry.items() if k != _AMBIGUOUS_KEY}
                ambiguous = True
            if ambiguous and verified and _entry_fingerprint(send_entry) in server_prints:
                # The original send committed after all — consume the buffered
                # copy without re-sending it.
                deduped += 1
                continue
            try:
                remember_entry_via_http(
                    dataset,
                    session_id,
                    send_entry,
                    timeout=min(30.0, max(1.0, budget_left)),
                )
                drained += 1
            except urllib.error.HTTPError as exc:
                http_failure = True
                hook_log(
                    "warmup_drain_error",
                    {"error": str(exc)[:200], "drained": drained, "status": exc.code},
                )
                break
            except Exception as exc:
                hook_log("warmup_drain_error", {"error": str(exc)[:200], "drained": drained})
                break
        # Both sent and dedup-consumed entries leave the buffer; they form a
        # contiguous head prefix because the loop only ever breaks.
        drained += deduped
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
                {
                    "session": session_id,
                    "count": drained,
                    "deduped": deduped,
                    "left": remaining,
                },
            )
        # Backoff bookkeeping. An HTTP-status failure arms (or re-arms) the
        # backoff; any progress first resets it — the entry now at the head is
        # a different one, so its failure streak starts over. Non-HTTP errors
        # leave the state untouched: they say nothing about this session.
        if http_failure or (drained and fail_count):
            try:
                with _buffer_lock():
                    cache = _load_json_file(path)
                    session_cache = cache.get(key) or {}
                    if http_failure:
                        session_cache["drain_fail_count"] = (
                            1 if drained else int(session_cache.get("drain_fail_count") or 0) + 1
                        )
                        session_cache["drain_fail_at"] = time.time()
                    else:
                        session_cache.pop("drain_fail_count", None)
                        session_cache.pop("drain_fail_at", None)
                    cache[key] = session_cache
                    _write_json_file(path, cache)
            except Exception as exc:
                hook_log("drain_backoff_write_failed", {"error": str(exc)[:200]})
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

    outcome = {"ok": True, "result": result if isinstance(result, dict) else {}}
    # Best-effort observability: the submit already succeeded, so a poll that
    # times out or errors must never turn that into a failure. Reporting the
    # pipeline states is what lets a caller (and the tests) distinguish "the
    # bridge was accepted" from "the graph actually finished building" — without
    # it, improve returns ok while the graph is still empty, which reads as a
    # silent data-loss bug from the outside.
    #
    # Parity with claude-code, which gained this in the background-remember
    # refactor; the rest of that work was ported here but the improve path was
    # missed, so codex reported no cognify_status at all.
    poll_deadline = _float_env("COGNEE_IMPROVE_POLL_DEADLINE", 600.0)
    dataset_id = ""
    if isinstance(result, dict):
        dataset_id = str(result.get("dataset_id") or "")
    if dataset_id and poll_deadline > 0:
        half = poll_deadline / 2
        outcome["cognify_status"] = wait_for_cognify(dataset_id, deadline_seconds=half)
        outcome["memify_status"] = wait_for_cognify(
            dataset_id, deadline_seconds=half, pipeline="memify_pipeline"
        )
    return outcome


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
                    with urllib.request.urlopen(
                        req, timeout=15.0, context=_https_context()
                    ) as resp:
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

    Serialized per session by ``improve_session_lock``. The guard lives here
    rather than at the three call sites (idle watcher, store hook, SessionEnd
    sync) so every path is covered by construction and a future fourth caller
    cannot reintroduce the double-submit.
    """
    with improve_session_lock(session_id, "run_session_improve") as claimed:
        if not claimed:
            # Another process is already bridging this exact session. Report
            # not-synced so the caller's own retry/reporting path is unchanged;
            # the work itself is in flight, not dropped.
            return False
        return _run_session_improve_locked(dataset, session_id)


def _run_session_improve_locked(dataset: str, session_id: str) -> bool:
    """Body of run_session_improve; assumes the per-session claim is held."""
    base_url = _local_api_url()
    if not _backend_reachable(base_url):
        return False
    # Detached/idle context: nothing user-visible waits on this, so the drain
    # gets a far larger budget than the per-prompt hook's default.
    final_budget = _improve_float_env("COGNEE_DRAIN_BUDGET_FINAL", 120.0)
    _, remaining = drain_warmup_entries(dataset, session_id, budget_seconds=final_budget)
    if remaining:
        # One bounded retry after a short pause: the tail usually failed on a
        # momentary blip, and improve reads only what reached the server cache.
        time.sleep(_DRAIN_RETRY_PAUSE_SECONDS)
        _, remaining = drain_warmup_entries(dataset, session_id, budget_seconds=final_budget)
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
    if outcome.get("ok"):
        # Status-line credits: attribute the spend recorded since the previous
        # reading to this improve. Approximate on purpose — the submit is
        # run_in_background, so part of this run's cognify cost lands in later
        # unlabeled refreshes. refresh_credits never raises and no-ops locally.
        refresh_credits("improve")
    if remaining:
        # Buffered entries never reached the server cache, so the improve above
        # persisted an incomplete session. Partial persist beats none (hence the
        # improve still ran), but report not-synced so the caller's retry loop
        # re-drives the whole drain+improve — the drained head is already
        # trimmed from the buffer, so the re-run replays only the tail.
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


# ---------------------------------------------------------------------------
# State sweep — the per-session files nothing else ever deletes.
# ---------------------------------------------------------------------------
#
# Every launch writes one file into several directories (a launch record, a
# connection marker, an LLM-key verdict, last-recall counts, a bridge cache, a
# pending-prompt buffer, maybe an improve lock). Nothing removed them, so a
# machine in daily use accumulated ~1,200 of them in two months. Each is
# useless once its session is over; the sweep runs at SessionStart and removes
# the ones whose session is provably gone. The rules are deliberately lazy —
# days, not minutes — because the only cost of a stale file is clutter, while
# a premature delete can lose a final sync. Same ownership rule as everywhere
# else in the state dir: this touches only THIS plugin's subdirectory, plus the
# one shared marker this plugin itself writes.

#: Status markers (conn-state, llm-state, recall) and the per-session bridge
#: cache / pending-prompt buffer: gone after a week without a write. A live
#: session rewrites its markers on every hook, so age alone is a safe signal.
_SWEEP_SESSION_FILE_MAX_AGE_SECONDS = 7 * 24 * 3600
#: Launch records: removed one day after their host pid is dead — the
#: exit-watcher's final sync reads the record right after the host exits, and
#: retries may follow — or after 30 days regardless (a record with no pid is
#: treated as alive, so this is the only bound for those).
_SWEEP_LAUNCH_RECORD_DEAD_GRACE_SECONDS = 24 * 3600
_SWEEP_LAUNCH_RECORD_MAX_AGE_SECONDS = 30 * 24 * 3600
#: Logs the sweep rotates when over the cap. Most are also rotated by their own
#: writer; this catches files that predate the cap and logs only ever written
#: by a child process.
_SWEEP_LOG_FILES = (
    "hook.log",
    "bootstrap.log",
    "watcher.log",
    "exit-watcher.log",
    "subprocess.log",
    "recall-audit.log",
    "activity.log",
)
#: Directories older plugin versions created here and nothing reads any more.
_SWEEP_LEGACY_DIRS = ("statusline",)


def _sweep_remove(path: Path, counts: dict, key: str) -> None:
    try:
        path.unlink()
        counts[key] = counts.get(key, 0) + 1
    except FileNotFoundError:
        pass  # a concurrent SessionStart swept it first
    except OSError as exc:
        counts.setdefault("errors", []).append(f"{path.name}: {str(exc)[:80]}")


def _sweep_dir_by_age(directory: Path, max_age: float, now: float, counts: dict, key: str) -> None:
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > max_age:
            _sweep_remove(path, counts, key)


def _sweep_launch_records(now: float, counts: dict) -> None:
    try:
        entries = list(_SESSIONS_MAP_DIR.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age > _SWEEP_LAUNCH_RECORD_MAX_AGE_SECONDS:
            _sweep_remove(path, counts, "launch_records")
            continue
        if age <= _SWEEP_LAUNCH_RECORD_DEAD_GRACE_SECONDS:
            continue
        rec = _load_json_file(path)
        try:
            pid = int((rec or {}).get("host_pid") or 0)
        except (TypeError, ValueError):
            pid = 0
        if pid > 0 and not _proc.pid_alive(pid):
            _sweep_remove(path, counts, "launch_records")


def _sweep_improve_locks(now: float, counts: dict) -> None:
    """Dead-pid or over-age locks. ``improve_session_lock`` clears such a lock
    only when the *same* session locks again, which for an ended session is
    never — so a crash left a lock file behind for good."""
    try:
        entries = list(_IMPROVE_LOCK_DIR.glob("*.lock"))
    except OSError:
        return
    for path in entries:
        stale = False
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
            pid = int(current.get("pid", 0) or 0)
            created_at = float(current.get("created_at", 0) or 0)
            stale = not (pid > 0 and _proc.pid_alive(pid)) or now - created_at > SYNC_LOCK_STALE_SECONDS
        except Exception:
            stale = True  # unreadable lock: nothing can release it either
        if stale:
            _sweep_remove(path, counts, "improve_locks")


def _sweep_expired_improve_marker(now: float, counts: dict) -> None:
    """The shared improve-unsupported marker outlives its TTL as a file; drop it
    once expired so it stops looking like live state. Shared root, but this
    plugin is one of its writers, so removing an expired one is in bounds."""
    data = _load_json_file(_IMPROVE_UNSUPPORTED_MARKER)
    if not isinstance(data, dict) or not data:
        return
    try:
        marked_at = float(data.get("marked_at", 0) or 0)
    except (TypeError, ValueError):
        marked_at = 0.0
    if now - marked_at >= _IMPROVE_UNSUPPORTED_TTL_SECONDS:
        _sweep_remove(_IMPROVE_UNSUPPORTED_MARKER, counts, "expired_markers")


def sweep_stale_state(now: Optional[float] = None) -> dict:
    """Remove this plugin's dead per-session files and legacy leftovers.

    Returns a count per category (only non-zero ones, plus ``errors`` when
    something could not be removed). Never raises: a sweep that fails must
    never cost a SessionStart. Logs one ``state_sweep`` event when it did
    anything.
    """
    counts: dict = {}
    now = datetime.now(timezone.utc).timestamp() if now is None else float(now)
    try:
        for directory, key in (
            (_CONN_STATE_DIR, "conn_state"),
            (_LLM_STATE_DIR, "llm_state"),
            (_PLUGIN_DIR / "recall", "recall"),
            (_BRIDGE_DIR, "bridge"),
            (_PENDING_DIR, "pending"),
        ):
            _sweep_dir_by_age(directory, _SWEEP_SESSION_FILE_MAX_AGE_SECONDS, now, counts, key)
        _sweep_launch_records(now, counts)
        _sweep_improve_locks(now, counts)
        _sweep_expired_improve_marker(now, counts)
        for name in _SWEEP_LEGACY_DIRS:
            legacy = _PLUGIN_DIR / name
            if legacy.is_dir():
                try:
                    shutil.rmtree(legacy)
                    counts["legacy_dirs"] = counts.get("legacy_dirs", 0) + 1
                except OSError as exc:
                    counts.setdefault("errors", []).append(f"{name}/: {str(exc)[:80]}")
        for name in _SWEEP_LOG_FILES:
            if _rotate_log_if_oversized(_PLUGIN_DIR / name):
                counts["logs_rotated"] = counts.get("logs_rotated", 0) + 1
    except Exception as exc:  # pragma: no cover - defensive
        counts.setdefault("errors", []).append(str(exc)[:120])
    if counts:
        hook_log("state_sweep", counts)
    return counts
