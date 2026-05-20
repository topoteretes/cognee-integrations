#!/usr/bin/env python3
"""Run Cognee graph sync after the owning Codex process exits.

Codex CLI currently may not invoke plugin SessionEnd on normal shutdown.
SessionStart launches this watcher with the hook parent PID. The watcher
does nothing while Codex is alive; once that PID disappears, it starts the
normal detached graph sync worker and exits.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
_PIDFILE = _PLUGIN_DIR / "exit-watcher.pid"
_LOGFILE = _PLUGIN_DIR / "exit-watcher.log"
_SYNC_SCRIPT = Path(__file__).with_name("sync-session-to-graph.py")
_DETACHED_SYNC_ARG = "--detached-final"
_POLL_SECONDS = 2.0
_SYNC_START_DELAY = 2.0


def _log(event: str, **detail) -> None:
    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        line = {"ts": time.time(), "pid": os.getpid(), "event": event}
        if detail:
            line["detail"] = detail
        with _LOGFILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception as exc:
        _log("pid_alive_check_failed", parent_pid=pid, error=str(exc)[:200])
        return False


def _owns_pidfile() -> bool:
    try:
        return int(_PIDFILE.read_text(encoding="utf-8").strip()) == os.getpid()
    except Exception as exc:
        _log("pidfile_read_failed", error=str(exc)[:200])
        return False


def _spawn_sync(session_id: str, dataset: str) -> None:
    try:
        env = os.environ.copy()
        env.setdefault("COGNEE_SYNC_START_DELAY", str(_SYNC_START_DELAY))
        subprocess.Popen(
            [sys.executable, str(_SYNC_SCRIPT), _DETACHED_SYNC_ARG],
            cwd=os.getcwd(),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        _log("exit_sync_deferred", session=session_id, dataset=dataset)
    except Exception as exc:
        _log("exit_sync_detach_failed", error=str(exc)[:300])


def main() -> None:
    if len(sys.argv) < 2:
        _log("fatal_missing_args")
        return
    try:
        bootstrap = json.loads(sys.argv[1])
    except Exception as exc:
        _log("fatal_bad_args", error=str(exc)[:200])
        return

    parent_pid = int(bootstrap.get("parent_pid") or 0)
    session_id = str(bootstrap.get("session_id") or "")
    dataset = str(bootstrap.get("dataset") or "codex_sessions")
    if not parent_pid:
        _log("fatal_no_parent_pid", session=session_id, dataset=dataset)
        return

    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _PIDFILE.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        _log("pidfile_write_failed", error=str(exc)[:200])
        return

    _log("started", parent_pid=parent_pid, session=session_id, dataset=dataset)
    while _owns_pidfile() and _pid_alive(parent_pid):
        time.sleep(_POLL_SECONDS)

    if not _owns_pidfile():
        _log("pidfile_replaced", parent_pid=parent_pid)
        return

    _log("parent_exited", parent_pid=parent_pid, session=session_id, dataset=dataset)
    _spawn_sync(session_id, dataset)

    try:
        if _owns_pidfile():
            _PIDFILE.unlink()
    except Exception as exc:
        _log("pidfile_unlink_failed", error=str(exc)[:200])
    _log("exiting")


if __name__ == "__main__":
    main()
