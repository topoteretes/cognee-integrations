"""End-to-end test: a started session registers and is listable via connections/me.

Drives the real plugin registration path against a stand-in Cognee agents server
over a genuine HTTP round-trip:

  register_agent_via_http()  ->  POST /api/v1/agents/register
  load_resolved()            ->  GET  /api/v1/agents/connections/me?agent_session_name=<conn_uuid>

and asserts the started session then appears in the connections listing as an
active/registered connection. This guards the regression where an active session
stops being listable (e.g. a mid-session mode flip), which is exactly what
`/agents/connections/me` is queried for on the hot path.

Run: python integrations/claude-code/tests/test_agent_register_e2e.py (or via pytest).
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402

# Redirect the plugin's on-disk state (session map + hook log) into a throwaway
# dir so the test never reads or writes the developer's real ~/.cognee-plugin.
_TMP = pathlib.Path(tempfile.mkdtemp(prefix="cognee-e2e-"))
pc._PLUGIN_DIR = _TMP / "claude-code"
pc._SESSIONS_MAP_DIR = pc._PLUGIN_DIR / "sessions"
pc._HOOK_LOG = pc._PLUGIN_DIR / "hook.log"


class _AgentsHandler(BaseHTTPRequestHandler):
    """Minimal stand-in for the Cognee agents API the plugin hooks talk to."""

    agents: dict = {}

    def log_message(self, *args):  # keep test output quiet
        pass

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}") if length else {}
        except Exception:
            payload = {}
        name = str(payload.get("agent_session_name") or "")
        if self.path.startswith("/api/v1/agents/register"):
            type(self).agents[name] = {
                "agent_session_name": name,
                "user_id": "e2e-user",
                "status": "active",
            }
            self._send(200, {"agent_session_name": name, "status": "active"})
        elif self.path.startswith("/api/v1/agents/unregister"):
            type(self).agents.pop(name, None)
            self._send(200, {"activeAgents": len(type(self).agents)})
        else:
            self._send(404, {"detail": "not found"})

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/users/me":
            self._send(200, {"id": "e2e-user"})
        elif parsed.path == "/api/v1/agents/connections/me":
            wanted = parse_qs(parsed.query).get("agent_session_name", [""])[0]
            self._send(200, {"agent": type(self).agents.get(wanted) or {}})
        else:
            self._send(404, {"detail": "not found"})


class _AgentsServer:
    """Context manager that runs `_AgentsHandler` on an ephemeral local port."""

    def __enter__(self):
        _AgentsHandler.agents.clear()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _AgentsHandler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        host, port = self._server.server_address
        self._prev = {k: os.environ.get(k) for k in ("COGNEE_LOCAL_API_URL", "COGNEE_API_KEY")}
        os.environ["COGNEE_LOCAL_API_URL"] = f"http://{host}:{port}"
        os.environ["COGNEE_API_KEY"] = "e2e-key"
        return self

    def __exit__(self, *exc):
        self._server.shutdown()
        self._server.server_close()
        for key, value in self._prev.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return False


def test_started_session_registers_and_is_listable():
    with _AgentsServer():
        session_key = pc._sanitize_session_key("claude_e2e-listable-session")
        conn_uuid = pc.resolve_conn_uuid(session_key)
        assert conn_uuid  # a per-launch registration handle was minted

        ok, _ = pc.register_agent_via_http(agent_session_name=conn_uuid, session_id="sess-e2e")
        assert ok is True
        assert conn_uuid in _AgentsHandler.agents  # server recorded the registration

        resolved = pc.load_resolved(session_key)
        # The started session is listable via /agents/connections/me ...
        assert resolved.get("agent_session_name") == conn_uuid
        # ... and surfaced as an active/registered connection.
        assert resolved.get("registered") is True


def test_unregistered_session_is_not_listed():
    # The failure mode this E2E guards against: a session that isn't registered
    # (or whose connection went away) must not report as an active connection.
    with _AgentsServer():
        resolved = pc.load_resolved(pc._sanitize_session_key("claude_e2e-unregistered-session"))
        assert resolved.get("registered") is not True


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
