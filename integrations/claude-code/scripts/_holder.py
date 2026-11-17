"""The options holder: keeps the plugin's options for its hooks.

Claude Code starts this file as a local MCP server (no tools) with the plugin's
options as its arguments, the form the plugin directory asks for. The options
must not stay on a command line, where any user on the machine could read
them, so the first thing this process does is hand them to a fresh copy of
itself through a pipe and replace itself with that copy: the arguments are on
the command line only for the moment that takes. The copy hands the options to
the plugin's own scripts over a socket in a folder only the user can enter.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
from pathlib import Path

NAMES = (
    "cognee_base_url",
    "cognee_api_key",
    "llm_api_key",
    "embedding_api_key",
    "cognee_user_email",
    "cognee_user_password",
)
_DIR = Path.home() / ".cognee-plugin" / "claude-code" / "holder"
_FROM_PIPE = "--from-fd"


def _hand_over(values: list) -> None:
    """Re-run this file with the values on a pipe instead of the command line."""
    read_end, write_end = os.pipe()
    os.write(write_end, json.dumps(values).encode("utf-8"))
    os.close(write_end)
    os.set_inheritable(read_end, True)
    script = os.path.abspath(__file__)
    os.execv(sys.executable, [sys.executable, script, _FROM_PIPE, str(read_end)])


def _read_pipe(fd: int) -> dict:
    with os.fdopen(fd, "rb") as stream:
        values = json.loads(stream.read().decode("utf-8") or "[]")
    values = [str(v) for v in values] if isinstance(values, list) else []
    return {name: (values[i] if i < len(values) else "") for i, name in enumerate(NAMES)}


def _serve(values: dict) -> None:
    _DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(_DIR, 0o700)
    path = _DIR / "holder.sock"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(16)
    answer = json.dumps(values).encode("utf-8")
    while True:
        conn, _ = server.accept()
        with conn:
            conn.sendall(answer)


def main(argv: list) -> int:
    if len(argv) == 3 and argv[1] == _FROM_PIPE:
        values = _read_pipe(int(argv[2]))
    else:
        _hand_over(argv[1:])
        return 1  # not reached: the process was replaced
    threading.Thread(target=_serve, args=(values,), daemon=True).start()
    for line in sys.stdin:
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if message.get("id") is None:
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
        reply = {"jsonrpc": "2.0", "id": message["id"], "result": result}
        sys.stdout.write(json.dumps(reply) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
