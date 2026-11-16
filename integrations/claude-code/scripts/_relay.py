"""The relay: how a script run from a shell reaches Cognee without holding a key.

The plugin's keys are plugin options. Claude Code hands them to the plugin's
hooks and to what those hooks start, and to nothing else: a script that a skill
runs through the Bash tool has no key. So a hook starts this relay, a small
process that stays up while sessions are in use. A skill's script hands its
command line to the relay, the relay runs that same script as its own child
(which has the options), and sends the output back. The key never leaves the
processes Claude Code gave it to, and is never written to disk.

What the relay accepts:

* only connections on a Unix socket in a directory only the user can enter
  (``~/.cognee-plugin/claude-code/relay``);
* only the plugin's own scripts named in ``RELAYED``, never a path or a command.

The relay starts only when the Cognee API key option is set. Without it (local
mode) the scripts need no relay: the local server's key is cached on disk.
Where Unix sockets do not exist (Windows before Python 3.13 added them), the
relay does not start, and a script run from a shell says that it has no key.

Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
_DIR = Path.home() / ".cognee-plugin" / "claude-code" / "relay"
_SOCKET = _DIR / "relay.sock"
_STATE = _DIR / "relay.json"
_HEARTBEAT = _DIR / "heartbeat"
_LOG = _DIR / "relay.log"

#: The scripts a skill may run through the relay, by file name.
RELAYED = frozenset(
    {
        "cognee_search.py",
        "cognee_remember.py",
        "cognee-forget.sh",
        "cognee-index-repo.sh",
        "cognee-recap.py",
        "cognee-evidence.py",
        "switch-dataset.py",
        "list-datasets.py",
        "plugin-keys.py",
        "doctor.py",
        "sync-session-to-graph.py",
    }
)
#: Set in a relayed script's environment, so it never hands itself over again.
RELAYED_VAR = "COGNEE_RELAYED"
#: Exit code of ``_relay.py run`` when there is no relay: carry on in place.
NO_RELAY = 111
#: The relay stops once no hook has asked for it for this long.
IDLE_SECONDS = 3600.0
_RUN_TIMEOUT_SECONDS = 1800.0
_MAX_REQUEST_BYTES = 8 * 1024 * 1024
#: What the relayed script takes from the caller's shell instead of the
#: relay's own: the session, and the settings one terminal can set for itself
#: (the mode switch, the server address, the dataset). Never a key.
PASSED = (
    "COGNEE_SESSION_REF",
    "CLAUDE_CODE_SESSION_ID",
    "COGNEE_BACKEND",
    "COGNEE_CLAUDE_BACKEND",
    "COGNEE_BASE_URL",
    "COGNEE_LOCAL_API_URL",
    "COGNEE_PLUGIN_DATASET",
    "COGNEE_ENV_FILE",
)


def _supported() -> bool:
    return hasattr(socket, "AF_UNIX")


def _in_relay_dir(action):
    """Run ``action("relay.sock")`` from inside the relay's directory.

    A Unix socket's path may be about 100 bytes at most, and a home directory
    can be longer than that. Naming the socket by its file name from inside
    its directory has no such limit.
    """
    before = os.getcwd()
    os.chdir(str(_DIR))
    try:
        return action(_SOCKET.name)
    finally:
        os.chdir(before)


def _key_option() -> str:
    from _env_file import cloud_key_option

    return cloud_key_option()


def _fingerprint() -> str:
    """Changes when the key option or the installed plugin does, so that the
    running relay is replaced. A hash: the key itself is not stored."""
    text = "\n".join((_key_option(), str(_SCRIPTS), sys.executable))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read_state() -> dict:
    try:
        data = json.loads(_STATE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _trace(message: str) -> None:
    try:
        with _LOG.open("a", encoding="utf-8") as stream:
            stream.write("%s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), message))
    except Exception:
        pass


def _alive(pid: int) -> bool:
    try:
        from _proc import pid_alive

        return bool(pid) and pid_alive(pid)
    except Exception:
        return False


# ------------------------------------------------------------ the hooks' side


def ensure_relay() -> None:
    """Start the relay if this process has the key option and none is running.

    Called by the hooks (session start, each prompt). Cheap when the relay is
    up: one small file read and one file touch. Never raises.
    """
    try:
        if not _supported() or not _key_option():
            return
        # The switch that keeps every background helper off (tests, CI).
        if os.environ.get("COGNEE_IDLE_DISABLED", "").lower() in {"1", "true", "yes"}:
            return
        _DIR.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(_DIR, 0o700)
        _HEARTBEAT.write_text(str(time.time()), encoding="utf-8")
        state = _read_state()
        pid = int(state.get("pid") or 0)
        if _alive(pid):
            if state.get("fingerprint") == _fingerprint():
                return
            # The key option or the plugin changed: the old relay must not go
            # on answering with what it was started with.
            try:
                os.kill(pid, 15)
            except Exception:
                pass
        kwargs = (
            {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
            if os.name == "nt"
            else {"start_new_session": True}
        )
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **kwargs,
        )
    except Exception:
        pass


# ---------------------------------------------------------------- the relay


def _child_env(extra: dict) -> dict:
    """The relay's environment (which holds the options), with the caller's
    values for the names in ``PASSED``: set when the caller has one, removed
    when it does not, so one terminal's mode never leaks into another's."""
    env = os.environ.copy()
    for name in PASSED:
        value = (extra or {}).get(name)
        if isinstance(value, str) and value:
            env[name] = value
        else:
            env.pop(name, None)
    env[RELAYED_VAR] = "1"
    return env


def _run(request: dict) -> dict:
    script = str(request.get("script") or "")
    if script not in RELAYED or not (_SCRIPTS / script).is_file():
        return {"code": 2, "stderr": "the relay does not run %r\n" % script}
    argv = [str(a) for a in (request.get("argv") or [])]
    head = ["bash"] if script.endswith(".sh") else [sys.executable]
    cwd = str(request.get("cwd") or "")
    try:
        ran = subprocess.run(
            [*head, str(_SCRIPTS / script), *argv],
            cwd=cwd if cwd and os.path.isdir(cwd) else None,
            env=_child_env(request.get("env") or {}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=_RUN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {"code": 124, "stderr": "the relayed script ran too long and was stopped\n"}
    except Exception as exc:
        return {"code": 1, "stderr": "the relay could not run the script: %s\n" % str(exc)[:200]}
    return {
        "code": ran.returncode,
        "stdout_b64": base64.b64encode(ran.stdout).decode("ascii"),
        "stderr_b64": base64.b64encode(ran.stderr).decode("ascii"),
    }


def _read_line(conn: socket.socket) -> bytes:
    chunks = []
    size = 0
    while True:
        chunk = conn.recv(65536)
        if not chunk:
            break
        chunks.append(chunk)
        size += len(chunk)
        if b"\n" in chunk or size > _MAX_REQUEST_BYTES:
            break
    return b"".join(chunks).split(b"\n", 1)[0]


def _answer(conn: socket.socket) -> None:
    try:
        with conn:
            try:
                request = json.loads(_read_line(conn).decode("utf-8"))
            except ValueError:
                request = {}
            reply = _run(request if isinstance(request, dict) else {})
            _trace("ran %s code=%s" % (request.get("script"), reply.get("code")))
            conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
    except Exception as exc:
        _trace("request failed: %s" % str(exc)[:200])


def serve() -> int:
    if not _supported() or not _key_option():
        return 1
    _DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_DIR, 0o700)
    state = _read_state()
    if _alive(int(state.get("pid") or 0)) and state.get("fingerprint") == _fingerprint():
        return 0
    try:
        _SOCKET.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    _in_relay_dir(server.bind)
    os.chmod(_SOCKET, 0o600)
    server.listen(16)
    server.settimeout(30.0)
    mine = {"pid": os.getpid(), "fingerprint": _fingerprint(), "started": time.time()}
    _STATE.write_text(json.dumps(mine), encoding="utf-8")
    _trace("started pid=%d" % os.getpid())
    # A stop request ends the loop below, so the socket and state are removed.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        while True:
            try:
                conn, _ = server.accept()
            except socket.timeout:
                if _read_state().get("pid") != os.getpid():
                    _trace("replaced, stopping")
                    return 0
                try:
                    idle = time.time() - _HEARTBEAT.stat().st_mtime
                except OSError:
                    idle = IDLE_SECONDS + 1
                if idle > IDLE_SECONDS:
                    _trace("idle, stopping")
                    return 0
                continue
            conn.settimeout(None)
            threading.Thread(target=_answer, args=(conn,), daemon=True).start()
    finally:
        server.close()
        if _read_state().get("pid") == os.getpid():
            for path in (_SOCKET, _STATE):
                try:
                    path.unlink()
                except OSError:
                    pass


# ----------------------------------------------------------- the scripts' side


def _session_ref() -> str:
    """The session this shell belongs to, worked out here: the relayed script
    runs under the relay, where the host's process cannot be found."""
    if os.environ.get("COGNEE_SESSION_REF", "").strip():
        return os.environ["COGNEE_SESSION_REF"].strip()
    previous = os.environ.get("COGNEE_PLUGIN_IN_VENV")
    os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
    try:
        import _plugin_common

        return str(_plugin_common.resolve_host_key_outside_hook()[0] or "")
    except Exception:
        return ""
    finally:
        if previous is None:
            os.environ.pop("COGNEE_PLUGIN_IN_VENV", None)
        else:
            os.environ["COGNEE_PLUGIN_IN_VENV"] = previous


# The shell's own exports, read before ~/.cognee/.env is loaded into the
# environment: the relayed script loads that file itself.
_SHELL = {
    "COGNEE_BASE_URL": os.environ.get("COGNEE_BASE_URL", ""),
    "COGNEE_LOCAL_API_URL": os.environ.get("COGNEE_LOCAL_API_URL", ""),
    "COGNEE_PLUGIN_DATASET": os.environ.get("COGNEE_PLUGIN_DATASET", ""),
}


def _shell_value(name: str) -> str:
    return _SHELL.get(name, "")


def relay(script: str, argv: list) -> int | None:
    """Run ``script`` through the relay and print what it printed.

    Returns its exit code, or None when this process should run the script
    itself: it already has the key option (a hook, or a relayed run), or no
    relay is up.
    """
    if os.environ.get("COGNEE_RELAYED", "") == "1" or _key_option() or not _supported():
        return None
    if not _SOCKET.exists():
        return None
    try:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(5.0)
        cwd = os.getcwd()
        _in_relay_dir(conn.connect)
    except OSError:
        return None
    env = {
        "COGNEE_SESSION_REF": _session_ref(),
        "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "COGNEE_BACKEND": os.environ.get("COGNEE_BACKEND", ""),
        "COGNEE_CLAUDE_BACKEND": os.environ.get("COGNEE_CLAUDE_BACKEND", ""),
        "COGNEE_BASE_URL": _shell_value("COGNEE_BASE_URL"),
        "COGNEE_LOCAL_API_URL": _shell_value("COGNEE_LOCAL_API_URL"),
        "COGNEE_PLUGIN_DATASET": _shell_value("COGNEE_PLUGIN_DATASET"),
        "COGNEE_ENV_FILE": os.environ.get("COGNEE_ENV_FILE", ""),
    }
    request = {"script": script, "argv": list(argv), "cwd": cwd, "env": env}
    try:
        with conn:
            conn.settimeout(None)
            conn.sendall(json.dumps(request).encode("utf-8") + b"\n")
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        reply = json.loads(b"".join(chunks).decode("utf-8"))
    except Exception as exc:
        sys.stderr.write("Cognee: the relay did not answer (%s)\n" % str(exc)[:200])
        return 1
    sys.stdout.buffer.write(base64.b64decode(reply.get("stdout_b64") or ""))
    sys.stdout.flush()
    sys.stderr.buffer.write(base64.b64decode(reply.get("stderr_b64") or ""))
    sys.stderr.write(str(reply.get("stderr") or ""))
    sys.stderr.flush()
    return int(reply.get("code") or 0)


def hand_over(script_file: str) -> None:
    """First call of a script a skill runs: when a relay is up and this process
    has no key option, the relay runs the script and this process exits with
    its result. Otherwise returns, and the script runs here."""
    code = relay(Path(script_file).name, sys.argv[1:])
    if code is not None:
        sys.exit(code)


def main(argv: list) -> int:
    if len(argv) >= 2 and argv[1] == "serve":
        return serve()
    if len(argv) >= 3 and argv[1] == "run":
        code = relay(argv[2], argv[3:])
        return NO_RELAY if code is None else code
    sys.stderr.write("usage: _relay.py serve | run <script> [args...]\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
