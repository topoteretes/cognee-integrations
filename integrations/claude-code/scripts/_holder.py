"""The options holder: keeps the plugin's options for its hooks.

Claude Code starts this file as one of the plugin's MCP servers (it offers no
tools), with the plugin's options as its arguments: ``${user_config.KEY}`` in a
local MCP server's arguments is the way the plugin directory accepts for a
plugin's keys, and Claude Code starts the server even when a sensitive option
is unset (it passes an empty value), which a hook would not do.

Arguments can be read by every user on the machine (``ps``), so on macOS and
Linux the first thing this process does is hand the values to a fresh copy of
itself through a pipe and replace itself with that copy. The arguments are on
a command line only for that moment; the copy's reads ``_holder.py --from-fd N``.

The copy hands the values to the plugin's own scripts. It listens on a Unix
socket in ``~/.cognee-plugin/claude-code/holder`` (a folder only the user can
enter, the socket readable by the user only) and records where in
``<claude pid>.json`` next to it, so the hooks of this Claude Code session find
their own holder. Where Unix sockets do not exist (Windows) it listens on a
loopback port and answers only a request carrying the random code written in
that file, which only the user can read. Windows keeps another user from
reading a process's command line, so there the values are not moved off it.

On macOS and Linux the holder also checks who is asking. It answers only a
process that is, or was started by, one of the plugin's own entry points:

* a hook: ``python -I <scripts>/hook_runner.py <scripts>/<hook>.py ...``;
* the relay: ``python -I <scripts>/_relay.py serve``, which runs a skill's
  script for a shell.

It reads the caller's pid from the socket and the command lines from the
process table, which no process can rewrite for another. ``-I`` keeps a
caller from loading code of its own through ``PYTHONPATH`` and the like, and
the hook's own script must be one of the plugin's. So a command the agent runs
through the Bash tool (``python3 -c ...``, or a plugin script started from a
shell) is refused: it is neither a hook nor started by the relay. Processes
that outlive the hook that started them get their options on a pipe from it
instead (``_options.popen``). What this does not stop is a process of the user
that rewrites the plugin's own files. On Windows (the loopback form) there is
no caller check: the random code file is the only gate.

The holder lives as long as Claude Code keeps the server, which is the whole
session, and removes its files when Claude Code closes it. It writes no value
to disk and logs nothing.

Stdlib only.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

#: The options, in the order the plugin manifest hands them to this file.
NAMES = (
    "cognee_base_url",
    "cognee_api_key",
    "llm_api_key",
    "embedding_api_key",
    "local_models",
    "cognee_user_email",
    "cognee_user_password",
)
DIR = Path.home() / ".cognee-plugin" / "claude-code" / "holder"
_FROM_FD = "--from-fd"
_SCRIPTS = Path(__file__).resolve().parent
#: How far up from the caller to look for one of the plugin's entry points: a
#: relayed shell script's python is the relay's grandchild.
_MAX_DEPTH = 8


# ------------------------------------------------------------ who is asking


def _peer_pid(conn: socket.socket) -> int:
    """The pid of the process at the other end of a Unix socket, or 0."""
    try:
        if sys.platform == "darwin":
            # SOL_LOCAL, LOCAL_PEERPID
            return struct.unpack("i", conn.getsockopt(0, 2, 4))[0]
        if hasattr(socket, "SO_PEERCRED"):
            pid, _uid, _gid = struct.unpack(
                "3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            )
            return pid
    except OSError:
        pass
    return 0


def _argv_macos(pid: int) -> list:
    """A process's arguments from the kernel (``KERN_PROCARGS2``)."""
    import ctypes
    import ctypes.util

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    mib = (ctypes.c_int * 3)(1, 49, pid)  # CTL_KERN, KERN_PROCARGS2
    size = ctypes.c_size_t(0)
    if libc.sysctl(mib, 3, None, ctypes.byref(size), None, 0) != 0 or not size.value:
        return []
    buf = ctypes.create_string_buffer(size.value)
    if libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0) != 0:
        return []
    raw = buf.raw[: size.value]
    argc = struct.unpack("i", raw[:4])[0]
    rest = raw[4:]
    # The executable's path, then NUL padding, then argc arguments.
    end = rest.find(b"\0")
    rest = rest[end:].lstrip(b"\0")
    return [a.decode("utf-8", "replace") for a in rest.split(b"\0")[:argc]]


def _process(pid: int) -> tuple:
    """``(parent pid, argv)`` of a process, or ``(0, [])``."""
    try:
        if sys.platform.startswith("linux"):
            base = Path("/proc/%d" % pid)
            argv = [
                a.decode("utf-8", "replace")
                for a in (base / "cmdline").read_bytes().split(b"\0")
                if a
            ]
            stat = (base / "stat").read_text(encoding="utf-8", errors="replace")
            parent = int(stat.rsplit(")", 1)[1].split()[1])
            return parent, argv
        if sys.platform == "darwin":
            out = subprocess.run(
                ["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=3
            ).stdout.strip()
            return (int(out) if out.isdigit() else 0), _argv_macos(pid)
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        pass
    return 0, []


def _in_scripts(path: str, name: str = "") -> bool:
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    if resolved.parent != _SCRIPTS or resolved.suffix != ".py":
        return False
    return not name or resolved.name == name


def entry_point(argv: list) -> bool:
    """Whether a command line is one of the plugin's entry points: a hook run
    through ``hook_runner.py``, or the relay. Both must run under ``-I``."""
    if len(argv) < 4 or argv[1] != "-I":
        return False
    if _in_scripts(argv[2], "hook_runner.py"):
        return _in_scripts(argv[3])
    if _in_scripts(argv[2], "_relay.py"):
        return argv[3:] == ["serve"]
    return False


def caller_allowed(conn: socket.socket) -> bool:
    """Whether the caller is an entry point or was started by one."""
    pid = _peer_pid(conn)
    seen = set()
    for _ in range(_MAX_DEPTH):
        if pid <= 1 or pid in seen:
            return False
        seen.add(pid)
        parent, argv = _process(pid)
        if entry_point(argv):
            return True
        pid = parent
    return False


def _checks_callers() -> bool:
    """macOS and Linux: the platforms where the caller's pid and command line
    can be read. Elsewhere the socket's permissions are the only gate."""
    return sys.platform == "darwin" or sys.platform.startswith("linux")


def _named(values: list) -> dict:
    values = [str(v) for v in values]
    return {name: (values[i] if i < len(values) else "") for i, name in enumerate(NAMES)}


def _hand_over(values: list) -> None:
    """Re-run this file with the values on a pipe instead of the command line."""
    read_end, write_end = os.pipe()
    payload = json.dumps(values).encode("utf-8")
    # The values are a few hundred bytes, far below a pipe's buffer, so this
    # write cannot block before the reader exists.
    os.write(write_end, payload)
    os.close(write_end)
    os.set_inheritable(read_end, True)
    script = os.path.abspath(__file__)
    os.execv(sys.executable, [sys.executable, script, _FROM_FD, str(read_end)])


def _read_fd(fd: int) -> dict:
    with os.fdopen(fd, "rb") as stream:
        values = json.loads(stream.read().decode("utf-8") or "[]")
    return _named(values if isinstance(values, list) else [])


def _write_private(path: Path, text: str) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    with os.fdopen(os.open(str(path), flags, 0o600), "w", encoding="utf-8") as stream:
        stream.write(text)


def _bind_unix(server: socket.socket, path: Path) -> None:
    """Bind by file name from inside the folder: a socket path is limited to
    about 100 bytes, and a home folder can be longer than that."""
    before = os.getcwd()
    os.chdir(str(path.parent))
    try:
        server.bind(path.name)
    finally:
        os.chdir(before)


def _answer(conn: socket.socket, values: dict, code: str, check=None) -> None:
    try:
        with conn:
            if check is not None and not check(conn):
                return
            if code:
                conn.settimeout(5.0)
                asked = b""
                while b"\n" not in asked and len(asked) < 4096:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    asked += chunk
                if not hmac.compare_digest(asked.split(b"\n", 1)[0], code.encode("ascii")):
                    return
            conn.sendall(json.dumps(values).encode("utf-8"))
    except OSError:
        pass


def serve(values: dict, state: Path, *, port_mode: bool | None = None, check=None) -> tuple:
    """Start answering ``values`` and record where in ``state``.

    ``check(conn)`` decides whether a caller is answered (``caller_allowed``
    for a session's holder); without one every caller that can open the
    socket is. Returns ``(server, cleanup)``; the answering runs on a daemon
    thread. Also used by the tests, which serve values of their own the same
    way.
    """
    state.parent.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(state.parent, 0o700)
    if port_mode is None:
        port_mode = not hasattr(socket, "AF_UNIX")
    code = ""
    if port_mode:
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        code = secrets.token_hex(32)
        record = {"pid": os.getpid(), "port": server.getsockname()[1], "code": code}
        sock_path = None
    else:
        sock_path = state.with_suffix(".sock")
        try:
            sock_path.unlink()
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        _bind_unix(server, sock_path)
        os.chmod(sock_path, 0o600)
        record = {"pid": os.getpid(), "socket": str(sock_path)}
    server.listen(16)
    _write_private(state, json.dumps(record))

    def loop() -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=_answer, args=(conn, values, code, check), daemon=True).start()

    threading.Thread(target=loop, daemon=True).start()

    def cleanup() -> None:
        try:
            server.close()
        except OSError:
            pass
        for path in (state, sock_path):
            if path is None:
                continue
            try:
                if json.loads(path.read_text(encoding="utf-8")).get("pid") != os.getpid():
                    continue
            except (OSError, ValueError):
                pass
            try:
                path.unlink()
            except OSError:
                pass

    return server, cleanup


def _mcp(stdin, stdout) -> None:
    """Answer Claude Code as an MCP server with no tools until it closes us."""
    for line in stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if not isinstance(message, dict) or message.get("id") is None:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cognee-options", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": []}
        else:
            result = {}
        stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        stdout.flush()


#: Programs that only start Python: a virtualenv's ``python.exe`` and the
#: ``py`` launcher run the real interpreter as their child on Windows.
_LAUNCHERS = frozenset({"python.exe", "python3.exe", "pythonw.exe", "py.exe", "pyw.exe"})


def _session_pid() -> int:
    """The pid of the Claude Code process that started this holder.

    On macOS and Linux that is the parent: a launcher there replaces itself
    (``exec``). On Windows a launcher stays as a process of its own between
    Claude Code and this one, so launchers are skipped.
    """
    pid = os.getppid()
    if os.name != "nt":
        return pid
    try:
        from _proc import _process_table_windows

        table = _process_table_windows()
    except Exception:
        return pid
    seen = set()
    while pid in table and pid not in seen:
        seen.add(pid)
        parent, exe = table[pid]
        if str(exe).lower() not in _LAUNCHERS or not parent:
            break
        pid = parent
    return pid


def main(argv: list) -> int:
    if len(argv) == 3 and argv[1] == _FROM_FD:
        values = _read_fd(int(argv[2]))
    elif os.name != "nt":
        _hand_over(argv[1:])
        return 1  # not reached: the process was replaced
    else:
        values = _named(argv[1:])
    # Recorded under the pid of the Claude Code that started it; the hooks of
    # the same session look the holder up by that pid.
    state = DIR / ("%d.json" % _session_pid())
    port_mode = not hasattr(socket, "AF_UNIX")
    check = caller_allowed if not port_mode and _checks_callers() else None
    _, cleanup = serve(values, state, port_mode=port_mode, check=check)
    try:
        _mcp(sys.stdin, sys.stdout)
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
