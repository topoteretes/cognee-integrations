import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

# Adjust sys.path to find the scripts folder
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

# Redirect plugin directory to a temp folder BEFORE importing common plugin module
_TMP_DIR = tempfile.mkdtemp(prefix="cognee-reregister-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP_DIR

import _plugin_common  # noqa: E402

# Force path variables to point to our temp folder so we don't mess up real data
_TMP_PATH = pathlib.Path(_TMP_DIR)
_plugin_common._SHARED_PLUGIN_ROOT = _TMP_PATH
_plugin_common._PLUGIN_DIR = _TMP_PATH / "claude-code"
_plugin_common._HOOK_LOG = _plugin_common._PLUGIN_DIR / "hook.log"
_plugin_common._COUNTER_FILE = _plugin_common._PLUGIN_DIR / "counter.json"
_plugin_common._ACTIVITY_FILE = _plugin_common._PLUGIN_DIR / "activity.ts"
_plugin_common._ACTIVITY_LOG = _plugin_common._PLUGIN_DIR / "activity.log"
_plugin_common._SAVE_COUNTER = _plugin_common._PLUGIN_DIR / "save_counter.json"
_plugin_common._SERVER_READY_MARKER = _TMP_PATH / "server-ready.json"
_plugin_common._SYNC_LOCK = _plugin_common._PLUGIN_DIR / "sync.lock"
_plugin_common._BRIDGE_DIR = _plugin_common._PLUGIN_DIR / "bridge"
_plugin_common._PENDING_DIR = _plugin_common._PLUGIN_DIR / "pending"
_plugin_common._SUBPROCESS_LOG = _plugin_common._PLUGIN_DIR / "subprocess.log"
_plugin_common._API_KEY_CACHE = _TMP_PATH / "api_key.json"
_plugin_common._SESSIONS_MAP_DIR = _plugin_common._PLUGIN_DIR / "sessions"


class MockServerRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        self.server.calls.append(("GET", self.path))
        if self.path == "/health":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path.startswith("/api/v1/users/me"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "user_123"}).encode("utf-8"))
        elif self.path.startswith("/api/v1/agents/connections/me"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"agent": {"agent_session_name": "some_conn", "status": "active"}}).encode("utf-8"))
        elif self.path.startswith("/api/v1/auth/api-keys"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps([{"key": "test_api_key"}]).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8") if content_length > 0 else ""
        self.server.calls.append(("POST", self.path, body))

        if self.path == "/api/v1/agents/register":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"id": "conn_registered"}).encode("utf-8"))
        elif self.path == "/api/v1/datasets":
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"name": "dataset_123"}).encode("utf-8"))
        elif self.path == "/api/v1/agents/unregister":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"activeAgents": 0}).encode("utf-8"))
        elif self.path == "/api/v1/auth/login":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"access_token": "token_123"}).encode("utf-8"))
        elif self.path == "/api/v1/auth/api-keys":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"key": "test_api_key"}).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()


def test_mid_session_reregister():
    # Spin up Server A and Server B on dynamic ports
    server_a = HTTPServer(("localhost", 0), MockServerRequestHandler)
    server_a.calls = []
    thread_a = threading.Thread(target=server_a.serve_forever)
    thread_a.daemon = True
    thread_a.start()

    server_b = HTTPServer(("localhost", 0), MockServerRequestHandler)
    server_b.calls = []
    thread_b = threading.Thread(target=server_b.serve_forever)
    thread_b.daemon = True
    thread_b.start()

    url_a = f"http://localhost:{server_a.server_port}"
    url_b = f"http://localhost:{server_b.server_port}"

    # Setup the initial session on Server A
    os.environ["COGNEE_SESSION_KEY"] = "test-session-key"
    os.environ["COGNEE_BASE_URL"] = url_a
    _plugin_common.mark_server_ready(url_a)

    # Resolve initial status to make sure we're on Server A
    resolved = _plugin_common.load_resolved()
    assert resolved.get("base_url") == url_a

    # Now change COGNEE_BASE_URL to Server B
    os.environ["COGNEE_BASE_URL"] = url_b

    # Mock subprocess.Popen and os.kill so we don't start real processes or kill things
    with patch("subprocess.Popen") as mock_popen, patch("os.kill") as mock_kill:
        mock_popen.return_value = MagicMock()

        # Trigger load_resolved, which should detect the mismatch
        resolved_new = _plugin_common.load_resolved()

        # Assertions:
        # 1. Base URL has switched to B
        assert resolved_new.get("base_url") == url_b

        # 2. server-ready.json was updated to url_b
        assert _plugin_common._SERVER_READY_MARKER.exists()
        ready_data = json.loads(_plugin_common._SERVER_READY_MARKER.read_text(encoding="utf-8"))
        assert ready_data.get("base_url") == url_b

        # 3. Server B received the registration and dataset requests
        register_calls = [c for c in server_b.calls if c[1] == "/api/v1/agents/register"]
        dataset_calls = [c for c in server_b.calls if c[1] == "/api/v1/datasets"]
        assert len(register_calls) > 0
        assert len(dataset_calls) > 0

        # 4. Mock popen was called to restart watchers with B config
        # (check that popen args contain B's url)
        assert mock_popen.call_count >= 2
        called_args = [call[0][0] for call in mock_popen.call_args_list]
        called_envs = [call[1].get("env", {}) for call in mock_popen.call_args_list]

        # Verify idle-watcher and exit-watcher arguments
        assert any("idle-watcher.py" in str(arg) for arg in called_args)
        assert any("exit-watcher.py" in str(arg) for arg in called_args)
        assert any(url_b in str(env.get("COGNEE_BASE_URL")) for env in called_envs)

    # Clean up mock servers
    server_a.shutdown()
    server_b.shutdown()
    thread_a.join()
    thread_b.join()


if __name__ == "__main__":
    try:
        test_mid_session_reregister()
        print("PASS test_mid_session_reregister")
        sys.exit(0)
    except AssertionError as e:
        import traceback
        traceback.print_exc()
        print("FAIL test_mid_session_reregister")
        sys.exit(1)
