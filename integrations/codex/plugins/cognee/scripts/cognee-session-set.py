#!/usr/bin/env python3
"""Switch the current launch's Cognee session (the picker's 'set' step).

Usage: ``cognee-session-set.py <session_id>``

A model-invoked command has no session id in its environment, so it discovers
its launch by walking the process tree to the host (claude/codex) pid, then maps
that to the launch's ``host_key`` (recorded at SessionStart). It then rewrites
only the ``session_id`` in that launch's map record — ``conn_uuid`` (the liveness
handle) is untouched, so registration/counting is unaffected.

On a real switch it also fires a detached sync of the *outgoing* session to the
graph (Feature 4), without unregistering. Prints a JSON result.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from _plugin_common import (  # noqa: E402
    find_host_pid,
    hook_log,
    list_sessions_via_http,
    mapped_session_id,
    read_host_key_for_pid,
    sanitize_session_id,
    set_mapped_session,
)
from cognee_statusline_render import render_status_for_host

try:
    from config import get_dataset, load_config
except Exception:  # pragma: no cover - config import is best-effort
    get_dataset = None
    load_config = None

_SYNC_SCRIPT = Path(__file__).with_name("sync-session-to-graph.py")


def _sync_on_switch_enabled() -> bool:
    return os.environ.get("COGNEE_SYNC_ON_SWITCH", "1").strip().lower() not in ("0", "false", "no")


def _spawn_switch_sync(old_session_id: str, host_key: str, dataset: str) -> None:
    """Detached, sync-only flush of the outgoing session (never unregisters)."""
    if not old_session_id or not _sync_on_switch_enabled():
        return
    try:
        env = os.environ.copy()
        env["COGNEE_SWITCH_SYNC"] = "1"  # sync-only: no once-claim, no unregister
        env["COGNEE_SYNC_SESSION_ID"] = old_session_id
        if host_key:
            env["COGNEE_SESSION_KEY"] = host_key
        if dataset:
            env["COGNEE_SYNC_DATASET"] = dataset
        env.pop("COGNEE_UNREGISTER_ON_FINISH", None)
        subprocess.Popen(
            [sys.executable, str(_SYNC_SCRIPT), "--detached-final"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except Exception as exc:
        hook_log("switch_sync_spawn_failed", {"error": str(exc)[:200]})


def _session_exists(session_id: str):
    """True/False if ``session_id`` is among the principal's sessions; None if the
    listing call failed (existence unknown)."""
    try:
        data = list_sessions_via_http(limit=500, range_="all")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    ids = {
        str(s.get("session_id") or s.get("id") or "")
        for s in data.get("sessions", [])
        if isinstance(s, dict)
    }
    return session_id in ids


def main() -> None:
    raw = sys.argv[1].strip() if len(sys.argv) > 1 else ""
    chosen = sanitize_session_id(raw)
    if not chosen:
        print(json.dumps({"ok": False, "error": "no valid session id provided"}))
        return

    # Scope the host-pid walk to this tree's host so it can't match a foreign one.
    host_key = read_host_key_for_pid(find_host_pid(("codex",)))
    if not host_key:
        print(json.dumps({"ok": False, "error": "could not determine current launch"}))
        return

    old = mapped_session_id(host_key)
    switched = bool(old and old != chosen)

    # Existence is informational: a non-existent id is created lazily (sessions
    # are opaque strings that become real on first save). None = couldn't check.
    existed = _session_exists(chosen)

    dataset = ""
    try:
        if load_config and get_dataset:
            dataset = get_dataset(load_config())
    except Exception:
        pass

    # Flush the OUTGOING session to the graph BEFORE committing the switch, so a
    # crash mid-switch can't strand it. touched[] + the exit-watcher final sweep
    # are the durable backstop regardless of ordering.
    if switched:
        _spawn_switch_sync(old, host_key, dataset)

    old_committed, new = set_mapped_session(host_key, chosen)

    hook_log(
        "session_switched",
        {"host_key": host_key, "old": old_committed, "new": new, "existed": existed},
    )
    print(
        json.dumps(
            {
                "ok": True,
                "old_session": old_committed,
                "new_session": new,
                "switched": switched,
                "existed": existed,
                "created": existed is False,
                "synced_previous": switched,
                "status_line": render_status_for_host(host_key),
            }
        )
    )


if __name__ == "__main__":
    main()
