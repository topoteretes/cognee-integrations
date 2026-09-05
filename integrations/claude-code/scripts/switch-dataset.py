#!/usr/bin/env python3
"""Move this launch to another Cognee dataset (``/cognee-memory:cognee-switch-datasets``).

A Cognee session never spans two datasets, so a switch is: sync the current
session into its dataset, register a NEW session bound to the new dataset,
retire the old one, and repoint every hook at the new triple.

Usage:
    switch-dataset.py --list [--json]          # datasets you can switch to
    switch-dataset.py <dataset-name> [--force] [--json]

Runs under the host's shell tool (no hook payload), so it finds its own launch
record via ``resolve_host_key_outside_hook``; pass ``--session-key <host id>``
to pin one when several launches share a directory.

Exit codes:
    0  switched / listed            3  sync of the current session failed (use --force)
    1  unexpected error              4  registering the new session failed (nothing changed)
    2  launch record not found       5  dataset not writable / not found for this principal
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _logfiles import rotate_if_oversized as _rotate_log_if_oversized  # noqa: E402
from _plugin_common import (  # noqa: E402
    _new_conn_uuid,
    _read_map_record,
    hook_log,
    list_writable_datasets,
    load_resolved,
    mint_switch_session_id,
    register_agent_via_http,
    resolve_host_key_outside_hook,
    resolved_http_endpoint_auth,
    set_session_key,
    switch_launch_record,
    touch_activity,
    unregister_agent_via_http,
)
from _proc import pid_alive  # noqa: E402
from config import ensure_dataset_ready_via_api, load_config  # noqa: E402

_STATE_DIR = Path.home() / ".cognee-plugin" / "claude-code"
_WATCHER_PID = _STATE_DIR / "watcher.pid"
_WATCHER_STOP = _STATE_DIR / "watcher.stop"
_HERE = Path(__file__).resolve().parent
_SYNC_SCRIPT = _HERE / "sync-session-to-graph.py"
_WATCHER_SCRIPT = _HERE / "idle-watcher.py"

EXIT_OK, EXIT_ERROR, EXIT_NO_RECORD, EXIT_SYNC_FAILED, EXIT_REGISTER_FAILED, EXIT_NOT_WRITABLE = (
    0,
    1,
    2,
    3,
    4,
    5,
)


class SwitchError(Exception):
    def __init__(self, code: int, message: str, **detail):
        super().__init__(message)
        self.code = code
        self.detail = detail


# ── launch resolution ──────────────────────────────────────────────────────


def _resolve_launch(explicit_key: str) -> tuple[str, dict]:
    host_key = explicit_key.strip()
    source = "flag"
    if not host_key:
        host_key, source = resolve_host_key_outside_hook()
    rec = _read_map_record(host_key) if host_key else {}
    if not rec.get("session_id"):
        hint = (
            "several Cognee launches share this directory — rerun with "
            "--session-key <host session id>"
            if source == "ambiguous_cwd"
            else "start a Claude Code session with the Cognee plugin active first"
        )
        raise SwitchError(EXIT_NO_RECORD, f"no launch record found ({source}); {hint}")
    set_session_key(host_key)
    return host_key, rec


# ── listing ────────────────────────────────────────────────────────────────


def _list(host_key: str, rec: dict) -> dict:
    resolved = load_resolved(session_key=host_key)
    user_id = str(resolved.get("user_id") or "")
    try:
        listing = list_writable_datasets(user_id)
    except urllib.error.HTTPError as exc:
        raise SwitchError(EXIT_ERROR, f"GET /api/v1/datasets failed (HTTP {exc.code})")
    except Exception as exc:
        raise SwitchError(EXIT_ERROR, f"GET /api/v1/datasets failed ({exc})")
    current = str(rec.get("dataset") or resolved.get("dataset") or "")
    rows = [{**row, "current": current in (row["name"], row["id"])} for row in listing["datasets"]]
    return {
        "current": current,
        "session_id": str(rec.get("session_id") or ""),
        "datasets": rows,
        "hidden_readonly": listing["hidden_readonly"],
        "filtered": listing["filtered"],
    }


# ── the switch ─────────────────────────────────────────────────────────────


def _sync_current(host_key: str, session_id: str, dataset: str) -> None:
    """Bridge the session we are about to retire, strictly (non-zero = failure)."""
    env = os.environ.copy()
    env["COGNEE_SESSION_KEY"] = host_key
    env["COGNEE_SYNC_SESSION_ID"] = session_id
    env["COGNEE_SYNC_DATASET"] = dataset
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, str(_SYNC_SCRIPT), "--strict"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        env=env,
        timeout=float(os.environ.get("COGNEE_SWITCH_SYNC_TIMEOUT", "") or 900),
    )
    hook_log(
        "switch_sync_result",
        {
            "session": session_id,
            "dataset": dataset,
            "returncode": proc.returncode,
            "elapsed_ms": int((time.monotonic() - started) * 1000),
            "stderr": (proc.stderr or "")[-400:],
        },
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no detail"]
        raise SwitchError(
            EXIT_SYNC_FAILED,
            f"sync of session {session_id} (dataset {dataset}) failed: {tail[0]}",
        )


def _register_new(session_id: str, dataset: str) -> str:
    """Register the new session under a fresh connection handle; returns the handle.

    Fresh handle first, old one released after: the server's agent-mode count
    goes 1→2→1 and never touches 0, which would shut a local server down.
    """
    conn_uuid = _new_conn_uuid()
    ok, _ = register_agent_via_http(
        agent_session_name=conn_uuid, session_id=session_id, dataset_names=[dataset]
    )
    if not ok:
        raise SwitchError(
            EXIT_REGISTER_FAILED,
            f"registering session {session_id} on dataset {dataset} failed; nothing was changed",
        )
    return conn_uuid


def _ensure_dataset(dataset: str) -> None:
    service_url, api_key = resolved_http_endpoint_auth()
    try:
        asyncio.run(ensure_dataset_ready_via_api(service_url, api_key, dataset))
    except Exception as exc:
        text = str(exc)
        code = EXIT_NOT_WRITABLE if any(s in text for s in ("401", "403", "404")) else EXIT_ERROR
        raise SwitchError(code, f"dataset {dataset!r} is not available to this principal ({text})")


def _restart_idle_watcher(host_key: str, session_id: str, dataset: str, user_id: str) -> None:
    """Replace the idle watcher so its bootstrap names the new triple.

    The watcher re-reads the record before each bridge anyway; this keeps its
    log/bootstrap honest and clears a pending bridge aimed at the old session.
    """
    if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in ("1", "true", "yes"):
        return
    try:
        if _WATCHER_PID.exists():
            pid = int(_WATCHER_PID.read_text(encoding="utf-8").strip() or 0)
            if pid and pid_alive(pid):
                _WATCHER_STOP.write_text("stop", encoding="utf-8")
                os.kill(pid, signal.SIGTERM)
                deadline = time.monotonic() + 5.0
                while pid_alive(pid) and time.monotonic() < deadline:
                    time.sleep(0.1)
        if _WATCHER_STOP.exists():
            _WATCHER_STOP.unlink()
    except Exception as exc:
        hook_log("switch_watcher_stop_failed", {"error": str(exc)[:200]})

    config = load_config()
    bootstrap = {
        "session_id": session_id,
        "dataset": dataset,
        "user_id": user_id,
        "session_key": host_key,
        "config": {
            "base_url": config.get("base_url", ""),
            "llm_model": config.get("llm_model", ""),
            "dataset": dataset,
        },
    }
    try:
        _rotate_log_if_oversized(_STATE_DIR / "watcher.log")  # the child writes it
        log_fh = (_STATE_DIR / "watcher.log").open("a", encoding="utf-8")
    except Exception:
        log_fh = subprocess.DEVNULL
    try:
        env = os.environ.copy()
        env["COGNEE_SESSION_KEY"] = host_key
        subprocess.Popen(
            [sys.executable, str(_WATCHER_SCRIPT), json.dumps(bootstrap)],
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=log_fh,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        hook_log("switch_watcher_restarted", {"session": session_id, "dataset": dataset})
    except Exception as exc:
        hook_log("switch_watcher_restart_failed", {"error": str(exc)[:200]})


def _switch(host_key: str, rec: dict, target: str, *, force: bool) -> dict:
    target = target.strip()
    if not target:
        raise SwitchError(EXIT_ERROR, "dataset name is empty")
    old_session = str(rec.get("session_id") or "")
    old_dataset = str(
        rec.get("dataset") or load_resolved(session_key=host_key).get("dataset") or ""
    )
    old_conn = str(rec.get("conn_uuid") or "")
    if target == old_dataset:
        return {
            "switched": False,
            "reason": "already_active",
            "dataset": target,
            "session_id": old_session,
        }

    resolved = load_resolved(session_key=host_key)
    user_id = str(resolved.get("user_id") or "")

    # The target must be one this principal can write to. Owned datasets are
    # guaranteed writable; anything else is refused up front rather than failing
    # half-way through.
    listing = list_writable_datasets(user_id)
    from _dataset_access import dataset_id

    matches = [row for row in listing["datasets"] if target in (row["id"], row["name"])]
    if len(matches) > 1:
        raise SwitchError(EXIT_NOT_WRITABLE, "Dataset name is ambiguous; select its UUID")
    if (not matches and target in listing["readonly"]) or (dataset_id(target) and not matches):
        raise SwitchError(EXIT_NOT_WRITABLE, "Selected dataset is not writable")
    if matches:
        row = matches[0]
        target = row["id"] if row["owner_id"] != user_id else row["name"]
        if row["writable"] is not True:
            raise SwitchError(EXIT_NOT_WRITABLE, "Write permission could not be verified")

    if dataset_id(target):
        from _plugin_common import require_typed_dataset_id_support

        require_typed_dataset_id_support()

    # 1. Sync the session we are leaving. Abort on failure unless forced — the
    #    retired triple stays in `touched`, so the final sync retries it.
    sync_ok = True
    sync_error = ""
    try:
        _sync_current(host_key, old_session, old_dataset)
    except SwitchError as exc:
        if not force:
            raise
        sync_ok, sync_error = False, str(exc)
        hook_log("switch_sync_forced_past_failure", {"error": sync_error[:300]})

    # 2. Make sure the dataset exists for this principal (idempotent).
    _ensure_dataset(target)

    # 3. Register the new session first (fresh handle) ...
    new_session = mint_switch_session_id(host_key)
    new_conn = _register_new(new_session, target)

    # 4. ... repoint the launch record (atomic) ...
    try:
        switch_launch_record(host_key, session_id=new_session, dataset=target, conn_uuid=new_conn)
    except Exception:
        try:
            released, _ = unregister_agent_via_http(agent_session_name=new_conn)
            hook_log("switch_aborted_handle_cleanup", {"conn_uuid": new_conn, "ok": released})
        except Exception as cleanup_error:
            hook_log("switch_aborted_handle_cleanup_failed", {"error": str(cleanup_error)[:200]})
        raise

    # 5. ... then release the old handle. Best-effort: a lingering active
    #    connection is harmless and the final unregister sweeps `touched`.
    old_released = False
    if old_conn:
        old_released, _ = unregister_agent_via_http(agent_session_name=old_conn)
        hook_log("switch_old_handle_released", {"conn_uuid": old_conn, "ok": old_released})

    # 6. Fresh watcher + reset the idle clock for the new session.
    _restart_idle_watcher(host_key, new_session, target, user_id)
    touch_activity()

    return {
        "switched": True,
        "dataset": target,
        "session_id": new_session,
        "conn_uuid": new_conn,
        "previous": {
            "dataset": old_dataset,
            "session_id": old_session,
            "synced": sync_ok,
            **({"sync_error": sync_error} if sync_error else {}),
            "unregistered": old_released,
        },
    }


# ── CLI ────────────────────────────────────────────────────────────────────


def _print_listing(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result))
        return
    print(f"Current dataset: {result['current']}  (session {result['session_id']})")
    if not result["datasets"]:
        print("No other writable datasets found.")
    for row in result["datasets"]:
        mark = "*" if row["current"] else " "
        print(f" {mark} {row['name']}")
    if result["hidden_readonly"]:
        print(f"({result['hidden_readonly']} read-only dataset(s) not shown)")
    if not result["filtered"]:
        print("(write access could not be verified — showing every readable dataset)")


def _print_switch(result: dict, as_json: bool) -> None:
    if as_json:
        print(json.dumps(result))
        return
    if not result.get("switched"):
        print(f"Already on dataset {result['dataset']!r} (session {result['session_id']}).")
        return
    prev = result["previous"]
    synced = "synced" if prev["synced"] else f"NOT synced ({prev.get('sync_error', '')})"
    print(
        f"Switched to dataset {result['dataset']!r} — new session {result['session_id']}. "
        f"Previous session {prev['session_id']} ({prev['dataset']}) {synced}."
    )


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    force = "--force" in args
    do_list = "--list" in args
    explicit_key = ""
    if "--session-key" in args:
        i = args.index("--session-key")
        explicit_key = args[i + 1] if i + 1 < len(args) else ""
        del args[i : i + 2]
    positional = [a for a in args if not a.startswith("--")]

    try:
        host_key, rec = _resolve_launch(explicit_key)
        if do_list or not positional:
            _print_listing(_list(host_key, rec), as_json)
            return EXIT_OK
        result = _switch(host_key, rec, positional[0], force=force)
        _print_switch(result, as_json)
        return EXIT_OK
    except SwitchError as exc:
        hook_log("switch_failed", {"code": exc.code, "error": str(exc)[:300], **exc.detail})
        if as_json:
            print(json.dumps({"error": str(exc), "code": exc.code, **exc.detail}))
        else:
            print(f"cognee-switch-datasets: {exc}", file=sys.stderr)
        return exc.code
    except Exception as exc:  # pragma: no cover - defensive
        hook_log("switch_crashed", {"error": str(exc)[:300]})
        if as_json:
            print(json.dumps({"error": str(exc), "code": EXIT_ERROR}))
        else:
            print(f"cognee-switch-datasets: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
