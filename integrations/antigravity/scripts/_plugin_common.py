"""Shared helpers across plugin hook scripts.

Kept deliberately small: user resolution, runtime-state read, a
single log-to-disk helper. Hook scripts shouldn't grow heavy because
they run on every user prompt / tool call.
"""

from __future__ import annotations

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
from _dataset_access import dataset_id as parse_dataset_id
from _dataset_access import recall_fields, write_fields
from _env_file import load_env_file
from _logfiles import append_line as _append_log_line
from _logfiles import rotate_if_oversized as _rotate_log_if_oversized
from _recall_http import DOWN, SLOW, UNKNOWN, classify_transport_exception
from event_names import event_fields

# One-time config: ~/.cognee/.env acts like shell exports (setdefault — a real
# export still wins). Loaded before any env read below or in importers.
load_env_file()

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "antigravity"
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
# One state file per session (see record_improve_success): when the last
# successful improve ran and how many turns the session had at that point.
# Every improve trigger records it; the idle and auto triggers consult it so
# the cooldown survives the watcher's exit-after-bridge respawn cycle.
_IMPROVE_STATE_DIR = _PLUGIN_DIR / "improve-state"
# Per-agent-session buffer dirs. Each agent session (one host terminal)
# owns its own file under these dirs, so two concurrent agents never
# read-modify-write the same file — no locks needed, no lost-update races.
_BRIDGE_DIR = _PLUGIN_DIR / "bridge"
_PENDING_DIR = _PLUGIN_DIR / "pending"
_SUBPROCESS_LOG = _PLUGIN_DIR / "subprocess.log"
# Single-principal model: one API key (user-provided COGNEE_API_KEY or one minted
# from the default user) is cached here. Replaces the old per-agent agent_keys.json.
_API_KEY_CACHE = _SHARED_PLUGIN_ROOT / "api_key.json"
# Plugin identity (server-side agent sub-user). Servers that expose
# POST /api/v1/integrations/plugins/{key}/provision mint a dedicated agent
# sub-user + API key per plugin, so cognee can attribute traffic to this
# plugin instead of the shared principal. The key is per-plugin state (the
# Claude Code and Codex plugins have their own), hence _PLUGIN_DIR, not the
# shared root. NOTE: "antigravity" must be registered in the server's plugin
# registry (cognee/modules/integrations/plugins.py) for provisioning to work;
# until then the provision route answers 404 and identity mode ``auto`` stays
# on the principal.
PLUGIN_KEY = "antigravity"
CONNECTION_TYPE = "antigravity"
_AGENT_KEY_CACHE = _PLUGIN_DIR / "agent_key.json"
# Shared agent memory: the role every provisioned plugin agent of a user joins
# so all of them read/write the user's datasets (see ensure_shared_memory).
# The marker records the tenant/role wiring, the canonical dataset ids this
# plugin resolved, and which datasets the role was already granted, so a
# session start / watcher refresh only issues the calls that are still missing.
AGENT_ROLE_NAME = "cognee-agent"
_SHARED_MEMORY_MARKER = _PLUGIN_DIR / "shared_memory.json"
# Host-session-id -> generated Cognee session-id map. The host (Antigravity)
# session id is used ONLY as a local correlation key so every hook process of a
# single launch resolves the SAME Cognee session id; it is never sent to Cognee
# as an identity. A genuinely new launch gets a new host id -> new Cognee session;
# a `resume` reuses the host id -> continues the same Cognee session.
_SESSIONS_MAP_DIR = _PLUGIN_DIR / "sessions"

# Save-kinds tracked per turn. Keep this tuple in sync with bump_save_counter callers.
SAVE_KINDS = ("prompt", "trace", "answer")
# A write the server never received is not a save. When a store hook diverts a
# trace/answer to the warmup buffer (server down, or a retryable send failure)
# it is counted under the kind's buffered twin so the recall header can show it
# as buffered instead of folding it into the saved count — through a 2.5-week
# outage every prompt read "saved last turn 1 prompt / 6 trace / 1 answer"
# while nothing reached the server (SDK-467).
BUFFERED_SAVE_KINDS = ("trace_buffered", "answer_buffered")
ALL_SAVE_KINDS = SAVE_KINDS + BUFFERED_SAVE_KINDS

# Cap the per-line log size so a noisy tool output doesn't bloat the file.
_LOG_LINE_CAP = 600

# Default auto-improve threshold (tool calls + stops). Env override.
AUTO_IMPROVE_EVERY_DEFAULT = 150
SYNC_LOCK_STALE_SECONDS = 15 * 60
_DEFAULT_LOCAL_SERVICE_URL = "http://localhost:8011"

# --- Self-managed cognee runtime (shared with existing Cognee plugins) -------
# Deliberately NOT namespaced under ~/.cognee-plugin/antigravity: the venv, the
# local cognee server, and the data store are shared with existing plugins so
# cognee is installed once and a single server serves every host. Only per-plugin
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

    The host (Antigravity) session id maps 1:1 to the conversation, so embedding it
    makes the Cognee session id deterministic per conversation and self-describing
    in the Cognee dashboard. Falls back to ``{agent}_{dirname}_{token}`` only when
    no host session id is available.
    """
    agent = (
        _sanitize_session_key(os.environ.get("COGNEE_SESSION_PREFIX", "") or "antigravity")
        or "antigravity"
    )
    host = _sanitize_session_key(host_key)
    if host:
        return f"{agent}_{host}"
    cwd = cwd or os.environ.get("AGY_CWD") or os.getcwd()
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
    """Preserve session metadata while renewing ownership after the host exits."""
    if not host_key or not isinstance(rec, dict):
        return
    updates = {}
    if dataset and not rec.get("dataset"):
        updates["dataset"] = dataset
    if cwd and not rec.get("cwd"):
        updates["cwd"] = cwd
    try:
        previous_pid = int(rec.get("host_pid") or 0)
    except (TypeError, ValueError):
        previous_pid = 0
    if host_pid and (not previous_pid or not _proc.pid_alive(previous_pid)):
        updates["host_pid"] = int(host_pid)
    if not updates:
        return
    merged = dict(_read_map_record(host_key) or rec)
    for key, value in updates.items():
        if key == "host_pid" and merged.get(key) == rec.get(key):
            merged[key] = value
        else:
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


def resolve_active_dataset_ids(host_key: str = "") -> tuple[str, list[str]]:
    """The launch's dataset as UUIDs: ``(write_id, read_ids)``.

    Under shared agent memory the active dataset is addressed by id, not name:
    a name only ever resolves server-side among the datasets the CALLER owns,
    so an agent asking for ``agent_sessions`` by name would fork its own empty
    copy instead of reaching the canonical (parent-owned) one it was granted.
    ``write_id`` is that canonical dataset; ``read_ids`` is it plus every other
    readable same-named dataset (legacy per-agent copies), so recall spans them
    all. Both empty when the launch runs name-addressed (separated memory,
    older server) — callers then fall back to the name.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if not host_key:
        return "", []
    rec = _read_map_record(host_key)
    write_id = str(rec.get("dataset_id") or "").strip()
    read_ids = [str(x).strip() for x in (rec.get("dataset_ids") or []) if str(x).strip()]
    if write_id and write_id not in read_ids:
        read_ids.insert(0, write_id)
    return write_id, read_ids


def set_launch_dataset_ids(
    host_key: str, write_id: str, read_ids: list[str], *, dataset: str = ""
) -> bool:
    """Record the active dataset's resolved UUIDs on the launch record.

    Called by SessionStart (after the canonical dataset is resolved) and by the
    idle watcher's periodic refresh; the dataset switch writes its own ids
    inside ``switch_launch_record``. Never touches the name/session/conn triple.

    Serialised with the switch on the launch's switch lock, and when ``dataset``
    is given the ids land only while the record still names that dataset: the
    refresh resolves ids over several network calls, and a switch completing
    meanwhile must not end up with the previous dataset's UUIDs under the new
    name. Returns True when the record was written.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if not host_key:
        return False
    from _file_lock import file_lock

    lock_path = _session_map_path(host_key).with_suffix(".switch.lock")
    with file_lock(lock_path, timeout=_float_env("COGNEE_SWITCH_LOCK_TIMEOUT", 5.0)) as held:
        if not held:
            hook_log(
                "launch_dataset_ids_skipped",
                {"host_key": host_key, "reason": "switch_in_progress"},
            )
            return False
        rec = _read_map_record(host_key)
        if not rec:
            return False
        if dataset and str(rec.get("dataset") or "").strip() != str(dataset).strip():
            hook_log(
                "launch_dataset_ids_skipped",
                {"host_key": host_key, "reason": "dataset_switched"},
            )
            return False
        merged = dict(rec)
        merged["dataset_id"] = str(write_id or "").strip()
        merged["dataset_ids"] = [str(x).strip() for x in read_ids if str(x).strip()]
        if merged.get("dataset_id") == rec.get("dataset_id") and merged["dataset_ids"] == (
            rec.get("dataset_ids") or []
        ):
            return False
        _write_map_record(host_key, merged)
        return True


def dataset_id_for(dataset: str, host_key: str = "") -> str:
    """The canonical UUID to WRITE ``dataset`` (a name) under, or "".

    Resolution: the launch record when ``dataset`` is the launch's active
    dataset, else the shared-memory marker's canonical map (names this plugin
    has resolved before — e.g. a retired pre-switch dataset the final sync
    still bridges). Empty means "address by name", the pre-shared behaviour.
    """
    dataset = str(dataset or "").strip()
    if not dataset:
        return ""
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if host_key:
        rec = _read_map_record(host_key)
        if str(rec.get("dataset") or "").strip() == dataset:
            write_id = str(rec.get("dataset_id") or "").strip()
            if write_id:
                return write_id
    # The marker's canonical map only applies while shared memory is live:
    # after an opt-out the plugin writes name-addressed (its own dataset), and
    # a stale id here would send writes to the shared dataset that recall —
    # now by name — no longer reads.
    if not shared_memory_enabled():
        return ""
    marker = load_shared_memory_marker()
    canonical = marker.get("canonical") if marker.get("mode") == "shared" else None
    if isinstance(canonical, dict):
        return str(canonical.get(dataset) or "").strip()
    return ""


def shell_runtime_overrides(service_url: str = "") -> dict:
    """Launch-record state for the shell skills (cognee-search.sh / cognee-remember.sh).

    Those run under the host's shell tool with no hook payload, so they find
    their launch record via ``resolve_host_key_outside_hook``. Returns the
    record's ``session_id`` / ``dataset`` (both empty when unrecorded), the
    dataset's canonical ``dataset_id`` and comma-joined ``dataset_ids``, and
    ``api_key`` — the provisioned plugin-agent key when one is cached, so the
    skills act as the same identity as the hooks (see ``_api_key_with_source``).
    Kept to one call on purpose: the skills embed Python in a ``$( <<'PY' )``
    block that macOS's bash 3.2 mis-parses once it grows past a few lines.
    """
    host_key, _ = resolve_host_key_outside_hook()
    rec = _read_map_record(host_key) if host_key else {}
    write_id, read_ids = resolve_active_dataset_ids(host_key) if host_key else ("", [])
    return {
        "session_id": str(rec.get("session_id") or "").strip(),
        "dataset": str(rec.get("dataset") or "").strip(),
        "dataset_id": write_id,
        "dataset_ids": ",".join(read_ids),
        "api_key": active_agent_key(service_url),
    }


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
    dataset_id: str = "",
    dataset_ids: list[str] | None = None,
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
    from _file_lock import file_lock

    # One switch at a time per launch: the write-then-verify below has no
    # transactional guarantee, so a concurrent switch landing in between would
    # make a successful write look unpersisted (and vice versa).
    lock_path = _session_map_path(host_key).with_suffix(".switch.lock")
    with file_lock(lock_path, timeout=_float_env("COGNEE_SWITCH_LOCK_TIMEOUT", 5.0)) as held:
        if not held:
            raise RuntimeError("Another dataset switch is in progress for this launch")
        return _switch_launch_record_locked(
            host_key,
            session_id=session_id,
            dataset=dataset,
            conn_uuid=conn_uuid,
            dataset_id=dataset_id,
            dataset_ids=dataset_ids,
        )


def _switch_launch_record_locked(
    host_key: str,
    *,
    session_id: str,
    dataset: str,
    conn_uuid: str,
    dataset_id: str,
    dataset_ids: list[str] | None,
) -> dict:
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
            # The old dataset's ids must not survive the switch: an id-addressed
            # write to the new name would otherwise land in the previous dataset.
            "dataset_id": str(dataset_id or "").strip(),
            "dataset_ids": [str(x).strip() for x in (dataset_ids or []) if str(x).strip()],
            "conn_uuid": str(conn_uuid),
            "switched_at": now,
            "touched": touched,
        }
    )
    merged.setdefault("created_at", now)
    _write_map_record(host_key, merged)
    saved = _read_map_record(host_key)
    if any(
        saved.get(key) != merged[key]
        for key in ("session_id", "dataset", "conn_uuid", "dataset_id", "dataset_ids")
    ):
        raise RuntimeError("Dataset switch was not persisted; previous session remains active")
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
    """Use effective permissions. Ownership cannot prove absence of write access.

    ``GET /permissions/principals/{user}/datasets?permission_name=write`` lists
    the caller's DIRECT write grants. A grant held through a role — shared agent
    memory's ``cognee-agent`` role on the parent user's datasets — does not
    appear there, so under a live shared-memory marker the parent's datasets
    count as writable too. Without the permissions route (older server) the
    owner match is the only evidence, and ``writable`` stays None when even that
    cannot be judged. Returns::

        {"datasets": [{"name", "id", "owner_id", "writable": True|None}],
         "readonly": [names], "readonly_ids": [ids], "hidden_readonly": N,
         "filtered": bool}
    """
    raw = _json_http_request("/api/v1/datasets/", method="GET", timeout=timeout)
    items = raw if isinstance(raw, list) else []
    if not user_id:
        me = _json_http_request("/api/v1/users/me", method="GET", timeout=timeout)
        user_id = str(me.get("id") or "") if isinstance(me, dict) else ""
    writable_ids = None
    if user_id:
        try:
            allowed = _json_http_request(
                f"/api/v1/permissions/principals/{urllib.parse.quote(user_id, safe='')}/datasets"
                "?permission_name=write",
                method="GET",
                timeout=timeout,
            )
            if isinstance(allowed, list):
                writable_ids = {str(row.get("id")) for row in allowed}
        except urllib.error.HTTPError as exc:
            if exc.code not in (404, 405):
                raise
    shared_parent = ""
    role_granted: dict = {}
    shared = load_shared_memory_marker()
    if shared_memory_enabled() and shared.get("mode") == "shared":
        shared_parent = str(shared.get("parent_user_id") or "")
        role_granted = shared.get("granted") if isinstance(shared.get("granted"), dict) else {}
    rows = []
    for item in items:
        owner = str(item.get("owner_id") or item.get("ownerId") or "")
        ident = str(item.get("id") or "")
        # Writable through the shared role only once the grant is confirmed —
        # a transient failure leaves a parent-owned dataset ungranted until the
        # next refresh, and it must not be offered as writable meanwhile.
        via_role = (
            bool(shared_parent) and owner == shared_parent and role_granted.get(ident) == "ok"
        )
        if writable_ids is not None:
            writable = ident in writable_ids or via_role
        else:
            writable = True if owner and (owner == user_id or via_role) else None
        rows.append(
            {
                "name": str(item.get("name") or ""),
                "id": ident,
                "owner_id": owner,
                "writable": writable,
            }
        )
    rows.sort(key=lambda row: (row["name"].lower(), row["id"]))
    return {
        "datasets": [row for row in rows if row["writable"] is not False],
        "readonly": [row["name"] for row in rows if row["writable"] is False],
        "readonly_ids": [row["id"] for row in rows if row["writable"] is False],
        "hidden_readonly": sum(row["writable"] is False for row in rows),
        "filtered": writable_ids is not None,
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
        suffix = "_agy"
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
    return _normalize("antigravity-agent")


def load_resolved(session_key: str = "", *, identity: bool = True) -> dict:
    """Resolve local session state, optionally enriching identity over HTTP.

    Prompt recall only needs local fields. Passing ``identity=False`` keeps
    identity probes outside that latency-sensitive path.
    """
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
    # Canonical UUIDs under shared agent memory (empty when name-addressed).
    resolved["dataset_id"], resolved["dataset_ids"] = resolve_active_dataset_ids(active_session_key)
    conn_uuid = resolve_conn_uuid(active_session_key)
    if conn_uuid:
        resolved["agent_session_name"] = conn_uuid

    service_url = _local_api_url().strip()
    if service_url:
        resolved["base_url"] = service_url

    api_key = _api_key().strip()
    if api_key:
        resolved["api_key"] = api_key

    if not identity:
        return resolved

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

    Each agent session owns its own pending and
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
    """Append one structured line to ~/.cognee-plugin/antigravity/hook.log.

    Safe to call silently — never raises. Use for forensic debugging
    of why a hook did (or did not) write something to memory.
    """
    try:
        _HOOK_LOG.parent.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
            **event_fields(event, "hook"),
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
    line to ``~/.cognee-plugin/antigravity/activity.log`` so saves that happen
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

    Antigravity parses stdout for JSON hook results. Some
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


def bump_save_counter(session_id: str, kind: str, *, buffered: bool = False) -> None:
    """Record a save of ``kind`` (one of ``SAVE_KINDS``) for this session.

    ``buffered=True`` records it under ``<kind>_buffered`` instead: the entry
    went to the warmup buffer, not the server, and the recall header must not
    report it as saved. Used to surface per-turn save volume back to the user
    through the next UserPromptSubmit's injected context. Cheap, best-effort
    file IO — never raises.
    """
    if buffered:
        kind = f"{kind}_buffered"
    if not session_id or kind not in ALL_SAVE_KINDS:
        return
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8")) if _SAVE_COUNTER.exists() else {}
        )
    except Exception as exc:
        hook_log("save_counter_read_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]})
        data = {}
    sess = data.get(session_id) or {k: 0 for k in ALL_SAVE_KINDS}
    sess[kind] = int(sess.get(kind, 0)) + 1
    data[session_id] = sess
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _SAVE_COUNTER.write_text(json.dumps(data), encoding="utf-8")
    except Exception as exc:
        hook_log("save_counter_write_failed", {"path": str(_SAVE_COUNTER), "error": str(exc)[:200]})


def read_and_reset_save_counter(session_id: str) -> dict:
    """Return the save-kind counts accumulated since the last reset, then zero them.

    Keyed by ``ALL_SAVE_KINDS``: the persisted kinds plus their buffered twins.
    """
    zero = {k: 0 for k in ALL_SAVE_KINDS}
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
    return {k: int(sess.get(k, 0)) for k in ALL_SAVE_KINDS}


def warmup_backlog() -> dict:
    """Entries still waiting in the warmup buffers, across every session on this machine.

    Returns ``{"pending": n, "oldest_age_seconds": float | None}``.
    Read-only and best-effort: a file that cannot be parsed is skipped and the
    scan never raises — it runs on the keystroke->answer path.

    Every per-session bridge file is scanned, not just the current session's: a
    buffer left behind by an earlier session drains only when that session runs
    again, so its entries can sit for weeks with nothing pointing at them — the
    machine that motivated this held entries from three weeks earlier (SDK-467).
    An entry written before the timestamp existed takes its file's mtime, a
    lower bound on its age.
    """
    result = {"pending": 0, "oldest_age_seconds": None}
    try:
        paths = list(_BRIDGE_DIR.glob("*.json")) if _BRIDGE_DIR.is_dir() else []
    except Exception:
        return result
    now = time.time()
    oldest = None
    for path in paths:
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
            fallback_ts = path.stat().st_mtime
        except Exception:
            continue
        if not isinstance(cache, dict):
            continue
        for state in cache.values():
            if not isinstance(state, dict):
                continue
            for entry in state.get("pending_entries") or []:
                result["pending"] += 1
                stamp = entry.get(_BUFFERED_AT_KEY) if isinstance(entry, dict) else None
                try:
                    buffered_at = float(stamp) if stamp else fallback_ts
                except (TypeError, ValueError):
                    buffered_at = fallback_ts
                age = max(0.0, now - buffered_at)
                if oldest is None or age > oldest:
                    oldest = age
    result["oldest_age_seconds"] = oldest
    return result


def format_age(seconds: float) -> str:
    """``45s`` / ``12m`` / ``3h`` / ``20d`` — coarse, for a one-line header."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def buffered_saves_segments(saves: dict, backlog: dict | None = None) -> list:
    """Header segments that make a buffering outage visible; empty when healthy.

    ``saves`` is a ``read_and_reset_save_counter`` result and ``backlog`` a
    ``warmup_backlog`` result. The recall headers append these after
    ``saved last turn …`` (Claude Code joins with ``; ``, Codex with `` · ``)::

        buffered last turn 6 trace / 1 answer (not saved yet)
        7 awaiting replay, oldest 20d

    Nothing is added when nothing was buffered and nothing awaits replay, so
    the healthy header reads exactly as before.
    """
    segments: list = []
    trace_buffered = int(saves.get("trace_buffered", 0) or 0)
    answer_buffered = int(saves.get("answer_buffered", 0) or 0)
    if trace_buffered or answer_buffered:
        segments.append(
            f"buffered last turn {trace_buffered} trace / {answer_buffered} answer (not saved yet)"
        )
    backlog = backlog or {}
    pending = int(backlog.get("pending", 0) or 0)
    if pending > 0:
        segment = f"{pending} awaiting replay"
        oldest = backlog.get("oldest_age_seconds")
        if oldest is not None:
            segment += f", oldest {format_age(oldest)}"
        segments.append(segment)
    return segments


def saves_segment(saves: dict) -> str:
    """``saved last turn 1 prompt / 3 trace / 1 answer`` — persisted writes only."""
    return (
        "saved last turn "
        f"{saves.get('prompt', 0)} prompt / {saves.get('trace', 0)} trace / "
        f"{saves.get('answer', 0)} answer"
    )


# Probe verdicts that settle the server's recorded state. ``slow`` and ``unknown``
# are not verdicts: they leave the recorded state untouched.
DEFINITIVE_FAILURE_STATES = ("auth_failed", "unreachable", "server_error")

_FAILURE_LABELS = {
    "unreachable": "server unreachable",
    "server_error": "server error",
    "auth_failed": "auth failed",
    "not_responding": "server not responding",
}


def describe_connection_failure(state: str) -> str:
    """A recorded connection state as the header names it, e.g. ``server unreachable``."""
    state = str(state or "unknown")
    return _FAILURE_LABELS.get(state, f"server {state.replace('_', ' ')}")


def outage_header(state: str, saves: dict, backlog: dict | None, joiner: str) -> str:
    """The one-line header for a prompt whose recall was skipped: the server is known bad.

    An empty recall used to be the only sign of an outage. The lookup hook returned
    nothing, so no header was shown, and because the save counter was only read on
    a successful recall, the first header after recovery reported weeks of buffered
    writes as one turn's saves (SDK-467). The buffered writes and the replay backlog
    are the whole story here, so this names the outage and reports them and nothing
    else. ``joiner`` is the host's segment separator (``"; "`` or ``" · "``)::

        Cognee memory: recall skipped (server unreachable); saved last turn 1 prompt
        / 0 trace / 0 answer; buffered last turn 6 trace / 1 answer (not saved yet);
        7 awaiting replay, oldest 20d
    """
    parts = [f"recall skipped ({describe_connection_failure(state)})", saves_segment(saves)]
    parts.extend(buffered_saves_segments(saves, backlog))
    return "Cognee memory: " + joiner.join(parts)


def _pending_keys(session_id: str, turn_id: str = "") -> tuple[str, str]:
    # Scope by the host-provided session key (COGNEE_SESSION_KEY, unique per
    # host session) rather than the cwd-derived cognee session_id, so
    # two concurrent agents in the same project don't collide on one pending
    # slot and scramble each other's prompts. Falls back to session_id.
    scope = get_session_key() or session_id
    session_key = f"{scope}:"
    turn_key = f"{scope}:{turn_id}" if turn_id else session_key
    return turn_key, session_key


def remember_pending_prompt(
    session_id: str, prompt: str, *, turn_id: str = "", context: str = ""
) -> None:
    """Store the current prompt until Antigravity Stop provides the assistant answer."""
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
    """Return and remove the prompt saved for this Antigravity turn."""
    if not session_id:
        return {"prompt": "", "context": ""}
    pending_path = _pending_file(session_id)
    data = _load_json_file(pending_path)
    turn_key, session_key = _pending_keys(session_id, turn_id)
    entry = data.pop(turn_key, None) or data.get(session_key) or {}
    data.pop(session_key, None)
    if data:
        _write_json_file(pending_path, data)
    else:
        # Last entry consumed: remove the file rather than write ``{}`` back.
        # Writing the emptied dict left one 2-byte husk per session, forever
        # (80 of 88 files in one pending/ dir were husks — SDK-469).
        try:
            pending_path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            hook_log("pending_unlink_failed", {"path": str(pending_path), "error": str(exc)[:200]})
    if not isinstance(entry, dict):
        return {"prompt": "", "context": ""}
    return {
        "prompt": str(entry.get("prompt") or ""),
        "context": str(entry.get("context") or ""),
    }


def _auto_improve_threshold() -> int:
    """Stored entries between automatic improves; ``0`` disables the trigger."""
    raw = os.environ.get("COGNEE_AUTO_IMPROVE_EVERY", "").strip()
    if raw.isdigit():
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


def read_turn_count(session_id: str) -> int:
    """Current per-session tool-call/stop count, as ``bump_turn_counter`` keeps it."""
    if not session_id or not _COUNTER_FILE.exists():
        return 0
    try:
        data = json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
        return int(data.get(session_id, 0) or 0)
    except Exception:
        return 0


IMPROVE_COOLDOWN_DEFAULT_SECONDS = 600.0


def improve_cooldown_seconds() -> float:
    """Minimum seconds between idle/auto improves of one session (COGNEE_IMPROVE_COOLDOWN)."""
    raw = os.environ.get("COGNEE_IMPROVE_COOLDOWN", "").strip()
    try:
        value = float(raw) if raw else IMPROVE_COOLDOWN_DEFAULT_SECONDS
    except ValueError:
        return IMPROVE_COOLDOWN_DEFAULT_SECONDS
    return max(0.0, value)


def _improve_state_path(session_id: str) -> Path:
    digest = hashlib.sha1(str(session_id).encode("utf-8")).hexdigest()
    return _IMPROVE_STATE_DIR / f"{digest}.json"


def read_improve_state(session_id: str) -> dict:
    """Last successful improve of ``session_id``; ``{}`` when it never improved."""
    if not session_id:
        return {}
    data = _load_json_file(_improve_state_path(session_id))
    return data if isinstance(data, dict) else {}


def record_improve_success(session_id: str, dataset: str, trigger: str) -> None:
    """Persist that an improve of ``session_id`` just succeeded.

    Called by the improve functions themselves (``_run_session_improve_locked``
    and ``config.improve_session_local``) on a confirmed submit, never by their
    callers: the idle watcher reports success even when the per-session lock
    refused it, so recording there would stamp an improve that never landed.
    The idle/auto triggers read this back through ``improve_throttle_reason``.
    Best-effort: a write failure is logged and never fails the improve.
    """
    if not session_id:
        return
    try:
        _write_json_file(
            _improve_state_path(session_id),
            {
                "session_id": session_id,
                "dataset": dataset,
                "last_improved_at": time.time(),
                "turn_count_at_improve": read_turn_count(session_id),
                "trigger": trigger,
            },
        )
    except Exception as exc:
        hook_log("improve_state_write_failed", {"session": session_id, "error": str(exc)[:200]})


def improve_throttle_reason(session_id: str) -> str:
    """Why an idle/auto improve of ``session_id`` should be skipped right now.

    ``"cooldown"`` while the last successful improve is younger than
    ``improve_cooldown_seconds()``; ``"no_new_entries"`` when nothing was stored
    since it; ``""`` when an improve may run. A session that never improved is
    never throttled. Only the automatic triggers (idle watcher, every-N entries)
    honour this — the session-end, manual and dataset-switch syncs always run.
    """
    state = read_improve_state(session_id)
    if not state:
        return ""
    try:
        last = float(state.get("last_improved_at", 0) or 0)
    except (TypeError, ValueError):
        last = 0.0
    if last and time.time() - last < improve_cooldown_seconds():
        return "cooldown"
    try:
        count_then = int(state.get("turn_count_at_improve", -1))
    except (TypeError, ValueError):
        count_then = -1
    if count_then >= 0 and read_turn_count(session_id) <= count_then:
        return "no_new_entries"
    return ""


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


def load_cached_agent_key(service_url: str = "") -> str:
    """Return the provisioned plugin-agent key (matching service_url if recorded).

    Empty string when the plugin has no provisioned identity — callers fall
    back to the principal-key chain.
    """
    data = _load_json_file(_AGENT_KEY_CACHE)
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


def load_cached_agent_id(service_url: str = "") -> str:
    """The provisioned agent sub-user's id (recorded with its key), or ""."""
    if not load_cached_agent_key(service_url):
        return ""
    data = _load_json_file(_AGENT_KEY_CACHE)
    return str(data.get("agent_id") or "").strip() if isinstance(data, dict) else ""


def save_cached_agent_key(
    service_url: str, key: str, agent_id: str = "", *, principal_key: str = ""
) -> None:
    if not str(key or "").strip():
        return
    _write_credential(
        {
            "base_url": _normalize_service_url(service_url),
            "api_key": str(key).strip(),
            "agent_id": str(agent_id or ""),
            "plugin_key": PLUGIN_KEY,
            "principal_fingerprint": _principal_fingerprint(principal_key),
            "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )


def clear_cached_agent_key() -> None:
    """Drop the provisioned identity (revoked key / dashboard disconnect)."""
    try:
        _AGENT_KEY_CACHE.unlink(missing_ok=True)
    except Exception:
        pass


# ── Shared agent memory ────────────────────────────────────────────────────
#
# A provisioned plugin agent is its own user, and cognee's grants only flow
# child -> parent: the parent sees what the agent creates, the agent sees
# nothing the parent (or a sibling agent) owns. Left alone, per-plugin
# identities would silo memory — Antigravity could not recall what Claude
# Code stored. Shared agent memory (the default; COGNEE_SHARED_AGENT_MEMORY=false
# opts out into separated memory) closes that with the server's existing
# permission model, no core changes:
#
#   1. the parent owns a tenant (created if it has none — roles only exist
#      inside a tenant) and a role named AGENT_ROLE_NAME in it;
#   2. every plugin agent is a member of that tenant and role;
#   3. the role holds read+write on the parent's datasets (backfilled on every
#      bootstrap/refresh, so datasets created later by any sibling converge);
#   4. the launch's dataset is addressed by UUID: a canonical, parent-owned
#      dataset per name that every agent writes to, plus any other readable
#      same-named copies for recall (see resolve_active_dataset_ids).
#
# Every control-plane call below authenticates as the PRINCIPAL (tenant/role
# management is owner-only server-side, so an agent key can never widen its
# own access); only ``select_tenant`` runs as the agent, on itself.


def shared_memory_enabled(config: dict | None = None) -> bool:
    """Shared agent memory is on unless the user opted out.

    ``COGNEE_SHARED_AGENT_MEMORY`` (env) wins over ``shared_agent_memory`` in
    the config file; both default to on.
    """
    raw = str(os.environ.get("COGNEE_SHARED_AGENT_MEMORY", "") or "").strip()
    if not raw and isinstance(config, dict) and config.get("shared_agent_memory") is not None:
        raw = str(config.get("shared_agent_memory"))
    if not raw:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def load_shared_memory_marker(service_url: str = "") -> dict:
    """The shared-memory marker for ``service_url`` ({} when none / other URL)."""
    data = _load_json_file(_SHARED_MEMORY_MARKER)
    if not isinstance(data, dict):
        return {}
    wanted = _normalize_service_url(service_url or _local_api_url())
    recorded = _normalize_service_url(str(data.get("base_url") or ""))
    if wanted and recorded and wanted != recorded:
        return {}
    return data


def _save_shared_memory_marker(marker: dict) -> None:
    marker["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # A structural reason is only structural for THIS plugin version: an
    # update may lift the limitation (e.g. once the server re-stamps
    # tenant-less datasets), so SessionStart re-evaluates it after an update.
    marker["plugin_version"] = _installed_plugin_version()
    _write_json_file(_SHARED_MEMORY_MARKER, marker)


def principal_key_for_control_plane(service_url: str = "") -> str:
    """The PARENT user's key, for tenant/role/grant calls.

    Never the provisioned agent key: ``_api_key_with_source`` stamps that one
    into the env, so an env key equal to the cached agent key is the agent's,
    not the user's. Falls through to the cached principal key (SessionStart
    caches an env-provided principal when it provisions, so detached workers
    can still act as the parent).
    """
    service_url = _normalize_service_url(service_url or _local_api_url())
    agent_key = load_cached_agent_key(service_url)
    for candidate in (
        os.environ.get("COGNEE_PRINCIPAL_API_KEY", ""),
        os.environ.get("COGNEE_API_KEY", ""),
        load_cached_api_key(service_url),
    ):
        candidate = str(candidate or "").strip()
        if candidate and candidate != agent_key:
            return candidate
    return ""


def active_agent_key(service_url: str = "") -> str:
    """The cached plugin-agent key when it is the credential in force.

    Cached for this server, not blocked, bound to the current principal, and
    allowed by the identity mode — the same verdict the data plane reaches in
    ``_api_key_with_source``. "" otherwise, never an exception: callers here
    (shared-memory refresh, the shell skills) fall back to the principal.
    """
    try:
        key, source = _api_key_with_source(service_url)
    except RuntimeError:
        return ""
    return key if source == "plugin_agent_key" else ""


def _control_plane_request(
    path: str,
    payload=None,
    *,
    api_key: str,
    method: str = "POST",
    timeout: float = 20.0,
) -> tuple[int, object]:
    """``_json_http_request`` that reports instead of raising: ``(status, body)``.

    ``status`` is the HTTP status (200 for any 2xx), or 0 when the request never
    got an HTTP answer (connection error, timeout).
    """
    try:
        body = _json_http_request(path, payload, method=method, timeout=timeout, api_key=api_key)
        return 200, body
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8")[:300]
        except Exception:
            pass
        return exc.code, {"detail": detail}
    except Exception as exc:
        hook_log("control_plane_request_failed", {"path": path, "error": str(exc)[:200]})
        return 0, {"error": str(exc)[:200]}


def _accepted(status: int) -> bool:
    """A 2xx, or a 409 — the server's "already exists" for idempotent adds."""
    return status == 200 or status == 409


def _row_str(row: dict, *keys: str) -> str:
    """First non-empty of several spellings (OutDTOs answer camelCase)."""
    for key in keys:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def user_me_via_http(api_key: str) -> dict:
    """``{"id", "tenant_id"}`` for the key's user, or {} (absent /users/me on some tenants)."""
    status, body = _control_plane_request(
        "/api/v1/users/me", api_key=api_key, method="GET", timeout=10.0
    )
    if status != 200 or not isinstance(body, dict):
        return {}
    return {
        "id": _row_str(body, "id"),
        "tenant_id": _row_str(body, "tenant_id", "tenantId"),
    }


def list_datasets_via_http(api_key: str, *, timeout: float = 15.0) -> list[dict]:
    """Every dataset the key can READ: ``[{"id", "name", "owner_id", "created_at"}]``."""
    status, body = _control_plane_request(
        "/api/v1/datasets", api_key=api_key, method="GET", timeout=timeout
    )
    if status != 200 or not isinstance(body, list):
        return []
    rows = []
    for item in body:
        if not isinstance(item, dict):
            continue
        dataset_id = _row_str(item, "id")
        name = _row_str(item, "name")
        if dataset_id and name:
            rows.append(
                {
                    "id": dataset_id,
                    "name": name,
                    "owner_id": _row_str(item, "owner_id", "ownerId"),
                    "created_at": _row_str(item, "created_at", "createdAt"),
                }
            )
    return rows


def create_dataset_via_http(api_key: str, name: str) -> dict:
    """POST /api/v1/datasets/ as ``api_key``: the (new or existing) dataset row, or {}."""
    # Either spelling works: the request helper replays a same-origin 307/308,
    # which is how cloud (bare -> slashed) and local (slashed -> bare) servers
    # disagree about this route.
    status, body = _control_plane_request(
        "/api/v1/datasets/", {"name": name}, api_key=api_key, timeout=30.0
    )
    if status != 200 or not isinstance(body, dict):
        return {}
    dataset_id = _row_str(body, "id")
    return {"id": dataset_id, "name": _row_str(body, "name") or name} if dataset_id else {}


def _permissions_supported(principal_key: str) -> bool:
    """Does this server expose the permissions API (tenants/roles/grants)?

    Probed on the one endpoint that cannot 404 for any other reason: several
    permission routes answer 404 for a *missing tenant* (TenantNotFoundError),
    so a 404 elsewhere is not a capability verdict.
    """
    status, _ = _control_plane_request(
        "/api/v1/permissions/tenants/me", api_key=principal_key, method="GET", timeout=10.0
    )
    return status not in (404, 405)


def _typed_dataset_ids_supported(api_key: str) -> bool:
    """Shared memory stores session entries by dataset UUID; an SDK that
    advertises ``dataset_id`` but rejects it at runtime cannot host it (see
    ``require_typed_dataset_id_support``)."""
    try:
        require_typed_dataset_id_support(api_key=api_key)
        return True
    except Exception:
        return False


def _ensure_tenant(principal_key: str, parent: dict, datasets: list[dict]) -> tuple[str, str]:
    """Resolve the tenant the shared role lives in: ``(tenant_id, reason)``.

    The parent's active tenant when it has one. A tenant-less parent (the OSS
    default user) gets one created — but only when no dataset it can read
    exists yet: activating a tenant re-scopes dataset visibility to that
    tenant, which would hide every dataset created under no tenant — the
    parent's own, and (since the agent selects the tenant too) any an agent
    already created under name addressing. Such installs stay on separated
    memory (reason ``tenantless_with_data``).
    """
    tenant_id = str(parent.get("tenant_id") or "")
    if tenant_id:
        return tenant_id, ""
    parent_id = str(parent.get("id") or "")
    if datasets:
        return "", "tenantless_with_data"
    status, body = _control_plane_request(
        f"/api/v1/permissions/tenants?tenant_name={urllib.parse.quote(f'cognee-{parent_id[:8]}')}",
        api_key=principal_key,
    )
    if status != 200 or not isinstance(body, dict):
        hook_log("shared_memory_tenant_create_failed", {"status": status})
        return "", "tenant_create_failed"
    return _row_str(body, "tenant_id", "tenantId"), ""


def _ensure_role(principal_key: str, tenant_id: str) -> tuple[str, str]:
    """Get-or-create AGENT_ROLE_NAME in ``tenant_id``: ``(role_id, reason)``."""

    def _find() -> str:
        status, body = _control_plane_request(
            f"/api/v1/permissions/tenants/{tenant_id}/roles",
            api_key=principal_key,
            method="GET",
            timeout=10.0,
        )
        if status == 200 and isinstance(body, list):
            for row in body:
                if isinstance(row, dict) and _row_str(row, "name") == AGENT_ROLE_NAME:
                    return _row_str(row, "id")
        return ""

    role_id = _find()
    if role_id:
        return role_id, ""
    status, body = _control_plane_request(
        f"/api/v1/permissions/roles?role_name={urllib.parse.quote(AGENT_ROLE_NAME)}",
        api_key=principal_key,
    )
    if status == 200 and isinstance(body, dict) and _row_str(body, "role_id", "roleId"):
        return _row_str(body, "role_id", "roleId"), ""
    if status == 409:
        return _find(), ""
    if status in (401, 403):
        # Only the tenant owner may create roles: an org member on a shared
        # tenant cannot run shared memory — stay separated rather than fail.
        return "", "not_tenant_owner"
    hook_log("shared_memory_role_create_failed", {"status": status})
    return "", "role_create_failed"


def _add_agent_to_tenant_and_role(
    principal_key: str, agent_key: str, agent_id: str, tenant_id: str, role_id: str
) -> str:
    """Membership wiring for one agent; returns a failure reason or ""."""
    status, _ = _control_plane_request(
        f"/api/v1/permissions/users/{agent_id}/tenants?tenant_id={tenant_id}",
        api_key=principal_key,
    )
    if not _accepted(status):
        return "not_tenant_owner" if status in (401, 403) else "tenant_membership_failed"
    # The agent selects the tenant ITSELF: membership alone doesn't set its
    # active tenant, and the dataset-visibility filter compares against that.
    # create_agent copies the parent's tenant at provision time, so this is a
    # no-op there; it matters when the tenant was created after provisioning —
    # the fresh-install path, where the agent is provisioned first. Without it
    # every grant that follows is invisible to the agent, so a failure here is
    # a wiring failure (retried on the next launch), not a warning.
    status, _ = _control_plane_request(
        "/api/v1/permissions/tenants/select", {"tenant_id": tenant_id}, api_key=agent_key
    )
    if status != 200:
        hook_log("shared_memory_agent_select_tenant_failed", {"status": status})
        return "agent_tenant_select_failed"
    status, _ = _control_plane_request(
        f"/api/v1/permissions/users/{agent_id}/roles?role_id={role_id}",
        api_key=principal_key,
    )
    if not _accepted(status):
        return "not_tenant_owner" if status in (401, 403) else "role_membership_failed"
    return ""


def _remove_agent_from_role(principal_key: str, agent_id: str, role_id: str) -> bool:
    """Take the agent out of the shared role (opt-out). True when it is no
    longer a member — including when it already wasn't (404)."""
    if not (principal_key and agent_id and role_id):
        return False
    status, _ = _control_plane_request(
        f"/api/v1/permissions/users/{agent_id}/roles?role_id={role_id}",
        api_key=principal_key,
        method="DELETE",
    )
    if status not in (200, 404):
        hook_log("shared_memory_leave_role_failed", {"status": status})
        return False
    return True


_GRANT_DENIED_RETRY_SECONDS = 3600.0


def _grant_role_on_datasets(
    principal_key: str, role_id: str, dataset_ids: list[str], marker: dict
) -> set[str]:
    """Give the role read+write on each dataset (one call per dataset, so a
    single unshareable one — e.g. read-only shared by someone else — cannot
    fail the batch). Outcomes are memoised in ``marker["granted"]``:
    ``"ok"`` never retried, a denial retried hourly and logged when it is
    new. Returns the ids the role holds read+write on."""
    granted = marker.setdefault("granted", {})
    if not isinstance(granted, dict):
        granted = marker["granted"] = {}
    now = time.time()
    for dataset_id in dataset_ids:
        prior = granted.get(dataset_id)
        if prior == "ok":
            continue
        if isinstance(prior, dict) and now - float(prior.get("denied_at") or 0) < (
            _GRANT_DENIED_RETRY_SECONDS
        ):
            continue
        outcome = "ok"
        for permission in ("read", "write"):
            status, _ = _control_plane_request(
                f"/api/v1/permissions/datasets/{role_id}?permission_name={permission}",
                [dataset_id],
                api_key=principal_key,
            )
            if status in (401, 403):
                outcome = {"denied_at": now}
                if not isinstance(prior, dict):
                    # Typically a dataset shared to the user read-only by someone
                    # else: the user cannot share it on, so the agents cannot
                    # reach it. Said once per dataset, not once per hour.
                    hook_log(
                        "shared_memory_grant_denied",
                        {"dataset_id": dataset_id, "permission": permission, "status": status},
                    )
                break
            if status != 200:
                outcome = None  # transient: retry next time
                break
        if outcome is None:
            granted.pop(dataset_id, None)
            continue
        granted[dataset_id] = outcome
    return {dataset_id for dataset_id, outcome in granted.items() if outcome == "ok"}


def _pick_canonical(rows: list[dict], parent_id: str) -> dict:
    """The canonical dataset among same-named rows: the parent's own copy,
    else the oldest (deterministic across plugins, so siblings converge)."""
    for row in rows:
        if row["owner_id"] and row["owner_id"] == parent_id:
            return row
    return sorted(rows, key=lambda r: (r["created_at"] or "9", r["id"]))[0]


def ensure_shared_memory(
    *,
    service_url: str,
    principal_key: str,
    agent_key: str,
    agent_id: str,
    dataset: str = "",
    allow_setup: bool = True,
    config: dict | None = None,
) -> dict:
    """Wire (or refresh) shared agent memory for this plugin's agent.

    Returns ``{"mode": "shared"|"separated", "reason", "dataset_id",
    "dataset_ids", "role_id"}``. ``dataset_id`` is the canonical UUID to write
    ``dataset`` under and ``dataset_ids`` the UUIDs to recall from; both empty
    when separated (callers keep addressing the dataset by name).

    ``allow_setup=False`` (the idle watcher's periodic refresh) only re-resolves
    the canonical dataset and backfills grants against wiring SessionStart
    already completed — it never creates tenants or roles. Every step degrades
    to separated memory with a logged reason; nothing here can fail a session.
    """
    service_url = _normalize_service_url(service_url or _local_api_url())

    def _separated(reason: str) -> dict:
        return {
            "mode": "separated",
            "reason": reason,
            "dataset_id": "",
            "dataset_ids": [],
            "role_id": "",
        }

    if not shared_memory_enabled(config):
        # Opting out is a real boundary, not just an addressing change: the
        # agent LEAVES the shared role, so it can no longer read or write the
        # user's datasets (its own remain). The marker is demoted so nothing
        # (dataset_id_for, the switch listing, the doctor) keeps treating the
        # wiring as active, but the tenant / role / parent ids are kept —
        # re-enabling puts the agent back into the same role and canonical
        # dataset instead of creating new ones. Runs as the principal (role
        # management is owner-only); retried on the next launch if it fails.
        marker = load_shared_memory_marker(service_url)
        if marker.get("mode") == "shared":
            removed = _remove_agent_from_role(
                principal_key,
                str(marker.get("agent_id") or agent_id),
                str(marker.get("role_id") or ""),
            )
            if removed:
                _save_shared_memory_marker(
                    {**marker, "mode": "separated", "reason": "opt_out", "role_member": False}
                )
            hook_log(
                "shared_memory_opted_out", {"role_id": marker.get("role_id"), "left_role": removed}
            )
        return _separated("opt_out")
    if not (principal_key and agent_key and agent_id):
        return _separated("no_agent_identity")

    marker = load_shared_memory_marker(service_url)
    wired = (
        marker.get("mode") == "shared"
        and marker.get("agent_id") == agent_id
        and bool(marker.get("role_id"))
        and bool(marker.get("parent_user_id"))
    )
    if not wired and not allow_setup:
        return _separated(str(marker.get("reason") or "not_wired"))

    datasets: list[dict] | None = None
    if not wired:
        if not _permissions_supported(principal_key):
            _save_shared_memory_marker(
                {"base_url": service_url, "mode": "separated", "reason": "unsupported"}
            )
            hook_log("shared_memory_skipped", {"reason": "unsupported"})
            return _separated("unsupported")
        if not _typed_dataset_ids_supported(principal_key):
            _save_shared_memory_marker(
                {
                    "base_url": service_url,
                    "mode": "separated",
                    "reason": "typed_dataset_unsupported",
                }
            )
            hook_log("shared_memory_skipped", {"reason": "typed_dataset_unsupported"})
            return _separated("typed_dataset_unsupported")
        parent = user_me_via_http(principal_key)
        if not parent.get("id"):
            return _separated("principal_unresolved")
        datasets = list_datasets_via_http(principal_key)
        tenant_id, reason = _ensure_tenant(principal_key, parent, datasets)
        role_id = ""
        if not reason:
            role_id, reason = _ensure_role(principal_key, tenant_id)
        if not reason:
            reason = _add_agent_to_tenant_and_role(
                principal_key, agent_key, agent_id, tenant_id, role_id
            )
        if reason:
            _save_shared_memory_marker(
                {"base_url": service_url, "mode": "separated", "reason": reason}
            )
            hook_log("shared_memory_skipped", {"reason": reason})
            return _separated(reason)
        marker = {
            "base_url": service_url,
            "mode": "shared",
            "reason": "",
            "tenant_id": tenant_id,
            "role_id": role_id,
            "parent_user_id": parent["id"],
            "agent_id": agent_id,
            "granted": {},
            "canonical": {},
        }
        hook_log(
            "shared_memory_wired",
            {"tenant_id": tenant_id, "role_id": role_id, "agent_id": agent_id},
        )

    parent_id = str(marker.get("parent_user_id") or "")
    role_id = str(marker.get("role_id") or "")
    if datasets is None:
        datasets = list_datasets_via_http(principal_key)

    # Backfill: the role gets read+write on everything the parent can share.
    # Datasets a sibling agent creates are auto-shared to the parent, so this
    # is also how they reach every other agent — no per-plugin coordination.
    role_holds = _grant_role_on_datasets(
        principal_key, role_id, [row["id"] for row in datasets], marker
    )

    # The launch's dataset: a canonical copy every agent writes to, plus other
    # same-named copies for recall. Only datasets the role actually holds (or
    # the parent owns) qualify — a same-named dataset shared to the user
    # read-only by someone else cannot be granted on, so it is neither the
    # write target nor part of the recall set (one unreadable id would fail the
    # whole recall). With no eligible copy the parent creates its own.
    write_id, read_ids = "", []
    if dataset:
        same_name = [row for row in datasets if row["name"] == dataset]
        eligible = [
            row for row in same_name if row["owner_id"] == parent_id or row["id"] in role_holds
        ]
        if not eligible:
            created = create_dataset_via_http(principal_key, dataset)
            if not created.get("id"):
                # The wiring itself is fine (the marker keeps its grants), but
                # this launch has no canonical UUID to address. Report that
                # rather than "shared" with an empty dataset_id, which would
                # fall back to name addressing and quietly write to an
                # agent-owned copy nobody else can see.
                _save_shared_memory_marker(marker)
                hook_log(
                    "shared_memory_skipped",
                    {"reason": "dataset_create_failed", "dataset": dataset},
                )
                return _separated("dataset_create_failed")
            created = {**created, "owner_id": parent_id, "created_at": ""}
            same_name.append(created)
            datasets.append(created)
            role_holds |= _grant_role_on_datasets(principal_key, role_id, [created["id"]], marker)
            eligible = [created]
        if eligible:
            write_id = _pick_canonical(eligible, parent_id)["id"]
            canonical = marker.setdefault("canonical", {})
            if isinstance(canonical, dict):
                canonical[dataset] = write_id
            read_ids = [write_id] + [
                row["id"]
                for row in same_name
                if row["id"] != write_id
                and (row["id"] in role_holds or row["owner_id"] == agent_id)
            ]
    _save_shared_memory_marker(marker)
    return {
        "mode": "shared",
        "reason": "",
        "dataset_id": write_id,
        "dataset_ids": read_ids,
        "role_id": role_id,
    }


def resolve_shared_dataset(dataset: str, *, allow_setup: bool = False) -> dict:
    """``ensure_shared_memory`` for an already-wired agent, from any process.

    Resolves the keys itself (principal for the control plane, cached agent
    identity), so the dataset switch and the idle watcher can re-resolve a
    dataset's canonical UUIDs and backfill grants without SessionStart's
    context. Returns the same outcome dict; ``mode == "separated"`` when
    shared memory is off, unwired, or the keys are unavailable.
    """
    service_url = _normalize_service_url(_local_api_url())
    outcome = {
        "mode": "separated",
        "reason": "not_wired",
        "dataset_id": "",
        "dataset_ids": [],
        "role_id": "",
    }
    if not shared_memory_enabled():
        return {**outcome, "reason": "opt_out"}
    principal_key = principal_key_for_control_plane(service_url)
    agent_key = active_agent_key(service_url)
    agent_id = load_cached_agent_id(service_url)
    if not (principal_key and agent_key and agent_id):
        return {**outcome, "reason": "no_agent_identity" if not agent_key else "no_principal_key"}
    return ensure_shared_memory(
        service_url=service_url,
        principal_key=principal_key,
        agent_key=agent_key,
        agent_id=agent_id,
        dataset=dataset,
        allow_setup=allow_setup,
    )


def refresh_shared_memory(host_key: str = "") -> bool:
    """Periodic refresh for a running launch (idle watcher).

    Re-resolves the active dataset's canonical UUIDs — a sibling plugin may
    have created a same-named copy or a brand-new dataset since this launch
    started — and backfills the shared role's grants, so new datasets become
    visible to every agent within one refresh interval instead of at the next
    session start. Returns True when the launch record was updated.
    """
    host_key = _sanitize_session_key(host_key) or get_session_key()
    if not host_key or not _read_map_record(host_key):
        return False
    dataset = resolve_active_dataset(host_key)
    shared = resolve_shared_dataset(dataset)
    if shared["mode"] != "shared" or not shared["dataset_id"]:
        return False
    # ``dataset=`` pins the ids to the dataset they were resolved for: a switch
    # that completed during the resolution above leaves the record untouched.
    return set_launch_dataset_ids(
        host_key, shared["dataset_id"], shared["dataset_ids"], dataset=dataset
    )


def plugin_identity_mode(config: dict | None = None) -> str:
    value = (config or {}).get("plugin_identity", os.environ.get("COGNEE_PLUGIN_IDENTITY", "auto"))
    if value is None or str(value).strip().lower() == "auto":
        return "auto"
    normalized = str(value).strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return "enabled"
    if normalized in ("0", "false", "no", "off"):
        return "disabled"
    raise ValueError("COGNEE_PLUGIN_IDENTITY must be auto, true, or false")


def _principal_fingerprint(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest() if key else ""


@contextmanager
def plugin_identity_lock(timeout: float = 25.0):
    """Fail closed; an OS lock is released even if a bootstrap process dies."""
    path = _AGENT_KEY_CACHE.with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        # Windows permits locking beyond EOF. Writing here would fail when
        # another process holds the byte lock, before our retry loop runs.
        deadline = time.monotonic() + timeout
        while not locked:
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except OSError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Timed out waiting for plugin identity bootstrap") from None
                time.sleep(0.05)
        yield
    finally:
        if locked:
            if os.name == "nt":
                import msvcrt

                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def block_cached_agent_key(expected_key: str) -> None:
    with plugin_identity_lock():
        data = _load_json_file(_AGENT_KEY_CACHE)
        # A concurrent explicit reconnect may already have replaced this key.
        if data.get("api_key") == expected_key:
            data["blocked"] = True
            _write_credential(data)


def _write_credential(data: dict) -> None:
    _AGENT_KEY_CACHE.parent.mkdir(parents=True, exist_ok=True)
    temporary = _AGENT_KEY_CACHE.with_name(f"agent_key.{uuid.uuid4().hex}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, _AGENT_KEY_CACHE)
    finally:
        temporary.unlink(missing_ok=True)


def _api_key_with_source(service_url: str = "") -> tuple[str, str]:
    """Select a bound agent credential without replacing the principal in env."""
    service_url = _normalize_service_url(service_url or _local_api_url())
    agent_key = load_cached_agent_key(service_url)
    env_key = str(os.environ.get("COGNEE_API_KEY", "") or "").strip()
    principal = str(os.environ.get("COGNEE_PRINCIPAL_API_KEY", "") or "").strip()
    if env_key and env_key != agent_key:
        principal = env_key
    principal = principal or load_cached_api_key(service_url)
    mode = plugin_identity_mode()
    if agent_key and mode != "disabled":
        record = _load_json_file(_AGENT_KEY_CACHE)
        problem = ""
        if record.get("blocked"):
            problem = "Plugin identity was rejected; reconnect explicitly (no automatic rotation)"
        elif not principal or record.get("principal_fingerprint") != _principal_fingerprint(
            principal
        ):
            problem = "Plugin identity belongs to another or unverified principal; run SessionStart"
        if not problem:
            return agent_key, "plugin_agent_key"
        # A rejected or foreign identity is never used. Explicit identity
        # (``true``) makes that an error; ``auto`` — identity only in service
        # of shared memory — keeps the plugin working as the principal. With
        # no principal to fall back to, ``auto`` must not run keyless either:
        # that would fail every request quietly instead of naming the cause.
        if mode == "enabled" or not principal:
            raise RuntimeError(
                problem
                if principal
                else problem + " — and no principal key is available (COGNEE_API_KEY unset)"
            )
    if mode == "enabled":
        raise RuntimeError("Plugin identity is enabled but not connected; run SessionStart")
    if principal:
        return principal, "env_api_key" if principal in (
            env_key,
            os.environ.get("COGNEE_PRINCIPAL_API_KEY"),
        ) else "cache_single_key"
    return "", "missing"


def _api_key() -> str:
    return _api_key_with_source()[0]


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
        previous = str(os.environ.get("COGNEE_API_KEY", "") or "").strip()
        if previous and previous != api_key:
            os.environ["COGNEE_PRINCIPAL_API_KEY"] = previous
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

    Global (not namespaced) because Cognee integrations share one server on the
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
    marker untouched — the renderer shows the age of an old reading instead,
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
# writes. Antigravity tracks the plugin's git ref (main) rather than a version
# pin, so the nudge is driven by comparing the published plugin.json
# version to the installed one. Talks only to raw.githubusercontent over the
# shared certifi TLS context. Opt out with COGNEE_UPDATE_CHECK=off.
_UPDATE_CHECK_FILE = _PLUGIN_DIR / "update-check.json"
_UPDATE_CHECK_INTERVAL_DEFAULT = 3600.0
_UPDATE_DEFAULT_REPO = "topoteretes/cognee-integrations"
_UPDATE_DEFAULT_REF = "main"
_UPDATE_MANIFEST_PATH = "integrations/antigravity/plugin.json"


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
    root = os.environ.get("COGNEE_ANTIGRAVITY_PLUGIN_ROOT", "").strip()
    if root:
        candidates.append(Path(root) / "plugin.json")
    candidates.append(Path(__file__).resolve().parent.parent / "plugin.json")
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

    Antigravity tracks the plugin ref rather than a version pin, so we read the published
    version from the default repo/ref. The nudge is purely a version comparison,
    so a local checkout only nags when it is behind the published version.
    """
    return _UPDATE_DEFAULT_REPO, _UPDATE_DEFAULT_REF


def _fetch_published_version(repo: str, ref: str, etag: str) -> tuple:
    """GET the raw plugin.json and read its version.

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


_REDIRECT_REPLAY_CODES = (307, 308)
_MAX_REDIRECT_REPLAYS = 2


def _same_origin(first: str, second: str) -> bool:
    """Same scheme, host and effective port — the gate for replaying a keyed request."""
    import urllib.parse

    def origin(url):
        parts = urllib.parse.urlsplit(url)
        port = parts.port or (443 if parts.scheme == "https" else 80)
        return (parts.scheme, (parts.hostname or "").lower(), port)

    return origin(first) == origin(second)


def urlopen_following_307(req, *, timeout: float, context=None):
    """``urlopen`` that replays a method-preserving redirect (307/308).

    urllib refuses to replay a POST across a 307/308 — ``HTTPRedirectHandler``
    raises ``HTTPError`` instead — so a server that redirects between the two
    spellings of a collection route (``/api/v1/datasets`` and
    ``/api/v1/datasets/``) fails every POST to it. Both spellings occur in the
    wild and they redirect in *opposite* directions: cloud tenants 307 the bare
    path to the slashed one, while a local server 307s the slashed path to the
    bare one. No single spelling works everywhere, so the redirect has to be
    followed rather than guessed.

    Only a **same-origin** target is followed: these requests carry
    ``X-Api-Key``, which must never be replayed to another host. A cross-origin
    target, a missing ``Location``, or any other status leaves the original
    ``HTTPError`` to the caller untouched.
    """
    import urllib.error
    import urllib.parse
    import urllib.request

    for _ in range(_MAX_REDIRECT_REPLAYS):
        try:
            return urllib.request.urlopen(req, timeout=timeout, context=context)
        except urllib.error.HTTPError as exc:
            if exc.code not in _REDIRECT_REPLAY_CODES:
                raise
            location = exc.headers.get("Location") if exc.headers else ""
            if not location:
                raise
            target = urllib.parse.urljoin(req.full_url, location)
            if not _same_origin(req.full_url, target):
                raise
            try:
                exc.close()
            except Exception:
                # Best-effort connection release. An HTTPError carrying no body
                # never initialized its underlying file, and closing that raises
                # (KeyError on Python 3.9, the hooks' floor). The replay does not
                # depend on the close, and a raise here would mask the HTTP error.
                pass
            replay = urllib.request.Request(
                target, data=req.data, headers=dict(req.headers), method=req.get_method()
            )
            req = replay
    return urllib.request.urlopen(req, timeout=timeout, context=context)


def _json_http_request(
    path: str,
    payload: dict | None = None,
    *,
    method: str = "POST",
    timeout: float = 30.0,
    base_url: str | None = None,
    api_key: str | None = None,
):
    # base_url overrides the resolved service URL for calls that target a
    # different host than the memory data plane (e.g. the cloud platform API's
    # billing routes). api_key overrides the resolved key for calls that must
    # authenticate as a specific identity (e.g. plugin provisioning runs as
    # the principal, never as the provisioned agent).
    base_url = (base_url or _local_api_url()).rstrip("/")
    api_key = api_key if api_key is not None else _api_key()
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
    with urlopen_following_307(req, timeout=timeout, context=_https_context()) as resp:
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


_TYPED_DATASET_CAPABILITIES = {}


def require_typed_dataset_id_support(*, service_url: str = "", api_key=None) -> None:
    """Old SDKs advertise dataset_id but reject typed entries at runtime."""
    url = _normalize_service_url(service_url or _local_api_url())
    cached = _TYPED_DATASET_CAPABILITIES.get(url)
    supported = cached[1] if cached else False
    if cached is None or time.monotonic() - cached[0] > 60.0:
        spec = _json_http_request("/openapi.json", method="GET", base_url=url, api_key=api_key)
        supported = (
            spec.get("paths", {})
            .get("/api/v1/remember/entry", {})
            .get("post", {})
            .get("x-cognee-session-dataset-ids")
            is True
        )
        _TYPED_DATASET_CAPABILITIES[url] = (time.monotonic(), supported)
    if not supported:
        raise RuntimeError(
            "This Cognee server cannot safely store typed session memory by dataset UUID. "
            "Update the SDK before selecting a shared write dataset."
        )


def remember_entry_via_http(
    dataset: str,
    session_id: str,
    entry: dict,
    *,
    dataset_id: str | None = None,
    timeout: float = 30.0,
) -> dict | None:
    """Store a typed QA/trace entry through the backend API.

    API-mode hooks use this instead of importing Cognee's Python client,
    so they don't initialize local databases while talking to a backend.
    ``dataset_id`` (the canonical UUID under shared memory; resolved from
    ``dataset`` when not given) takes precedence server-side over the name.
    """
    if not dataset or not session_id:
        return None
    from _project_memory import route

    target = route(dataset, session_id)
    dataset = target["write"]
    if parse_dataset_id(dataset):
        require_typed_dataset_id_support()
    entry = _sanitize_value(entry)
    if target.get("node_set") and entry.get("type") in ("qa", "trace"):
        entry = {**entry, "node_set": target["node_set"]}
    # The canonical UUID under shared memory (explicit, or resolved from the
    # launch record) wins: a name only resolves among datasets the caller owns.
    # Otherwise a UUID-shaped dataset is sent as an id and a name as a name.
    resolved_id = dataset_id if dataset_id is not None else dataset_id_for(dataset)
    fields = {"dataset_id": resolved_id} if resolved_id else write_fields(dataset)
    return _json_http_request(
        "/api/v1/remember/entry",
        {"entry": entry, **fields, "session_id": session_id},
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


def disconnect_plugin_agent_via_http(*, principal_key: str, timeout: float = 20.0) -> bool:
    """DELETE /api/v1/integrations/plugins/{PLUGIN_KEY}: revoke this plugin's agent keys.

    The agent user and its data stay (re-provisioning later revives the same
    identity with a fresh key). Used when SessionStart discards a key it just
    minted, so no valid key nobody holds is left behind. Best-effort.
    """
    if not str(principal_key or "").strip():
        return False
    status, _ = _control_plane_request(
        f"/api/v1/integrations/plugins/{PLUGIN_KEY}",
        api_key=principal_key,
        method="DELETE",
        timeout=timeout,
    )
    if status != 200:
        hook_log("plugin_disconnect_failed", {"status": status})
    return status == 200


def provision_plugin_agent_via_http(
    *,
    principal_key: str,
    service_url: str = "",
    timeout: float = 20.0,
) -> tuple[str, dict]:
    """Create this plugin's identity without rotating an existing key.

    Authenticate as the user principal and require the server's advertised
    create_only contract before POSTing. OpenAPI is a capability declaration;
    its enforcement is covered by the SDK's provisioning regression tests.
    Unsupported/failed provisioning must fail closed in the caller.
    """
    if not str(principal_key or "").strip():
        return "failed", {}
    try:
        # Older servers ignore unknown query parameters and rotate keys. Verify
        # the create-only contract BEFORE sending any provisioning request.
        spec = _json_http_request(
            "/openapi.json",
            method="GET",
            api_key=principal_key,
            base_url=service_url or _local_api_url(),
            timeout=timeout,
        )
        operation = (
            spec.get("paths", {})
            .get("/api/v1/integrations/plugins/{plugin_key}/provision", {})
            .get("post", {})
        )
        if not any(
            p.get("name") == "create_only" and p.get("in") == "query"
            for p in operation.get("parameters", [])
        ):
            return "unsupported", {}
        result = _json_http_request(
            f"/api/v1/integrations/plugins/{PLUGIN_KEY}/provision?create_only=true",
            {},
            method="POST",
            timeout=timeout,
            api_key=principal_key,
            base_url=service_url or _local_api_url(),
        )
        reason = "not_an_object"
        if isinstance(result, dict):
            body = {
                "api_key": str(result.get("api_key") or result.get("apiKey") or "").strip(),
                "agent_id": str(result.get("agent_id") or result.get("agentId") or ""),
                "created": bool(result.get("created")),
            }
            if not (body["api_key"] and body["created"]):
                reason = "incomplete"
            elif not body["agent_id"]:
                # Shared memory wires the agent by id; without one the launch
                # would provision, then report ``no_agent_identity``.
                reason = "missing_agent_id"
            elif body["api_key"] == str(principal_key or "").strip():
                # The caller's own key handed back as the "agent" key: no
                # isolation at all, and the principal resolvers skip whatever
                # the agent cache holds, so the launch would end up keyless.
                reason = "agent_key_equals_principal"
            else:
                return "provisioned", body
        hook_log(
            "plugin_provision_bad_response",
            {
                "reason": reason,
                "keys": sorted(result) if isinstance(result, dict) else str(type(result)),
            },
        )
        return "failed", {}
    except urllib.error.HTTPError as exc:
        # 404/405: the server predates plugin provisioning (or the route set
        # differs) — a capability verdict, not a fault.
        if exc.code in (404, 405):
            return "unsupported", {}
        hook_log("plugin_provision_failed", {"status": exc.code, "error": str(exc)[:200]})
        return "failed", {}
    except Exception as exc:
        hook_log("plugin_provision_failed", {"error": str(exc)[:200]})
        return "failed", {}


def register_agent_via_http(
    *,
    agent_session_name: str,
    session_id: str = "",
    dataset_names: list[str] | None = None,
    dataset_ids: list[str] | None = None,
    timeout: float = 15.0,
) -> tuple[bool, dict]:
    payload = {
        "agent_session_name": agent_session_name,
        # Self-declared connection type (the server keeps a free-form registry;
        # "antigravity" is not in its documented KNOWN_AGENT_CONNECTION_TYPES yet).
        "type": CONNECTION_TYPE,
        "memory_mode": "hybrid",
        "source": "api",
    }
    if session_id:
        payload["session_id"] = session_id
    if dataset_names:
        payload["dataset_names"] = [
            str(name) for name in dataset_names if str(name).strip() and not parse_dataset_id(name)
        ]
        ids = [parse_dataset_id(name) for name in dataset_names if parse_dataset_id(name)]
        if ids:
            payload["dataset_ids"] = ids
    if dataset_ids:
        # The canonical dataset under shared memory — bound by UUID so the
        # connection registry points at the dataset actually written to.
        payload["dataset_ids"] = list(
            dict.fromkeys(
                [*payload.get("dataset_ids", [])]
                + [str(x).strip() for x in dataset_ids if str(x).strip()]
            )
        )

    try:
        result = _json_http_request(
            "/api/v1/agents/register", payload, method="POST", timeout=timeout
        )
        if isinstance(result, dict):
            return True, result
        return True, {}
    except Exception as exc:
        status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
        if status == 404:
            # The lifecycle routes are optional server-side: a missing register
            # route is a capability verdict, not a failed session.
            hook_log("agent_lifecycle_unsupported", {"operation": "register", "status": status})
            return False, {"lifecycle_supported": False}
        hook_log("agent_register_failed", {"error": str(exc)[:200], "status": status})
        # status_code lets SessionStart raise a classified HTTPError; auth_failed
        # lets it block a revoked plugin-agent key instead of failing outright.
        return False, {"status_code": status, "auth_failed": status in (401, 403)}


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
        if isinstance(exc, urllib.error.HTTPError) and exc.code == 404:
            hook_log("agent_lifecycle_unsupported", {"operation": "unregister", "status": 404})
            return True, 0
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
    dataset_ids: list[str] | None = None,
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
    # Project memory may route this session's writes to a companion dataset; the
    # recall scope follows that routing (the primary is searched separately below).
    from _deadlines import bounded_call
    from _project_memory import route

    target = route(dataset, session_id) if dataset and not code_query else {"write": dataset}
    write_dataset = target["write"]
    # Three sources of scope, in precedence order:
    #   1. COGNEE_PLUGIN_READ_DATASET_IDS on a graph-only recall: the user's own
    #      federated read set. Session history stays bound to ONE dataset, so
    #      the session id is dropped from that request.
    #   2. ``dataset_ids`` resolved by shared agent memory (the canonical
    #      parent-owned dataset plus readable same-named copies): a name only
    #      resolves among datasets the caller OWNS, which under a plugin
    #      identity is not the dataset the agent was granted.
    #   3. The (routed) dataset itself: sent as an id when UUID-shaped, else
    #      by name.
    fields, federated = recall_fields(write_dataset, scope)
    ids = [str(x).strip() for x in (dataset_ids or []) if str(x).strip()]
    if federated:
        payload.update(fields)
        payload.pop("session_id", None)
    elif ids and write_dataset == dataset:
        # Shared-memory ids describe the primary dataset; a companion-routed
        # session is addressed by its routed name instead.
        payload["dataset_ids"] = ids
    else:
        payload.update(fields)
    if search_type:
        payload["search_type"] = search_type
    if context_profile:
        payload["context_profile"] = context_profile

    def fetch_scopes():
        started = time.monotonic()
        result = _json_http_request("/api/v1/recall", payload, timeout=timeout)
        result = result if isinstance(result, list) else []
        if dataset != write_dataset and "graph" in scope:
            remaining = timeout - (time.monotonic() - started)
            if remaining > 0.05:
                primary_payload = {**payload, "datasets": [dataset], "scope": ["graph"]}
                primary_payload.pop("session_id", None)
                try:
                    extra = _json_http_request("/api/v1/recall", primary_payload, timeout=remaining)
                    if isinstance(extra, list):
                        result.extend(extra)
                except (OSError, TimeoutError):
                    pass
        return result

    return bounded_call(fetch_scopes, timeout)


def _backend_reachable(base_url: str, timeout: float = 1.5) -> bool:
    try:
        with urllib.request.urlopen(
            f"{base_url.rstrip('/')}/health", timeout=timeout, context=_https_context()
        ) as resp:
            return 200 <= resp.status < 500
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


# --- Session improve (server-side session->graph bridge) ----------------------
# The hooks write every turn into the SERVER session cache via /remember/entry,
# so the server can bridge a session itself: POST /api/v1/improve runs feedback
# weights, QA persist, trace-feedback persist, distillation, and enrichment over
# that cache. There is deliberately no client-side fallback: the old document
# bridge re-sent the whole accumulated session text (raw tool outputs included)
# for a full re-cognify on every sync, and a server without session-aware
# improve now simply reports the session as not synced.


def _improve_float_env(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name, "").strip()
        return float(raw) if raw else default
    except (TypeError, ValueError):
        return default


def _improve_submit_timeout() -> float:
    return _improve_float_env("COGNEE_IMPROVE_SUBMIT_TIMEOUT", 180.0)


_AMBIGUOUS_KEY = "_replay_ambiguous"
# Epoch seconds at which the entry was buffered. Read back by ``warmup_backlog``
# so the recall header can say how long the oldest unreplayed entry has waited.
_BUFFERED_AT_KEY = "_buffered_at"
# Buffer-internal bookkeeping, stripped before an entry is sent to the server.
_BUFFER_META_KEYS = (_AMBIGUOUS_KEY, _BUFFERED_AT_KEY)


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
    entry = dict(_sanitize_value(entry))
    if ambiguous:
        entry[_AMBIGUOUS_KEY] = True
    entry[_BUFFERED_AT_KEY] = time.time()
    with _buffer_lock():
        cache = _load_json_file(_bridge_file(session_id))
        key = _bridge_cache_key(dataset, session_id)
        session_cache = cache.setdefault(key, {"pending_entries": []})
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
    from _capture_policy import allow_tool, capture_enabled, redact

    if not capture_enabled():
        return 0, 0

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
            if isinstance(entry, dict):
                ambiguous = bool(entry.get(_AMBIGUOUS_KEY))
                send_entry = {k: v for k, v in entry.items() if k not in _BUFFER_META_KEYS}
            if ambiguous and verified and _entry_fingerprint(send_entry) in server_prints:
                # The original send committed after all — consume the buffered
                # copy without re-sending it.
                deduped += 1
                continue
            try:
                if send_entry.get("type") == "trace" and not allow_tool(
                    str(send_entry.get("origin_function", "")), send_entry.get("method_params", {})
                ):
                    deduped += 1
                    continue
                send_entry = redact(send_entry)
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
    from _project_memory import route

    dataset = route(dataset, session_id)["write"]
    submit_timeout = timeout if timeout is not None else _improve_submit_timeout()
    improve_payload = {
        **write_fields(dataset),
        "session_ids": [session_id],
        "run_in_background": True,
    }
    # Canonical UUID under shared memory: improve accepts dataset_id and
    # prefers it over the name (which only resolves among owned datasets).
    canonical_id = dataset_id_for(dataset)
    if canonical_id:
        improve_payload["dataset_id"] = canonical_id
    try:
        result = _json_http_request(
            "/api/v1/improve",
            improve_payload,
            timeout=submit_timeout,
        )
    except urllib.error.HTTPError as exc:
        outcome = {"ok": False, "status": exc.code, "error": f"HTTP {exc.code}: {exc.reason}"}
        if exc.code in (404, 405, 422):
            # Server without session-aware improve. Reported, never worked
            # around: the only alternative is a full-document re-cognify.
            outcome["unsupported"] = True
        return outcome
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
    # missed, so the host reported no cognify_status at all.
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
    if parse_dataset_id(dataset):
        listing = list_writable_datasets()
        if not any(row["id"] == dataset and row["writable"] is True for row in listing["datasets"]):
            raise RuntimeError("Write permission for the selected dataset could not be verified")
        return
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


def run_session_improve(dataset: str, session_id: str, *, trigger: str = "final") -> bool:
    """API-mode session->graph sync: drain warmup entries, then improve.

    ``trigger`` names the caller (``idle``, ``auto``, ``final``, ``manual``,
    ``switch``) and is recorded with the session's improve state on success.
    A server without session-aware improve is reported as not synced — there
    is no fallback. Returns True when a sync ran successfully.

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
        return _run_session_improve_locked(dataset, session_id, trigger=trigger)


def _run_session_improve_locked(dataset: str, session_id: str, *, trigger: str = "final") -> bool:
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
    ensure_dataset_via_http(dataset)
    outcome = improve_session_via_http(dataset, session_id)
    if outcome.get("unsupported"):
        # No session-aware improve on this server. Nothing else is tried: the
        # old document bridge re-cognified the whole transcript on every sync.
        hook_log(
            "improve_unsupported",
            {"dataset": dataset, "session": session_id, "status": outcome.get("status")},
        )
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
            "trigger": trigger,
            "ok": bool(outcome.get("ok")),
            "busy": bool(outcome.get("busy")),
            "error": str(outcome.get("error") or "")[:120],
        },
    )
    if outcome.get("ok"):
        record_improve_success(session_id, dataset, trigger)
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
#: Launch records: removed a week after their host pid is dead, or after 30
#: days regardless (a record with no pid is treated as alive, so that is the
#: only bound for those). The functional reader — the exit-watcher's final
#: sync — needs the record for seconds after the host exits, but a human
#: debugging a Friday crash on Monday needs it for days; a week matches the
#: marker rule above so there is one number to remember. Costs nothing:
#: `_live_launch_records` already ignores dead-pid records at read time.
_SWEEP_LAUNCH_RECORD_DEAD_GRACE_SECONDS = 7 * 24 * 3600
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
#: Files likewise. recall-breaker.json: cognee-search.sh used to redirect the
#: circuit breaker into this dir, splitting it from the one the hooks use.
_SWEEP_LEGACY_FILES = ("recall-breaker.json",)


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


def _sweep_pending_husks(counts: dict) -> None:
    """Empty ``{}`` pending files left by older versions of ``pop_pending_prompt``.
    Nothing is in flight for an empty buffer, so age is irrelevant; a live
    session that needs the file again simply recreates it."""
    try:
        entries = list(_PENDING_DIR.glob("*.json"))
    except OSError:
        return
    for path in entries:
        try:
            if path.stat().st_size <= 2 and not _load_json_file(path):
                _sweep_remove(path, counts, "pending_husks")
        except OSError:
            continue


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
            stale = (
                not (pid > 0 and _proc.pid_alive(pid)) or now - created_at > SYNC_LOCK_STALE_SECONDS
            )
        except Exception:
            stale = True  # unreadable lock: nothing can release it either
        if stale:
            _sweep_remove(path, counts, "improve_locks")


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
            (_IMPROVE_STATE_DIR, "improve_state"),
        ):
            _sweep_dir_by_age(directory, _SWEEP_SESSION_FILE_MAX_AGE_SECONDS, now, counts, key)
        _sweep_pending_husks(counts)
        _sweep_launch_records(now, counts)
        _sweep_improve_locks(now, counts)
        for name in _SWEEP_LEGACY_FILES:
            legacy_file = _PLUGIN_DIR / name
            if legacy_file.is_file():
                _sweep_remove(legacy_file, counts, "legacy_files")
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
