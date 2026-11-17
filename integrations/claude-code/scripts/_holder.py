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


def _answer(conn: socket.socket, values: dict, code: str) -> None:
    try:
        with conn:
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


def serve(values: dict, state: Path, *, port_mode: bool | None = None) -> tuple:
    """Start answering ``values`` and record where in ``state``.

    Returns ``(server, cleanup)``; the answering runs on a daemon thread. Also
    used by the tests, which serve values of their own the same way.
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
            threading.Thread(target=_answer, args=(conn, values, code), daemon=True).start()

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


def main(argv: list) -> int:
    if len(argv) == 3 and argv[1] == _FROM_FD:
        values = _read_fd(int(argv[2]))
    elif os.name != "nt":
        _hand_over(argv[1:])
        return 1  # not reached: the process was replaced
    else:
        values = _named(argv[1:])
    # This process's parent is Claude Code, which started it; the hooks of the
    # same session look the holder up by that pid.
    state = DIR / ("%d.json" % os.getppid())
    _, cleanup = serve(values, state)
    try:
        _mcp(sys.stdin, sys.stdout)
    finally:
        cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
