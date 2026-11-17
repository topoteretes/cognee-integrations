"""The plugin's options, as the options holder hands them over (``_holder.py``).

Claude Code starts the holder with the plugin's options; this is the other end,
used by the hooks and by the processes they start. A hook finds the holder of
its own Claude Code session by that session's pid (``CLAUDE_PID``, which Claude
Code exports to hooks, else the process's ancestors) and records the holder in
``COGNEE_OPTIONS_HOLDER``, so the processes it starts reach the same holder.

The values are read once per process and kept in memory: a watcher or worker
that outlives its session goes on with what it read while the session ran.

A script run from a shell (a skill's command) has neither: it finds no holder
and has no options. Such scripts reach the server through the relay
(``_relay.py``), as before.

Stdlib only, and fast when there is no holder: no import of the plugin's other
modules.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

_DIR = Path.home() / ".cognee-plugin" / "claude-code" / "holder"
#: Inherited by the processes a hook starts: the holder's record file.
HOLDER_VAR = "COGNEE_OPTIONS_HOLDER"
#: Set by the relay for a script it runs for another server than the options
#: name: the Cognee API key option is not handed to that run.
OTHER_SERVER_VAR = "COGNEE_RELAY_OTHER_SERVER"

_cache: dict = {}
_cache_from = [""]


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


def _record_for_session() -> str:
    """The holder record of this process's Claude Code session, or ""."""
    held = os.environ.get("COGNEE_OPTIONS_HOLDER", "").strip()
    if held:
        return held
    if not os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip():
        # Not a hook, nor started by one: a shell. It uses the relay.
        return ""
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


def values(wait: float = 0.0) -> dict:
    """Every option the holder has, by its ``userConfig`` name; {} without one.

    ``wait`` is how long to keep looking for a holder that is still starting:
    Claude Code starts its MCP servers alongside the first hooks, so session
    start waits for it. Later hooks find it at once.
    """
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        record = _record_for_session()
        if record and record == _cache_from[0]:
            # Read once per process: a watcher or worker whose session (and
            # holder) has ended goes on with what it read.
            return dict(_cache)
        if record:
            answer = _ask(record)
            if answer is not None:
                _cache.clear()
                _cache.update(answer)
                _cache_from[0] = record
                os.environ["COGNEE_OPTIONS_HOLDER"] = record
                return dict(_cache)
        if time.monotonic() >= deadline:
            return {}
        time.sleep(0.1)


def value(name: str) -> str:
    """One option's value, stripped; "" when unset or without a holder."""
    if name == "cognee_api_key" and os.environ.get("COGNEE_RELAY_OTHER_SERVER", "") == "1":
        return ""
    return str(values().get(name, "")).strip()
