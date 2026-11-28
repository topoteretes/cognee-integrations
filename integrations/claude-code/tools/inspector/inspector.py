#!/usr/bin/env python3
"""Cognee inspector: one live timeline of what the plugin and the server did.

A development tool, not part of the plugin's hooks. Read-only: it tails the
Claude Code plugin's hook event log and the Cognee server's log, and asks the
server for its pipeline/operation runs (improve stages included). It changes
nothing, sends requests only to the Cognee server the plugin already uses, and
binds to 127.0.0.1.

    python3 inspector.py            # http://127.0.0.1:8030

Settings (environment):
    INSPECTOR_PORT         port to listen on (default 8030)
    INSPECTOR_SERVER_URL   Cognee server (default: the plugin's cached base URL,
                           else http://localhost:8011)
    INSPECTOR_SERVER_LOG   server log to tail (default: the console log of the
                           server the plugin starts)
    INSPECTOR_HOOK_LOG     hook event log (default: the plugin's hook.log)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOME = Path.home()
PLUGIN_DIR = HOME / ".cognee-plugin"
PORT = int(os.environ.get("INSPECTOR_PORT", "8030"))
KEY_FILE = PLUGIN_DIR / "api_key.json"
HOOK_LOG = Path(os.environ.get("INSPECTOR_HOOK_LOG") or PLUGIN_DIR / "claude-code/hook.log")
SERVER_LOG = Path(
    os.environ.get("INSPECTOR_SERVER_LOG") or PLUGIN_DIR / "claude-code/server-console.log"
)
PAGE = Path(__file__).with_name("index.html")


def _server_url() -> str:
    """The server the plugin talks to: the setting, else its cached base URL."""
    url = os.environ.get("INSPECTOR_SERVER_URL", "").strip()
    if not url:
        try:
            url = str(json.loads(KEY_FILE.read_text()).get("base_url") or "")
        except (OSError, ValueError):
            url = ""
    return (url or "http://localhost:8011").rstrip("/")


SERVER = _server_url()

TAIL_BYTES = 600_000
ANSI = re.compile(r"\x1b\[[0-9;]*m")
LOG_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d+)?)\s+\[(\w+)\s*\]\s+(.*)$")
# Server log lines worth a row: enola, improve, distillation, code graph, reviews, failures.
SERVER_KEEP = re.compile(
    r"enola|improve|distill|Coding unit|code_graph|Code repo|review|locate|"
    r"PermissionDenied|raised \(Status|Traceback|abandoned|Applying migration|"
    r"Building memory|is indexed|memory is fully built",
    re.IGNORECASE,
)
SECRET = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)


def _tail(path: Path) -> list[str]:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            handle.seek(max(0, size - TAIL_BYTES))
            data = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []
    lines = data.splitlines()
    return lines[1:] if size > TAIL_BYTES else lines


def _redact(value):
    if isinstance(value, dict):
        return {
            key: (
                "<redacted>" if SECRET.search(str(key)) and isinstance(item, str) else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _session_of(detail) -> str:
    """The session a hook event names, without the host prefix."""
    if not isinstance(detail, dict):
        return ""
    for key in ("session", "session_id", "value"):
        value = detail.get(key)
        if isinstance(value, str) and re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-", value):
            return value.replace("claude_", "")
    return ""


def hook_events() -> list[dict]:
    rows, session_by_pid = [], {}
    for line in _tail(HOOK_LOG):
        start = line.find("{")
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        detail = _redact(event.get("detail"))
        pid = event.get("pid")
        session = _session_of(detail)
        if session:
            session_by_pid[pid] = session
        name = str(event.get("event") or "")
        # State lookups that miss are frequent and harmless: not errors on the page.
        noisy = name.startswith("runtime_state_")
        level = "error" if not noisy and re.search(r"fail|error|blocked|denied", name) else "info"
        rows.append(
            {
                "ts": str(event.get("ts") or "")[:19],
                "source": "plugin",
                "level": level,
                "title": name,
                "pid": pid,
                "session": session,
                "detail": detail,
            }
        )
    # A hook process logs several lines and names its session in one of them.
    for row in rows:
        row["session"] = row["session"] or session_by_pid.get(row["pid"], "")
    return rows


def server_log() -> list[dict]:
    rows = []
    for raw in _tail(SERVER_LOG):
        match = LOG_LINE.match(ANSI.sub("", raw))
        if not match or not SERVER_KEEP.search(match.group(3)):
            continue
        level = match.group(2).lower()
        text = match.group(3).strip()
        rows.append(
            {
                "ts": match.group(1)[:19],
                "source": "enola"
                if "Running enola" in text or text.startswith("enola")
                else "server",
                "level": "error" if level in ("error", "critical") else level,
                "title": text[:160],
                "session": (re.search(r"claude_([0-9a-f-]{36})", text) or [None, ""])[1],
                "detail": {"line": text},
            }
        )
    return rows


def server_runs() -> tuple[list[dict], str]:
    try:
        key = json.loads(KEY_FILE.read_text())["api_key"]
        request = urllib.request.Request(
            f"{SERVER}/api/v1/activity/pipeline-runs?limit=60", headers={"X-Api-Key": key}
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            data = json.loads(response.read())
    except Exception as error:  # the page says the server could not be asked
        return [], f"{type(error).__name__}: {str(error)[:120]}"
    rows = []
    for run in data if isinstance(data, list) else []:
        name = run.get("operation_name") or run.get("pipeline_name") or run.get("kind") or "run"
        status = str(run.get("outcome") or run.get("status") or "")
        sessions = run.get("session_ids") or []
        stages = run.get("stages")
        rows.append(
            {
                "ts": str(run.get("started_at") or run.get("created_at") or "")[:19],
                "source": "run",
                "level": "error" if re.search(r"error|fail", status, re.IGNORECASE) else "info",
                "title": f"{name}: {status}"
                + (f" ({run.get('dataset_name')})" if run.get("dataset_name") else ""),
                "session": str(sessions[0]).replace("claude_", "") if sessions else "",
                "detail": {
                    key: run.get(key)
                    for key in (
                        "id",
                        "kind",
                        "dataset_name",
                        "origin",
                        "started_at",
                        "ended_at",
                        "tokens_in",
                        "tokens_out",
                        "error_class",
                        "session_ids",
                    )
                    if run.get(key) not in (None, "", [])
                },
                "stages": stages if isinstance(stages, list) else None,
            }
        )
    return rows, ""


def server_health() -> dict:
    try:
        with urllib.request.urlopen(f"{SERVER}/health", timeout=3) as response:
            return json.loads(response.read())
    except Exception as error:
        return {"status": "unreachable", "error": f"{type(error).__name__}"}


def snapshot() -> dict:
    runs, runs_error = server_runs()
    events = hook_events() + server_log() + runs
    events.sort(key=lambda row: row["ts"], reverse=True)
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
        "server": SERVER,
        "health": server_health(),
        "runs_error": runs_error,
        "events": events[:1500],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    def _send(self, body: bytes, kind: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path.startswith("/api/events"):
            self._send(json.dumps(snapshot()).encode(), "application/json")
        elif self.path in ("/", "/index.html"):
            self._send(PAGE.read_bytes(), "text/html; charset=utf-8")
        else:
            self.send_error(404)


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Cognee inspector: http://127.0.0.1:{PORT}  (server {SERVER})", file=sys.stderr)
    server.serve_forever()
