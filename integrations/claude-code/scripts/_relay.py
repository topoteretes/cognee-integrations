"""The relay: how a script run from a shell reaches Cognee without holding a key.

The plugin's keys are plugin options. The plugin's hooks, and what those hooks
start, read them from the options holder of their session (``_holder.py``); a
script that a skill runs through the Bash tool is neither and has no key. So a
hook starts this relay, a small process that stays up while sessions are in
use. A skill's script hands its command line to the relay, together with the
holder of the Claude Code session it runs in; the relay runs that same script
as its own child, pointed at that holder, and sends the output back.

The relay holds no key itself, so one relay serves every open session, each
with its own options: two sessions never replace each other's relay.

What the relay accepts:

* only connections on a Unix socket in a directory only the user can enter
  (``~/.cognee-plugin/claude-code/relay``);
* only the plugin's own scripts named in ``RELAYED``, never a path or a command.

Where Unix sockets do not exist (Windows), the relay listens on a loopback
port instead and asks every request for a one-time code. The code is random,
made when the relay starts, kept in a file only the user can read
(``relay.json``) and worthless once the relay stops. It is not one of the
user's keys and opens nothing but this relay.

The relay starts only when the Cognee API key option is set. Without it (local
mode) the scripts need no relay: the local server's key is cached on disk.

Stdlib only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
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
#: (the mode switch, the server address, the dataset). Never a key, and never
#: the server the key goes to: a caller's address that differs from the
#: relay's (``KEY_URL_VAR``) gets a child without the key option.
PASSED = (
    "COGNEE_SESSION_REF",
    "CLAUDE_CODE_SESSION_ID",
    "COGNEE_BACKEND",
    "COGNEE_CLAUDE_BACKEND",
    "COGNEE_BASE_URL",
    "COGNEE_LOCAL_API_URL",
    "COGNEE_PLUGIN_DATASET",
)
#: Set in a relayed script's environment, after the caller's values: the server
#: the relay resolved from the options. The Cognee API key option is sent to
#: this address only (``_plugin_common.user_cloud_key``).
KEY_URL_VAR = "COGNEE_RELAY_KEY_URL"
_DEFAULT_URL = "http://localhost:8011"


def _on_port() -> bool:
    """Whether the relay listens on a loopback port (no Unix sockets here)."""
    return not hasattr(socket, "AF_UNIX") or os.environ.get("COGNEE_RELAY_PORT_MODE", "") == "1"


def _write_private(path: Path, text: str) -> None:
    """Write a file only the user can read."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(str(path), flags, 0o600), "w", encoding="utf-8") as stream:
        stream.write(text)


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


def _normalized(url: str) -> str:
    return str(url or "").strip().rstrip("/")


def _bound_url() -> str:
    """The server this process resolved, the way ``_plugin_common`` does: the
    hook that starts the relay has loaded the options and ~/.cognee/.env, and
    the relay inherits that environment."""
    return _normalized(
        os.environ.get("COGNEE_LOCAL_API_URL", "").strip()
        or os.environ.get("COGNEE_BASE_URL", "").strip()
        or _DEFAULT_URL
    )


def _fingerprint() -> str:
    """Changes when the server the key is for, or the plugin's code, does, so
    that the running relay is replaced and never answers with what it was
    started with. Neither the key nor the session's holder is part of it: each
    relayed script reads its own session's options, so every session with the
    same server shares one relay."""
    try:
        from _version import PLUGIN_VERSION as version
    except Exception:
        version = ""
    try:
        changed = str(os.path.getmtime(os.path.abspath(__file__)))
    except OSError:
        changed = ""
    parts = (_bound_url(), str(_SCRIPTS), sys.executable, version, changed)
    text = "\n".join(parts)
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
        if not _key_option():
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
            # The server or the plugin changed: the old relay must not go on
            # answering with what it was started with.
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


def _child_env(extra: dict, holder: str = "") -> dict:
    """The relay's environment, with the caller's values for the names in
    ``PASSED``: set when the caller has one, removed when it does not, so one
    terminal's mode never leaks into another's.

    ``holder`` is the options holder of the caller's Claude Code session; the
    child reads its options there. Only a live record in the holder folder is
    taken. Without one the child keeps the relay's own, which is the holder of
    the session that started the relay, or none.
    """
    import _options
    from _proc import child_env

    env = child_env()
    env.pop("COGNEE_OPTIONS_FD", None)
    if holder and _options.is_record(holder):
        env["COGNEE_OPTIONS_HOLDER"] = holder
    for name in PASSED:
        value = (extra or {}).get(name)
        if isinstance(value, str) and value:
            env[name] = value
        else:
            env.pop(name, None)
    bound = _bound_url()
    # The caller may aim this run at another server (a local one, say), but the
    # key option is for the relay's server only.
    asked = _normalized(env.get("COGNEE_LOCAL_API_URL") or env.get("COGNEE_BASE_URL") or "")
    if asked and asked != bound:
        env["COGNEE_RELAY_OTHER_SERVER"] = "1"
    else:
        env.pop("COGNEE_RELAY_OTHER_SERVER", None)
    env[KEY_URL_VAR] = bound
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
            env=_child_env(request.get("settings") or {}, str(request.get("holder") or "")),
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


def _answer(conn: socket.socket, code: str = "") -> None:
    try:
        with conn:
            try:
                request = json.loads(_read_line(conn).decode("utf-8"))
            except ValueError:
                request = {}
            request = request if isinstance(request, dict) else {}
            if code and not hmac.compare_digest(str(request.get("code") or ""), code):
                reply = {"code": 2, "stderr": "the relay refused the request\n"}
            else:
                reply = _run(request)
            _trace("ran %s code=%s" % (request.get("script"), reply.get("code")))
            conn.sendall(json.dumps(reply).encode("utf-8") + b"\n")
    except Exception as exc:
        _trace("request failed: %s" % str(exc)[:200])


def serve() -> int:
    if not _key_option():
        return 1
    _DIR.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(_DIR, 0o700)
    state = _read_state()
    if _alive(int(state.get("pid") or 0)) and state.get("fingerprint") == _fingerprint():
        return 0
    try:
        _SOCKET.unlink()
    except FileNotFoundError:
        pass
    mine = {"pid": os.getpid(), "fingerprint": _fingerprint(), "started": time.time()}
    code = ""
    if _on_port():
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        code = secrets.token_hex(32)
        mine.update({"port": server.getsockname()[1], "code": code})
    else:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _in_relay_dir(server.bind)
        os.chmod(_SOCKET, 0o600)
    server.listen(16)
    server.settimeout(30.0)
    _write_private(_STATE, json.dumps(mine))
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
            threading.Thread(target=_answer, args=(conn, code), daemon=True).start()
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


def _caller_holder() -> str:
    """The options holder of the Claude Code session this shell runs in, or "".

    The relay runs the script against it, so each session uses its own
    options. Naming another of the user's own sessions gets that session's
    options, which are the same user's; nothing outside the user is opened.
    """
    try:
        import _options

        return _options.session_record()
    except Exception:
        return ""


def relay(script: str, argv: list) -> int | None:
    """Run ``script`` through the relay and print what it printed.

    Returns its exit code, or None when this process should run the script
    itself: it already has the key option (a hook, or a relayed run), or no
    relay is up.
    """
    if os.environ.get("COGNEE_RELAYED", "") == "1" or _key_option():
        return None
    state = _read_state()
    if not state:
        return None
    cwd = os.getcwd()
    try:
        if state.get("port"):
            conn = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            conn.settimeout(5.0)
            conn.connect(("127.0.0.1", int(state["port"])))
        else:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(5.0)
            _in_relay_dir(conn.connect)
    except (OSError, ValueError):
        return None
    env = {
        "COGNEE_SESSION_REF": _session_ref(),
        "CLAUDE_CODE_SESSION_ID": os.environ.get("CLAUDE_CODE_SESSION_ID", ""),
        "COGNEE_BACKEND": os.environ.get("COGNEE_BACKEND", ""),
        "COGNEE_CLAUDE_BACKEND": os.environ.get("COGNEE_CLAUDE_BACKEND", ""),
        "COGNEE_BASE_URL": _shell_value("COGNEE_BASE_URL"),
        "COGNEE_LOCAL_API_URL": _shell_value("COGNEE_LOCAL_API_URL"),
        "COGNEE_PLUGIN_DATASET": _shell_value("COGNEE_PLUGIN_DATASET"),
    }
    request = {
        "script": script,
        "argv": list(argv),
        "cwd": cwd,
        "settings": env,
        "holder": _caller_holder(),
    }
    if state.get("code"):
        request["code"] = str(state["code"])
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
