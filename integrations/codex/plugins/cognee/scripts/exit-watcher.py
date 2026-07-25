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

sys.path.insert(0, os.path.dirname(__file__))
from _proc import pid_alive as _pid_alive

_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "codex"
_EXIT_WATCHERS_DIR = _PLUGIN_DIR / "exit-watchers"
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


def _owns_pidfile(pidfile: Path) -> bool:
    try:
        return int(pidfile.read_text(encoding="utf-8").strip()) == os.getpid()
    except Exception as exc:
        _log("pidfile_read_failed", pidfile=str(pidfile), error=str(exc)[:200])
        return False


def _spawn_sync(
    session_id: str,
    dataset: str,
    *,
    session_key: str = "",
    agent_session_name: str = "",
    api_key: str = "",
    service_url: str = "",
) -> None:
    try:
        env = os.environ.copy()
        env.setdefault("COGNEE_SYNC_START_DELAY", str(_SYNC_START_DELAY))
        env["COGNEE_UNREGISTER_ON_FINISH"] = "1"
        if session_id:
            env["COGNEE_SYNC_SESSION_ID"] = session_id
        if dataset:
            env["COGNEE_SYNC_DATASET"] = dataset
        if session_key:
            env["COGNEE_SESSION_KEY"] = session_key
        if agent_session_name:
            env["COGNEE_AGENT_SESSION_NAME"] = agent_session_name
        if api_key:
            env["COGNEE_API_KEY"] = api_key
        if service_url:
            env["COGNEE_BASE_URL"] = service_url
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
    dataset = str(bootstrap.get("dataset") or "agent_sessions")
    session_key = str(bootstrap.get("session_key") or "")
    agent_session_name = str(bootstrap.get("agent_session_name") or "")
    api_key = str(bootstrap.get("api_key") or "")
    service_url = str(bootstrap.get("base_url") or "")
    pidfile_raw = str(bootstrap.get("pidfile") or "").strip()
    pidfile = Path(pidfile_raw) if pidfile_raw else _PIDFILE
    if not parent_pid:
        _log("fatal_no_parent_pid", session=session_id, dataset=dataset)
        return

    try:
        _PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        _EXIT_WATCHERS_DIR.mkdir(parents=True, exist_ok=True)

        if pidfile.exists():
            try:
                existing = int(pidfile.read_text(encoding="utf-8").strip())
                if _pid_alive(existing):
                    _log(
                        "already_running_for_parent",
                        parent_pid=parent_pid,
                        session=session_id,
                        dataset=dataset,
                        pidfile=str(pidfile),
                        existing_pid=existing,
                    )
                    return
            except Exception:
                pass

        pidfile.write_text(str(os.getpid()), encoding="utf-8")
    except Exception as exc:
        _log("pidfile_write_failed", pidfile=str(pidfile), error=str(exc)[:200])
        return

    _log(
        "started",
        parent_pid=parent_pid,
        session=session_id,
        dataset=dataset,
        pidfile=str(pidfile),
    )
    while _owns_pidfile(pidfile) and _pid_alive(parent_pid):
        time.sleep(_POLL_SECONDS)

    if not _owns_pidfile(pidfile):
        _log("pidfile_replaced", parent_pid=parent_pid, pidfile=str(pidfile))
        return

    _log("parent_exited", parent_pid=parent_pid, session=session_id, dataset=dataset)
    _spawn_sync(
        session_id,
        dataset,
        session_key=session_key,
        agent_session_name=agent_session_name,
        api_key=api_key,
        service_url=service_url,
    )

    try:
        if _owns_pidfile(pidfile):
            pidfile.unlink()
    except Exception as exc:
        _log("pidfile_unlink_failed", pidfile=str(pidfile), error=str(exc)[:200])
    _log("exiting", parent_pid=parent_pid, pidfile=str(pidfile))


if __name__ == "__main__":
    main()
