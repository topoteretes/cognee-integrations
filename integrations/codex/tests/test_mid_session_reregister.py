"""Codex mirror of the mid-session COGNEE_BASE_URL switch test (gh #3544).

The re-registration block is byte-identical to claude-code's; this focused smoke
test guards the codex-specific wiring (state dir, watcher script paths, imports).
Standalone-runnable and pytest-discoverable; no cognee/network/LLM key needed.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

_SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"
sys.path.insert(0, str(_SCRIPTS))

import _plugin_common  # noqa: E402


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self):
        self.server.calls.append(("GET", self.path))
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path.startswith("/api/v1/users/me"):
            self._json(200, {"id": "user_123"})
        elif self.path.startswith("/api/v1/agents/connections/me"):
            self._json(200, {"agent": {"agent_session_name": "conn", "status": "active"}})
        elif self.path.startswith("/api/v1/auth/api-keys"):
            self._json(200, [{"key": "minted_key"}])
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length) if length else b""
        self.server.calls.append(("POST", self.path))
        if self.path == "/api/v1/agents/register":
            self._json(200, {"id": "conn_registered"})
        elif self.path == "/api/v1/datasets":
            self._json(201, {"name": "dataset_123"})
        elif self.path == "/api/v1/auth/login":
            self._json(200, {"access_token": "token_123"})
        elif self.path == "/api/v1/auth/api-keys":
            self._json(200, {"key": "minted_key"})
        else:
            self.send_response(404)
            self.end_headers()


def _start_server():
    srv = HTTPServer(("localhost", 0), _MockHandler)
    srv.calls = []
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://localhost:{srv.server_port}"


def test_switch_registers_on_new_target():
    saved_env = dict(os.environ)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cognee-reregister-codex-"))
    _plugin_common._SHARED_PLUGIN_ROOT = tmp
    _plugin_common._PLUGIN_DIR = tmp / "codex"
    _plugin_common._PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    _plugin_common._SERVER_READY_MARKER = tmp / "server-ready.json"
    _plugin_common._API_KEY_CACHE = tmp / "api_key.json"
    _plugin_common._HOOK_LOG = _plugin_common._PLUGIN_DIR / "hook.log"

    srv_a, url_a = _start_server()
    srv_b, url_b = _start_server()
    with (
        patch("subprocess.Popen"),
        patch("os.kill"),
        patch("subprocess.check_output", return_value=""),
    ):
        try:
            os.environ["COGNEE_SESSION_KEY"] = "k"
            os.environ["COGNEE_BASE_URL"] = url_a
            _plugin_common.mark_server_ready(url_a)

            os.environ["COGNEE_BASE_URL"] = url_b
            _plugin_common.load_resolved()

            assert any(c[1] == "/api/v1/agents/register" for c in srv_b.calls)
            assert any(c[1] == "/api/v1/datasets" for c in srv_b.calls)
            marker = json.loads(_plugin_common._SERVER_READY_MARKER.read_text())
            assert marker["base_url"] == url_b
            assert not any(c[1] == "/api/v1/agents/register" for c in srv_a.calls)
        finally:
            srv_a.shutdown()
            srv_b.shutdown()
            os.environ.clear()
            os.environ.update(saved_env)


if __name__ == "__main__":
    try:
        test_switch_registers_on_new_target()
        print("PASS test_switch_registers_on_new_target")
        sys.exit(0)
    except AssertionError:
        import traceback

        traceback.print_exc()
        print("FAIL test_switch_registers_on_new_target")
        sys.exit(1)
