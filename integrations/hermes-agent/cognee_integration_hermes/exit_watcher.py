"""Crash insurance for a Hermes session: improve-then-unregister on unclean death.

The provider already closes a session properly — ``on_session_end`` bridges the
session cache into the graph and ``shutdown`` unregisters the agent connection.
But none of that runs when Hermes crashes or is killed, and then two things leak:
the agent registration (so the server's ``COGNEE_AGENT_MODE`` idle watchdog never
sees zero agents and the server lingers forever) and the session's turns (nothing
ever promotes them into the permanent graph).

So ``arm()`` spawns this file as a small detached process — the same pattern the
claude-code, codex and openclaw plugins use — that polls the Hermes PID and, when
it dies without a clean shutdown, runs ``improve(session_ids=[...])``
synchronously and then unregisters. A clean shutdown ``disarm()``s the watcher by
deleting its state file, and the watcher exits without acting.

The watcher half of this module runs as a plain script (``python exit_watcher.py
--state <path>``), outside the package, so nothing here may import from the
package — everything it needs travels in the state file, plus the API key, which
stays out of world-readable places: it is handed over via the child's
environment and falls back to the shared ``~/.cognee-plugin/api_key.json`` cache.
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

# Duplicated from config.SHARED_PLUGIN_STATE_DIR (this file must run without the
# package): the key cache shared with the other cognee plugins.
_SHARED_KEY_CACHE = Path.home() / ".cognee-plugin" / "api_key.json"

_DEFAULT_POLL_SECONDS = 2.0
_UNREGISTER_TIMEOUT = 15.0
_WINDOWS = os.name == "nt"


# --- state file ---------------------------------------------------------------


def read_state(state_path):
    """The parsed state dict, or None when missing/unreadable (i.e. disarmed)."""
    try:
        data = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def write_state(state_path, state):
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    os.replace(tmp, path)


def pid_alive(pid):
    """True when *pid* is a live process.

    POSIX probes with signal 0, which touches nothing. Windows MUST NOT go
    through ``os.kill``: CPython implements non-console signals there as
    ``OpenProcess`` + ``TerminateProcess`` — "probing" would kill the very
    Hermes this watcher is guarding — so it queries the process handle instead.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        # POSIX gives pid<=0 group/broadcast semantics; never treat those as
        # "a process worth waiting on".
        return False
    if _WINDOWS:
        return _pid_alive_windows(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False
    return True


def _pid_alive_windows(pid):
    import ctypes
    from ctypes import wintypes

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    ERROR_ACCESS_DENIED = 5

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied means the pid exists but belongs to someone else.
            return ctypes.get_last_error() == ERROR_ACCESS_DENIED
        try:
            exit_code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            # Documented Windows ambiguity: a process that exited with the
            # literal code 259 reads as alive. Acceptable for a 2s poll —
            # the watcher just fires one poll later than it could have.
            return exit_code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        # An identity we cannot determine is treated as alive: firing improve/
        # unregister against a session that is still running is worse than
        # firing late.
        return True


# --- the provider-facing API ---------------------------------------------------


def arm(
    *,
    state_path,
    log_path,
    parent_pid,
    url,
    api_key,
    agent_session_name,
    dataset,
    session_id,
    improve,
    improve_timeout,
    poll_interval=_DEFAULT_POLL_SECONDS,
):
    """Write the watcher's state file and spawn the detached watcher process.

    The state file deliberately carries no API key — the key goes through the
    child's environment instead, and the watcher can re-read the shared cache at
    fire time (the key may not even exist yet when Hermes starts).
    """
    write_state(
        state_path,
        {
            "parent_pid": int(parent_pid),
            "url": str(url),
            "agent_session_name": str(agent_session_name),
            "dataset": str(dataset),
            "session_id": str(session_id),
            "improve": bool(improve),
            "improve_timeout": float(improve_timeout),
            "poll_interval": float(poll_interval),
        },
    )
    env = dict(os.environ)
    if api_key:
        env["COGNEE_API_KEY"] = str(api_key)
    try:
        log = open(log_path, "ab", buffering=0)  # noqa: SIM115 — handed to the child
    except Exception:
        log = subprocess.DEVNULL
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "--state", str(state_path)],
            env=env,
            stdout=log,
            stderr=log,
            start_new_session=True,  # detach: must outlive Hermes to be of any use
        )
    finally:
        if log is not subprocess.DEVNULL:
            log.close()


def update(state_path, **fields):
    """Merge *fields* into the state file (e.g. a new session id on switch).

    Best-effort and never raises: the watcher is insurance, and no memory
    operation should fail because insurance paperwork did.
    """
    try:
        state = read_state(state_path)
        if state is None:
            return
        state.update(fields)
        write_state(state_path, state)
    except Exception as exc:
        logger.debug("could not update the cognee exit watcher state: %s", exc)


def disarm(state_path):
    """Delete the state file; the watcher notices and exits without acting."""
    try:
        Path(state_path).unlink(missing_ok=True)
    except Exception as exc:
        logger.debug("could not disarm the cognee exit watcher: %s", exc)


# --- the watcher process --------------------------------------------------------


def _log(message):
    print(time.strftime("[%Y-%m-%d %H:%M:%S] ") + message, flush=True)


def _cached_api_key(url):
    """The shared single-principal key, matching the other plugins' cache rules."""
    try:
        data = json.loads(_SHARED_KEY_CACHE.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    key = str(data.get("api_key") or "").strip()
    cached_url = str(data.get("base_url") or "").strip().rstrip("/")
    wanted = str(url or "").strip().rstrip("/")
    if wanted and cached_url and cached_url != wanted:
        return ""
    return key


def _post(url, path, payload, *, api_key, timeout):
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-Api-Key"] = api_key
    request = urllib.request.Request(
        url.rstrip("/") + path,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return getattr(response, "status", None) or response.getcode()


def fire(state):
    """The unclean-death path: improve first, then unregister.

    That order is load-bearing (and matches openclaw's exit watcher): the improve
    must finish while this connection still counts as a registered agent, or the
    server's idle watchdog could tear the server down mid-promotion.
    """
    url = str(state.get("url") or "")
    api_key = os.environ.get("COGNEE_API_KEY", "").strip() or _cached_api_key(url)
    session_id = str(state.get("session_id") or "")
    if state.get("improve") and session_id:
        try:
            _post(
                url,
                "/api/v1/improve",
                {
                    "dataset_name": str(state.get("dataset") or ""),
                    "session_ids": [session_id],
                    "run_in_background": False,
                },
                api_key=api_key,
                timeout=float(state.get("improve_timeout") or 300.0),
            )
            _log("improve submitted for session %s" % session_id)
        except Exception as exc:
            _log("improve failed for session %s: %s" % (session_id, exc))
    try:
        _post(
            url,
            "/api/v1/agents/unregister",
            {"agent_session_name": str(state.get("agent_session_name") or "hermes")},
            api_key=api_key,
            timeout=_UNREGISTER_TIMEOUT,
        )
        _log("agent connection unregistered")
    except Exception as exc:
        _log("unregister failed: %s" % exc)


def watch(state_path):
    """Poll until disarmed, superseded, or the parent dies (then act)."""
    state = read_state(state_path)
    if state is None:
        return 0
    # Claim the state file. A later arm() for the same path overwrites this, and
    # the superseded watcher sees a foreign pid below and bows out.
    state["watcher_pid"] = os.getpid()
    write_state(state_path, state)
    _log(
        "watching hermes pid %s for session %s" % (state.get("parent_pid"), state.get("session_id"))
    )
    while True:
        state = read_state(state_path)
        if state is None:
            _log("disarmed (clean shutdown); exiting")
            return 0
        if state.get("watcher_pid") != os.getpid():
            _log("superseded by a newer watcher; exiting")
            return 0
        if not pid_alive(state.get("parent_pid", -1)):
            _log("hermes pid %s is gone; closing the session" % state.get("parent_pid"))
            fire(state)
            disarm(state_path)
            return 0
        time.sleep(float(state.get("poll_interval") or _DEFAULT_POLL_SECONDS))


def main(argv=None):
    parser = argparse.ArgumentParser(description="cognee-hermes exit watcher")
    parser.add_argument("--state", required=True)
    args = parser.parse_args(argv)
    return watch(args.state)


if __name__ == "__main__":
    sys.exit(main())
