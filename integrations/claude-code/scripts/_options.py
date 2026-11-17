"""The plugin's options, as the options holder hands them over (``_holder.py``).

Claude Code starts the holder with the plugin's options; this is the other end,
used by the hooks and by the processes they start. A hook finds the holder of
its own Claude Code session by that session's pid (``CLAUDE_PID``, which Claude
Code exports to hooks, else the process's ancestors) and records the holder in
``COGNEE_OPTIONS_HOLDER``, so the processes it starts reach the same holder.

The values are read once per process and kept in memory. A process that starts
another one after its session may have ended (the exit watcher's final sync,
the cloud worker's improve passes) hands them over on a pipe instead
(``popen``): the child reads them from the descriptor named in
``COGNEE_OPTIONS_FD`` and needs no holder at all.

A script run from a shell (a skill's command) has neither: it finds no holder
and has no options. Such scripts reach the server through the relay
(``_relay.py``), which runs them against their own session's holder
(``session_record``).

Stdlib only, and fast when there is no holder: no import of the plugin's other
modules.
"""

from __future__ import annotations

import json
import os
import re
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

_DIR = Path.home() / ".cognee-plugin" / "claude-code" / "holder"
#: Inherited by the processes a hook starts: the holder's record file.
HOLDER_VAR = "COGNEE_OPTIONS_HOLDER"
#: Set for a child that gets its parent's options on a pipe: the descriptor
#: (POSIX) or ``h<handle>`` (Windows) to read them from, once.
FD_VAR = "COGNEE_OPTIONS_FD"
#: Set by the relay for a script it runs for another server than the options
#: name: the Cognee API key option is not handed to that run.
OTHER_SERVER_VAR = "COGNEE_RELAY_OTHER_SERVER"
_RECORD_NAME = re.compile(r"^\d+\.json$")

_cache: dict = {}
#: Where the cached values came from: "" (nothing read yet), "pipe", or the
#: holder record they were read from. Set once; a process never reads twice.
_source = [""]


def _ancestors() -> list:
    """This process's ancestors, nearest first (macOS and Linux)."""
    if sys.platform == "win32":
        try:
            from _proc import _process_table_windows

            table = {pid: parent for pid, (parent, _exe) in _process_table_windows().items()}
        except Exception:
            return []
    else:
        try:
            raw = subprocess.run(
                ["ps", "-axo", "pid=,ppid="],
                capture_output=True,
                text=True,
                timeout=3,
            ).stdout
        except Exception:
            return []
        table = {}
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                table[int(parts[0])] = int(parts[1])
    found = []
    pid = os.getppid()
    while pid > 1 and pid not in found:
        found.append(pid)
        pid = table.get(pid, 0)
    return found


def is_record(path: str) -> bool:
    """Whether ``path`` is a live holder record: ``<pid>.json`` in the holder
    folder. The relay checks what a caller names with this."""
    try:
        record = Path(path)
        return (
            bool(_RECORD_NAME.match(record.name))
            and record.parent.resolve() == _DIR.resolve()
            and record.is_file()
        )
    except (OSError, ValueError):
        return False


def session_record() -> str:
    """The holder record of the Claude Code session this process runs in, or "".

    Found by ``CLAUDE_PID``, else by walking up to the first ancestor that has
    a holder. Used by a hook, and by a script run from a shell to tell the
    relay which session it belongs to.
    """
    candidates = []
    raw = os.environ.get("CLAUDE_PID", "").strip()
    if raw.isdigit():
        candidates.append(int(raw))
    if not candidates or not (_DIR / ("%d.json" % candidates[0])).is_file():
        candidates.extend(_ancestors())
    for pid in candidates:
        record = _DIR / ("%d.json" % pid)
        if record.is_file():
            return str(record)
    return ""


def _record_for_session() -> str:
    """The holder record this process reads from, or ""."""
    held = os.environ.get("COGNEE_OPTIONS_HOLDER", "").strip()
    if held and Path(held).is_file():
        return held
    if not os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip():
        # Not a hook, nor started by one: a shell. It uses the relay.
        return ""
    # An inherited holder whose session has ended falls through to this
    # process's own session, if it has one.
    return session_record()


def _ask(record: str):
    """The holder's answer, or None when it cannot be reached."""
    try:
        state = json.loads(Path(record).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(state, dict):
        return None
    try:
        if state.get("port"):
            conn = socket.create_connection(("127.0.0.1", int(state["port"])), timeout=3.0)
            conn.sendall(str(state.get("code") or "").encode("ascii") + b"\n")
        else:
            path = Path(str(state.get("socket") or ""))
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(3.0)
            before = os.getcwd()
            os.chdir(str(path.parent))
            try:
                conn.connect(path.name)
            finally:
                os.chdir(before)
        with conn:
            chunks = []
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        answer = json.loads(b"".join(chunks).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(answer, dict):
        return None
    return {str(k): str(v) for k, v in answer.items()}


def _keep(answer: dict, source: str) -> dict:
    _cache.clear()
    _cache.update(answer)
    _source[0] = source
    return dict(_cache)


def _from_pipe():
    """The values a parent handed this process on a pipe, read once; or None."""
    raw = os.environ.pop("COGNEE_OPTIONS_FD", "").strip()
    if not raw:
        return None
    try:
        if raw.startswith("h"):
            import msvcrt

            fd = msvcrt.open_osfhandle(int(raw[1:]), os.O_RDONLY)
        else:
            fd = int(raw)
            # Only the pipe a parent handed over: a process that inherited the
            # variable without the descriptor may have another file there.
            if not stat.S_ISFIFO(os.fstat(fd).st_mode):
                return None
        with os.fdopen(fd, "rb") as stream:
            answer = json.loads(stream.read().decode("utf-8") or "null")
    except (OSError, ValueError):
        return None
    if not isinstance(answer, dict):
        return None
    return {str(k): str(v) for k, v in answer.items()}


def values(wait: float = 0.0) -> dict:
    """Every option this process has, by its ``userConfig`` name; {} without any.

    ``wait`` is how long to keep looking for a holder that is still starting:
    Claude Code starts its MCP servers alongside the first hooks, so session
    start waits for it. Later hooks find it at once.
    """
    if _source[0]:
        # Read once per process: a watcher or worker whose session (and
        # holder) has ended goes on with what it read, as long as it still
        # names that holder.
        held = os.environ.get("COGNEE_OPTIONS_HOLDER", "").strip()
        if _source[0] == "pipe" or held == _source[0]:
            return dict(_cache)
        _source[0] = ""
    handed = _from_pipe()
    if handed is not None:
        return _keep(handed, "pipe")
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        record = _record_for_session()
        if record:
            answer = _ask(record)
            if answer is not None:
                os.environ["COGNEE_OPTIONS_HOLDER"] = record
                return _keep(answer, record)
        if time.monotonic() >= deadline:
            return {}
        time.sleep(0.1)


def have_options() -> bool:
    """Whether this process got its options (from a holder or a parent's pipe).

    False also when every option is unset but no holder answered: the two must
    not be confused, since "the URL option is empty" clears the recorded server
    only when the options were really read.
    """
    values()
    return bool(_source[0])


def value(name: str) -> str:
    """One option's value, stripped; "" when unset or without a holder."""
    if name == "cognee_api_key" and os.environ.get("COGNEE_RELAY_OTHER_SERVER", "") == "1":
        return ""
    return str(values().get(name, "")).strip()


def popen(args: list, *, env: dict | None = None, **kwargs) -> subprocess.Popen:
    """``subprocess.Popen`` that hands this process's options to the child on a
    pipe, so the child has them even once this session's holder is gone.

    Without options of its own this is a plain ``Popen``. The pipe holds a few
    hundred bytes, far below its buffer, so it is written in full before the
    child starts, and the parent's ends are closed once the child has its copy.
    """
    if env is None:
        from _proc import child_env

        env = child_env()
    env = dict(env)
    env.pop("COGNEE_OPTIONS_FD", None)
    if not have_options():
        return subprocess.Popen(args, env=env, **kwargs)
    have = dict(_cache)
    read_end, write_end = os.pipe()
    try:
        os.write(write_end, json.dumps(have).encode("utf-8"))
    finally:
        os.close(write_end)
    try:
        if os.name == "nt":
            import msvcrt

            handle = msvcrt.get_osfhandle(read_end)
            os.set_handle_inheritable(handle, True)
            info = kwargs.pop("startupinfo", None) or subprocess.STARTUPINFO()
            attributes = dict(getattr(info, "lpAttributeList", None) or {})
            attributes["handle_list"] = [*attributes.get("handle_list", []), handle]
            info.lpAttributeList = attributes
            env["COGNEE_OPTIONS_FD"] = "h%d" % handle
            kwargs["startupinfo"] = info
            kwargs["close_fds"] = True
        else:
            env["COGNEE_OPTIONS_FD"] = str(read_end)
            kwargs["pass_fds"] = (*tuple(kwargs.get("pass_fds") or ()), read_end)
        return subprocess.Popen(args, env=env, **kwargs)
    finally:
        os.close(read_end)


def run(args: list, *, env: dict | None = None, input=None, timeout=None, **kwargs):
    """``subprocess.run`` with this process's options handed over (``popen``)."""
    if kwargs.pop("capture_output", False):
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
    if input is not None:
        kwargs["stdin"] = subprocess.PIPE
    proc = popen(args, env=env, **kwargs)
    try:
        out, err = proc.communicate(input, timeout=timeout)
    except BaseException:
        proc.kill()
        proc.wait()
        raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def execv(path: str, argv: list) -> None:
    """``os.execv`` that keeps this process's options: the new program finds
    them on a pipe, so it need not reach the holder again. On Windows, where
    ``execv`` starts a new process, it reads them from the holder as before."""
    if os.name != "nt" and have_options():
        read_end, write_end = os.pipe()
        try:
            os.write(write_end, json.dumps(dict(_cache)).encode("utf-8"))
        finally:
            os.close(write_end)
        os.set_inheritable(read_end, True)
        os.environ["COGNEE_OPTIONS_FD"] = str(read_end)
        try:
            os.execv(path, argv)
        except OSError:
            os.environ.pop("COGNEE_OPTIONS_FD", None)
            os.close(read_end)
            raise
    os.execv(path, argv)
