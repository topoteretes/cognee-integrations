#!/usr/bin/env python3
"""The plugin's MCP server: the one process Claude Code gives the plugin's keys to.

Claude Code starts this server when a session starts and stops it when the
session ends. The manifest (``mcpServers`` in .claude-plugin/plugin.json) hands
it every plugin option through its ``env`` block, the keys included. Hook
processes no longer do the plugin's work: each hook in hooks/hooks.json either
calls this server's ``cognee_hook`` tool (an ``mcp_tool`` hook) or, for the two
events where Claude Code cannot reach a server (SessionStart at launch and
SessionEnd), leaves a request file that this server picks up
(``hook-relay.py``).

For each event the server runs the scripts listed in ``server-hooks.json`` as
its own child processes, with the event's payload on stdin, exactly as Claude
Code ran them as hooks before. A script that is not ``async`` has its output
returned to Claude Code, which reads it as the hook's output. An ``async``
script runs in the background and outlives the request.

When the server is told to stop it starts a short detached process
(``--finish``) that runs the SessionEnd scripts, because Claude Code stops the
server before the final sync could finish inside it.

Stdlib only, and nothing from the plugin is imported: the server has to start
on the interpreter Claude Code finds, before the plugin's venv is known.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

SERVER_NAME = "cognee"
HOOK_TOOL = "cognee_hook"
_PROTOCOL_FALLBACK = "2025-06-18"

_SCRIPTS = Path(__file__).resolve().parent
_HOOK_TABLE = _SCRIPTS / "server-hooks.json"
_RUNNER = _SCRIPTS / "hook_runner.py"
_PLUGIN_MANIFEST = _SCRIPTS.parent / ".claude-plugin" / "plugin.json"

#: Seconds a script without a ``timeout`` in server-hooks.json may run.
_DEFAULT_TIMEOUT = 600.0
#: How often the relay directory is checked for a request file.
_RELAY_POLL_SECONDS = 0.1
#: How long ``--finish`` waits for the SessionEnd hook's request file.
_FINISH_WAIT_SECONDS = 5.0

#: Payload fields Claude Code substitutes as JSON text; decoded back to objects.
_JSON_FIELDS = ("tool_input", "tool_response")
_BOOL_FIELDS = ("stop_hook_active",)
#: The events whose matcher is tested against the tool name / the start source.
_MATCH_FIELD = {"PreToolUse": "tool_name", "PostToolUse": "tool_name", "SessionStart": "source"}

_write_lock = threading.Lock()


# --------------------------------------------------------------------- state


def state_dir() -> Path:
    """``~/.cognee-plugin/claude-code`` (``COGNEE_PLUGIN_STATE_DIR`` is for tests)."""
    base = os.environ.get("COGNEE_PLUGIN_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".cognee-plugin"
    )
    return Path(base) / "claude-code"


_HOST_PID = 0


def host_pid() -> int:
    """The Claude Code process this server belongs to: the process that started it.

    Claude Code exports ``CLAUDE_PID`` to hooks but not to MCP servers, so a
    value in this process's environment comes from somewhere else (a session
    started from inside another one) and is not used.
    """
    return _HOST_PID or os.getppid()


def relay_dir() -> Path:
    """Where ``hook-relay.py`` leaves requests for this Claude Code process."""
    return state_dir() / "relay" / str(host_pid())


def _trace(event: str, **detail) -> None:
    """One line in ``mcp-server.log``: the server's own diagnostics, apart from hook.log."""
    try:
        path = state_dir() / "mcp-server.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 1_000_000:
            os.replace(path, path.with_suffix(".log.1"))
        line = {"ts": round(time.time(), 3), "pid": os.getpid(), "event": event}
        if detail:
            line["detail"] = detail
        with open(path, "a", encoding="utf-8", errors="backslashreplace") as handle:
            handle.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _write_json(path: Path, value) -> None:
    tmp = path.with_name(".%s.%d.tmp" % (path.name, os.getpid()))
    tmp.write_text(json.dumps(value), encoding="utf-8")
    os.replace(tmp, path)


def _pid_alive(pid: int) -> bool:
    if pid <= 1:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------- hook table


def parse_command(command: str) -> tuple[str, list[str]]:
    """The script and arguments a ``server-hooks.json`` command names.

    Commands keep the shape they had as Claude Code hook commands,
    ``python3 "<root>/scripts/hook_runner.py" "<root>/scripts/<script>" [args] || python ...``.
    Only the part before ``||`` is read. The script must be a file name in this
    directory.
    """
    first = str(command or "").split("||", 1)[0]
    tokens = shlex.split(first)
    names = [os.path.basename(token) for token in tokens]
    if "hook_runner.py" not in names:
        raise ValueError("command does not run hook_runner.py: %r" % command)
    rest = tokens[names.index("hook_runner.py") + 1 :]
    if not rest:
        raise ValueError("command names no script: %r" % command)
    script = os.path.basename(rest[0])
    if not script.endswith(".py") or not (_SCRIPTS / script).is_file():
        raise ValueError("command names an unknown script: %r" % command)
    return script, rest[1:]


def load_hook_table() -> dict:
    spec = json.loads(_HOOK_TABLE.read_text(encoding="utf-8"))
    return spec.get("hooks", {})


def entries_for(event: str, payload: dict, table: dict | None = None) -> list[dict]:
    """The scripts registered for ``event``, filtered by matcher like Claude Code does."""
    table = load_hook_table() if table is None else table
    match = str(payload.get(_MATCH_FIELD.get(event, ""), "") or "")
    entries = []
    for group in table.get(event, []):
        matcher = group.get("matcher") or "*"
        if matcher != "*" and not re.fullmatch(matcher, match):
            continue
        for hook in group.get("hooks", []):
            script, args = parse_command(hook.get("command", ""))
            entries.append(
                {
                    "script": script,
                    "args": args,
                    "async": bool(hook.get("async")),
                    "timeout": float(hook.get("timeout") or _DEFAULT_TIMEOUT),
                }
            )
    return entries


# ------------------------------------------------------------ running scripts


def _child_env(payload: dict) -> dict:
    env = os.environ.copy()
    session_id = str(payload.get("session_id", "") or "").strip()
    if session_id:
        # The server keeps the id of the session it started in; after /clear
        # the payload carries the current one.
        env["CLAUDE_CODE_SESSION_ID"] = session_id
    return env


def _child_cwd(payload: dict):
    cwd = str(payload.get("cwd", "") or "")
    return cwd if cwd and os.path.isdir(cwd) else None


def _detach_kwargs() -> dict:
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(
            subprocess, "DETACHED_PROCESS", 0
        )
        return {"creationflags": flags}
    return {"start_new_session": True}


def run_script(entry: dict, payload: dict) -> str:
    """Run one script with ``payload`` on stdin. Returns its stdout, or "" when async."""
    command = [sys.executable, str(_RUNNER), str(_SCRIPTS / entry["script"]), *entry["args"]]
    data = json.dumps(payload).encode("utf-8")
    env, cwd = _child_env(payload), _child_cwd(payload)
    if entry["async"]:
        try:
            child = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=env,
                cwd=cwd,
                **_detach_kwargs(),
            )
        except Exception as exc:
            _trace("script_start_failed", script=entry["script"], error=str(exc)[:300])
            return ""

        def feed() -> None:
            try:
                child.stdin.write(data)
                child.stdin.close()
                child.wait()
            except Exception:
                pass

        threading.Thread(target=feed, daemon=True).start()
        return ""
    try:
        done = subprocess.run(
            command,
            input=data,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env=env,
            cwd=cwd,
            timeout=entry["timeout"],
        )
    except subprocess.TimeoutExpired:
        _trace("script_timeout", script=entry["script"], timeout=entry["timeout"])
        return ""
    except Exception as exc:
        _trace("script_failed", script=entry["script"], error=str(exc)[:300])
        return ""
    return done.stdout.decode("utf-8", "replace").strip()


def run_event(payload: dict) -> str:
    """Run every script registered for the payload's event. Returns the hook output."""
    event = str(payload.get("hook_event_name", "") or "")
    try:
        entries = entries_for(event, payload)
    except Exception as exc:
        _trace("hook_table_unreadable", event_name=event, error=str(exc)[:300])
        return ""
    for entry in entries:
        if entry["async"]:
            run_script(entry, payload)
    outputs = [run_script(entry, payload) for entry in entries if not entry["async"]]
    outputs = [text for text in outputs if text]
    if len(outputs) > 1:
        _trace("extra_hook_output_dropped", event_name=event, count=len(outputs))
    return outputs[0] if outputs else ""


def payload_from_arguments(arguments: dict) -> dict:
    """The hook payload behind an ``mcp_tool`` hook's ``input``.

    Claude Code fills each ``${field}`` from the hook's own payload: a missing
    field arrives as "", an object as its JSON text.
    """
    payload = {}
    for key, value in (arguments or {}).items():
        if value is None or value == "":
            continue
        if key in _JSON_FIELDS and isinstance(value, str):
            try:
                decoded = json.loads(value)
            except ValueError:
                decoded = value
            value = decoded if isinstance(decoded, (dict, list)) else value
        elif key in _BOOL_FIELDS and isinstance(value, str):
            value = value.strip().lower() == "true"
        payload[key] = value
    return payload


def _remember_session(payload: dict) -> None:
    """Keep the latest session facts for ``--finish``, which may have no payload."""
    if not payload.get("session_id"):
        return
    kept = ("session_id", "transcript_path", "cwd")
    facts = {key: payload[key] for key in kept if key in payload}
    try:
        _write_json(relay_dir() / "last-session.json", facts)
    except Exception:
        pass


# --------------------------------------------------------------------- relay


def _claim(request: Path) -> Path | None:
    claimed = request.with_name("run-" + request.name[len("req-") :])
    try:
        os.replace(request, claimed)
    except OSError:
        return None
    return claimed


def serve_request(request: Path) -> bool:
    """Run the scripts for one request file and write its response. False if not ours."""
    claimed = _claim(request)
    if claimed is None:
        return False
    try:
        payload = json.loads(claimed.read_text(encoding="utf-8"))
    except Exception as exc:
        _trace("relay_request_unreadable", error=str(exc)[:200])
        payload = {}
    output = ""
    if isinstance(payload, dict) and payload:
        _remember_session(payload)
        output = run_event(payload)
        if payload.get("hook_event_name") == "SessionEnd":
            try:
                (claimed.parent / "session-end-done").write_text("1", encoding="utf-8")
            except Exception:
                pass
    try:
        _write_json(claimed.with_name("res-" + claimed.name[len("run-") :]), {"stdout": output})
    except Exception as exc:
        _trace("relay_response_failed", error=str(exc)[:200])
    try:
        claimed.unlink()
    except OSError:
        pass
    return True


def _relay_loop(stop: threading.Event) -> None:
    directory = relay_dir()
    while not stop.is_set():
        try:
            for request in sorted(directory.glob("req-*.json")):
                threading.Thread(target=serve_request, args=(request,), daemon=True).start()
        except Exception as exc:
            _trace("relay_scan_failed", error=str(exc)[:200])
        stop.wait(_RELAY_POLL_SECONDS)


def _open_relay() -> None:
    directory = relay_dir()
    directory.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(directory, 0o700)
    except OSError:
        pass
    _write_json(
        directory / "server.json",
        {
            "pid": os.getpid(),
            "host_pid": host_pid(),
            "started": time.time(),
            # The session this server started in: how hook-relay.py finds the
            # server when the host pid it sees is not this server's parent.
            "session_id": str(os.environ.get("CLAUDE_CODE_SESSION_ID", "") or ""),
        },
    )
    _sweep_dead_relays(directory.parent, keep=directory)


def _sweep_dead_relays(root: Path, keep: Path) -> None:
    """Remove relay directories whose server and Claude Code process are both gone."""
    for directory in root.iterdir():
        if directory == keep or not directory.is_dir():
            continue
        try:
            info = json.loads((directory / "server.json").read_text(encoding="utf-8"))
            alive = _pid_alive(int(info.get("pid", 0))) or _pid_alive(int(info.get("host_pid", 0)))
            age = time.time() - float(info.get("started", 0))
        except Exception:
            alive, age = False, time.time() - directory.stat().st_mtime
        if not alive and age > 3600:
            shutil.rmtree(directory, ignore_errors=True)


# -------------------------------------------------------------------- finish


def _spawn_finisher() -> None:
    try:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--finish",
                str(relay_dir()),
                str(host_pid()),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            **_detach_kwargs(),
        )
    except Exception as exc:
        _trace("finisher_start_failed", error=str(exc)[:300])


def finish(directory: Path) -> int:
    """After the server stopped: run the SessionEnd scripts if the session ended.

    The SessionEnd hook leaves a request file; it is served here when the
    server was stopped first. With no request, the scripts still run once
    Claude Code itself is gone (a crash or a kill). If Claude Code is still
    running and sent no SessionEnd, the server was only restarted, and nothing
    is done.
    """
    deadline = time.time() + _FINISH_WAIT_SECONDS
    while time.time() < deadline and not (directory / "session-end-done").exists():
        for request in sorted(directory.glob("req-*.json")):
            if _request_event(request) == "SessionEnd":
                serve_request(request)
        time.sleep(_RELAY_POLL_SECONDS)
    host_alive = _pid_alive(host_pid())
    ended = (directory / "session-end-done").exists()
    if not ended and not host_alive:
        try:
            payload = json.loads((directory / "last-session.json").read_text(encoding="utf-8"))
        except Exception:
            payload = {}
        if payload.get("session_id"):
            payload.update({"hook_event_name": "SessionEnd", "reason": "other"})
            run_event(payload)
            ended = True
    _trace("finished", session_end_ran=ended, host_alive=host_alive)
    # Claude Code exits right after SessionEnd; a restarted server keeps the directory.
    deadline = time.time() + _FINISH_WAIT_SECONDS
    while ended and _pid_alive(host_pid()) and time.time() < deadline:
        time.sleep(_RELAY_POLL_SECONDS)
    if not _pid_alive(host_pid()):
        shutil.rmtree(directory, ignore_errors=True)
    return 0


def _request_event(request: Path) -> str:
    try:
        return str(json.loads(request.read_text(encoding="utf-8")).get("hook_event_name", ""))
    except Exception:
        return ""


# ------------------------------------------------------------------ protocol


def _plugin_version() -> str:
    try:
        return str(json.loads(_PLUGIN_MANIFEST.read_text(encoding="utf-8")).get("version", "0"))
    except Exception:
        return "0"


def tool_definitions() -> list[dict]:
    return [
        {
            "name": HOOK_TOOL,
            "description": (
                "Internal to the Cognee memory plugin: its hooks call this to run memory "
                "recall and capture for a session event. Not for the assistant to call."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"hook_event_name": {"type": "string"}},
                "required": ["hook_event_name"],
                "additionalProperties": True,
            },
        }
    ]


def call_tool(name: str, arguments: dict) -> dict:
    if name != HOOK_TOOL:
        return {
            "content": [{"type": "text", "text": "Unknown tool: %s" % name}],
            "isError": True,
        }
    payload = payload_from_arguments(arguments)
    _remember_session(payload)
    return {"content": [{"type": "text", "text": run_event(payload)}]}


def _send(message: dict) -> None:
    with _write_lock:
        sys.stdout.write(json.dumps(message) + "\n")
        sys.stdout.flush()


def _answer_call(request_id, params: dict) -> None:
    try:
        result = call_tool(str(params.get("name", "")), params.get("arguments") or {})
    except Exception as exc:
        _trace("tool_call_failed", error=str(exc)[:300])
        result = {"content": [{"type": "text", "text": ""}]}
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def handle(message: dict) -> None:
    method, request_id = message.get("method"), message.get("id")
    params = message.get("params") or {}
    if method == "initialize":
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": params.get("protocolVersion") or _PROTOCOL_FALLBACK,
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "cognee-memory", "version": _plugin_version()},
                },
            }
        )
    elif method == "tools/list":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": tool_definitions()}})
    elif method == "tools/call":
        # One thread per call: a recall must not wait behind another event.
        threading.Thread(target=_answer_call, args=(request_id, params), daemon=True).start()
    elif method == "ping":
        _send({"jsonrpc": "2.0", "id": request_id, "result": {}})
    elif request_id is not None:
        _send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {"code": -32601, "message": "Method not found: %s" % method},
            }
        )


def serve() -> int:
    for name in ("stdin", "stdout"):
        stream = getattr(sys, name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="surrogateescape")
            except Exception:
                pass
    stop = threading.Event()
    try:
        _open_relay()
        threading.Thread(target=_relay_loop, args=(stop,), daemon=True).start()
    except Exception as exc:
        _trace("relay_open_failed", error=str(exc)[:300])

    def on_signal(_signum, _frame) -> None:
        raise SystemExit(0)

    for name in ("SIGTERM", "SIGINT"):
        try:
            signal.signal(getattr(signal, name), on_signal)
        except Exception:
            pass
    _trace("started", host_pid=host_pid(), version=_plugin_version())
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict):
                handle(message)
    finally:
        stop.set()
        _spawn_finisher()
        _trace("stopped")
    return 0


def main(argv: list) -> int:
    global _HOST_PID
    if len(argv) >= 4 and argv[1] == "--finish":
        try:
            _HOST_PID = int(argv[3])
        except ValueError:
            return 0
        return finish(Path(argv[2]))
    _HOST_PID = os.getppid()
    return serve()


if __name__ == "__main__":
    sys.exit(main(sys.argv))
