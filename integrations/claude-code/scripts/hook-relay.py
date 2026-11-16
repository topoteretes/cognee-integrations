#!/usr/bin/env python3
"""Hand a hook event to the plugin's MCP server through a request file.

Claude Code cannot call an MCP server from two hooks: SessionStart when it
launches (servers are not connected yet) and SessionEnd (the server is being
stopped). For those two events hooks/hooks.json runs this script instead. It
writes the hook's payload to a request file in the server's relay directory
and, unless ``--no-wait`` is given, waits for the server's response file and
prints it as the hook's output.

This process reads no plugin option and no key, and sends nothing over the
network. Stdlib only, and nothing from the plugin is imported.

Usage: hook-relay.py [--no-wait] [--timeout SECONDS]
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path

#: How long to wait for the server to appear before reporting that it is off.
_SERVER_GRACE_SECONDS = 15.0
_POLL_SECONDS = 0.05
_DEFAULT_TIMEOUT = 110.0
_STALE_SECONDS = 86400.0

_SERVER_OFF = (
    "Cognee memory is off for this session: the plugin's MCP server (cognee) did not "
    "start. Check /mcp, and that python3 is on your PATH."
)


def state_dir() -> Path:
    base = os.environ.get("COGNEE_PLUGIN_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cognee-plugin"
    )
    return Path(base) / "claude-code"


def host_pid() -> int:
    """The Claude Code process, which exports its pid to hooks as ``CLAUDE_PID``."""
    try:
        pid = int(str(os.environ.get("CLAUDE_PID", "") or "0").strip() or 0)
    except ValueError:
        pid = 0
    return pid if pid > 1 else os.getppid()


def _server_info(directory: Path) -> dict:
    try:
        info = json.loads((directory / "server.json").read_text(encoding="utf-8"))
    except Exception:
        return {}
    return info if isinstance(info, dict) else {}


def _last_session(directory: Path) -> str:
    try:
        facts = json.loads((directory / "last-session.json").read_text(encoding="utf-8"))
        return str(facts.get("session_id", "") or "")
    except Exception:
        return ""


def find_server_dir(payload: dict) -> Path | None:
    """The relay directory of this Claude Code process's server, or None.

    The server names its directory after the process that started it, which is
    Claude Code itself unless a launcher sits in between. So the directory is
    looked up by Claude Code's pid first, then by the session id the server
    started in (or last saw), and the match is remembered for later events.
    """
    root = state_dir() / "relay"
    pid = host_pid()
    direct = root / str(pid)
    if _server_info(direct):
        return direct
    pointer = root / ("host-%d" % pid)
    try:
        remembered = root / pointer.read_text(encoding="utf-8").strip()
        if _server_info(remembered):
            return remembered
    except Exception:
        pass
    session_id = str(payload.get("session_id", "") or "")
    if not session_id or not root.is_dir():
        return None
    for directory in root.iterdir():
        info = _server_info(directory) if directory.is_dir() else {}
        if info and session_id in (info.get("session_id"), _last_session(directory)):
            try:
                pointer.write_text(directory.name, encoding="utf-8")
            except OSError:
                pass
            return directory
    return None


def _sweep(directory: Path) -> None:
    """Drop request and response files nobody collected."""
    now = time.time()
    for pattern in ("req-*.json", "res-*.json", "run-*.json"):
        for path in directory.glob(pattern):
            try:
                if now - path.stat().st_mtime > _STALE_SECONDS:
                    path.unlink()
            except OSError:
                pass


def relay(payload: dict, *, wait: bool, timeout: float) -> str:
    """Leave ``payload`` for the server. Returns the server's hook output, or ""."""
    started = time.time()
    grace = _SERVER_GRACE_SECONDS if wait else 0.0
    directory = find_server_dir(payload)
    while directory is None and time.time() - started < grace:
        time.sleep(_POLL_SECONDS)
        directory = find_server_dir(payload)
    if directory is None:
        return json.dumps({"systemMessage": _SERVER_OFF}) if wait else ""
    _sweep(directory)
    name = uuid.uuid4().hex
    request = directory / ("req-%s.json" % name)
    response = directory / ("res-%s.json" % name)
    tmp = directory / (".req-%s.tmp" % name)
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, request)
    if not wait:
        return ""
    while time.time() - started < timeout:
        if response.is_file():
            try:
                output = json.loads(response.read_text(encoding="utf-8")).get("stdout", "")
            except Exception:
                output = ""
            try:
                response.unlink()
            except OSError:
                pass
            return str(output or "")
        time.sleep(_POLL_SECONDS)
    return ""


def main(argv: list) -> int:
    wait, timeout = True, _DEFAULT_TIMEOUT
    args = list(argv[1:])
    while args:
        arg = args.pop(0)
        if arg == "--no-wait":
            wait = False
        elif arg == "--timeout" and args:
            try:
                timeout = float(args.pop(0))
            except ValueError:
                pass
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    if not isinstance(payload, dict) or not payload.get("hook_event_name"):
        return 0
    output = relay(payload, wait=wait, timeout=timeout)
    if output:
        sys.stdout.write(output + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
