"""Mid-session COGNEE_BASE_URL switch -> re-register on the new target (gh #3544).

Standalone-runnable (python3 tests/test_mid_session_reregister.py) and
pytest-discoverable. Uses in-process mock Cognee servers on ephemeral ports; no
cognee install, network, or LLM key required.
"""

import json
import os
import pathlib
import socket
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common  # noqa: E402


def _point_plugin_dirs(tmp: pathlib.Path) -> None:
    """Redirect the module's on-disk state into a throwaway temp dir."""
    _plugin_common._SHARED_PLUGIN_ROOT = tmp
    _plugin_common._PLUGIN_DIR = tmp / "claude-code"
    _plugin_common._PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    _plugin_common._SERVER_READY_MARKER = tmp / "server-ready.json"
    _plugin_common._API_KEY_CACHE = tmp / "api_key.json"
    _plugin_common._HOOK_LOG = _plugin_common._PLUGIN_DIR / "hook.log"


class _MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):  # silence
        pass

    def _record(self, method, body=""):
        self.server.calls.append(
            {
                "method": method,
                "path": self.path,
                "body": body,
                "api_key": self.headers.get("X-Api-Key", ""),
            }
        )

    def _json(self, code, payload):
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode("utf-8"))

    def do_GET(self):
        self._record("GET")
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
        body = self.rfile.read(length).decode("utf-8") if length else ""
        self._record("POST", body)
        if self.path == "/api/v1/agents/register":
            self._json(200, {"id": "conn_registered"})
        elif self.path == "/api/v1/datasets":
            self._json(201, {"name": "dataset_123"})
        elif self.path == "/api/v1/agents/unregister":
            self._json(200, {"activeAgents": 0})
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


def _closed_url() -> str:
    """A URL whose port is bound then freed -> connection refused (unreachable)."""
    s = socket.socket()
    s.bind(("localhost", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://localhost:{port}"


def _registers(calls):
    return [c for c in calls if c["path"] == "/api/v1/agents/register"]


def _run(fn):
    """Run one test with fresh plugin state and a restored environment."""
    saved_env = dict(os.environ)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="cognee-reregister-"))
    _point_plugin_dirs(tmp)
    # os.kill / check_output / Popen are stubbed so the switch touches no real
    # processes; the check_output stub keeps _find_parent_pid off real `ps`.
    with (
        patch("subprocess.Popen") as popen,
        patch("os.kill"),
        patch("subprocess.check_output", return_value=""),
    ):
        try:
            fn(popen)
        finally:
            os.environ.clear()
            os.environ.update(saved_env)


def test_switch_registers_on_new_target():
    """The connection lands on the NEW target, not the old one (issue done-when)."""

    def body(popen):
        srv_a, url_a = _start_server()
        srv_b, url_b = _start_server()
        try:
            os.environ["COGNEE_SESSION_KEY"] = "k"
            os.environ["COGNEE_BASE_URL"] = url_a
            _plugin_common.mark_server_ready(url_a)

            os.environ["COGNEE_BASE_URL"] = url_b
            _plugin_common.load_resolved()

            # Registration + dataset landed on B, and B is now the ready marker.
            assert _registers(srv_b.calls), "no register on the new target"
            assert any(c["path"] == "/api/v1/datasets" for c in srv_b.calls)
            marker = json.loads(_plugin_common._SERVER_READY_MARKER.read_text())
            assert marker["base_url"] == url_b
            # The switch must not register on the OLD target.
            assert not _registers(srv_a.calls), "unexpected register on the old target"
            # Both watchers restarted, pointed at B and flagged as watchers.
            assert popen.call_count >= 2
            scripts = [str(c[0][0]) for c in popen.call_args_list]
            envs = [c.kwargs.get("env", {}) for c in popen.call_args_list]
            assert any("idle-watcher.py" in s for s in scripts)
            assert any("exit-watcher.py" in s for s in scripts)
            assert all(e.get("COGNEE_BASE_URL") == url_b for e in envs if "COGNEE_BASE_URL" in e)
            assert all(e.get("COGNEE_IN_WATCHER") == "1" for e in envs)
        finally:
            srv_a.shutdown()
            srv_b.shutdown()

    _run(body)


def test_stale_key_not_sent_to_new_target():
    """A key cached/env-set for the OLD target must not be posted to the NEW one."""

    def body(_popen):
        srv_a, url_a = _start_server()
        srv_b, url_b = _start_server()
        try:
            os.environ["COGNEE_SESSION_KEY"] = "k"
            os.environ["COGNEE_BASE_URL"] = url_a
            os.environ["COGNEE_API_KEY"] = "key_a"
            _plugin_common.save_cached_api_key(url_a, "key_a")
            _plugin_common.mark_server_ready(url_a)

            os.environ["COGNEE_BASE_URL"] = url_b
            _plugin_common.load_resolved()

            regs = _registers(srv_b.calls)
            assert regs, "no register on the new target"
            # It must carry B's freshly minted key, never A's stale key.
            assert all(c["api_key"] != "key_a" for c in regs)
            assert regs[0]["api_key"] == "minted_key"
        finally:
            srv_a.shutdown()
            srv_b.shutdown()

    _run(body)


def test_down_target_is_skipped():
    """An unreachable new target: no re-registration, marker untouched, cooldown set."""

    def body(_popen):
        srv_a, url_a = _start_server()
        down = _closed_url()
        try:
            os.environ["COGNEE_SESSION_KEY"] = "k"
            os.environ["COGNEE_BASE_URL"] = url_a
            _plugin_common.mark_server_ready(url_a)

            os.environ["COGNEE_BASE_URL"] = down
            _plugin_common.load_resolved()

            # Marker unchanged (still A), so a later healthy hook re-tries.
            marker = json.loads(_plugin_common._SERVER_READY_MARKER.read_text())
            assert marker["base_url"] == url_a
            assert (_plugin_common._PLUGIN_DIR / "reregister-cooldown.json").exists()
        finally:
            srv_a.shutdown()

    _run(body)


if __name__ == "__main__":
    tests = [
        test_switch_registers_on_new_target,
        test_stale_key_not_sent_to_new_target,
        test_down_target_is_skipped,
    ]
    failures = 0
    for t in tests:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as exc:
            failures += 1
            import traceback

            traceback.print_exc()
            print("FAIL", t.__name__, exc)
    sys.exit(1 if failures else 0)
