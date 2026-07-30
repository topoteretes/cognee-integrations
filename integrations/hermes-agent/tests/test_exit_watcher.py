"""Tests for the crash-safe session close (the detached exit watcher).

The unit half exercises the state-file API and the fire logic against an
in-process HTTP server. The live half spawns the real watcher process against a
real dummy parent and kills the parent — the only way to prove the whole
detached chain (spawn, poll, fire, disarm) actually works.

Runs under pytest or standalone (``python3 tests/test_exit_watcher.py``); needs
neither cognee nor the network (servers are local, on ephemeral ports).
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cognee_integration_hermes import exit_watcher as ew  # noqa: E402


class _RecordingServer:
    """A local HTTP server that records every request and answers 200."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                outer.requests.append(
                    {
                        "path": self.path,
                        "body": json.loads(body) if body else None,
                        "api_key": self.headers.get("X-Api-Key"),
                    }
                )
                payload = b'{"ok": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args):  # keep test output quiet
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def wait_for(self, count, timeout=15.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.requests) >= count:
                return True
            time.sleep(0.05)
        return False

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _state(tmp, **overrides):
    state = {
        "parent_pid": os.getpid(),
        "url": "http://127.0.0.1:1",
        "agent_session_name": "hermes",
        "dataset": "agent_sessions",
        "session_id": "hermes_s1",
        "improve": True,
        "improve_timeout": 5.0,
        "poll_interval": 0.05,
    }
    state.update(overrides)
    path = Path(tmp) / "watcher.json"
    ew.write_state(path, state)
    return path


class TestStateFileApi(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def test_arm_writes_state_and_spawns_detached(self):
        state_path = Path(self.tmp) / "w.json"
        with mock.patch.object(ew.subprocess, "Popen") as popen:
            ew.arm(
                state_path=state_path,
                log_path=Path(self.tmp) / "w.log",
                parent_pid=123,
                url="http://127.0.0.1:8011",
                api_key="secret-key",
                agent_session_name="hermes",
                dataset="agent_sessions",
                session_id="hermes_s1",
                improve=True,
                improve_timeout=300,
            )
        state = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(state["parent_pid"], 123)
        self.assertEqual(state["session_id"], "hermes_s1")
        # The key travels via the child env, never via the state file or argv.
        self.assertNotIn("api_key", state)
        args, kwargs = popen.call_args
        self.assertEqual(args[0][0], sys.executable)
        self.assertIn("--state", args[0])
        self.assertNotIn("secret-key", " ".join(args[0]))
        self.assertEqual(kwargs["env"]["COGNEE_API_KEY"], "secret-key")
        self.assertTrue(kwargs["start_new_session"])

    def test_update_merges_fields_and_preserves_the_rest(self):
        path = _state(self.tmp, session_id="hermes_old")
        ew.update(path, session_id="hermes_new", improve=False)
        state = ew.read_state(path)
        self.assertEqual(state["session_id"], "hermes_new")
        self.assertIs(state["improve"], False)
        self.assertEqual(state["dataset"], "agent_sessions")

    def test_update_and_disarm_are_noops_without_a_state_file(self):
        missing = Path(self.tmp) / "missing.json"
        ew.update(missing, session_id="x")  # must not raise or create
        self.assertFalse(missing.exists())
        ew.disarm(missing)  # must not raise

    def test_pid_alive(self):
        self.assertTrue(ew.pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(ew.pid_alive(proc.pid))


class TestFire(unittest.TestCase):
    def setUp(self):
        self.server = _RecordingServer()
        self.addCleanup(self.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def _fire(self, **overrides):
        state = ew.read_state(_state(self.tmp, url=self.server.url, **overrides))
        with mock.patch.dict(os.environ, {"COGNEE_API_KEY": "k-env"}, clear=False):
            ew.fire(state)
        return self.server.requests

    def test_improves_then_unregisters_in_that_order(self):
        requests = self._fire()
        self.assertEqual(
            [r["path"] for r in requests],
            ["/api/v1/improve", "/api/v1/agents/unregister"],
        )
        improve = requests[0]["body"]
        self.assertEqual(improve["session_ids"], ["hermes_s1"])
        self.assertEqual(improve["dataset_name"], "agent_sessions")
        self.assertIs(improve["run_in_background"], False)
        self.assertEqual(requests[0]["api_key"], "k-env")

    def test_improve_is_skipped_when_disabled_but_unregister_still_runs(self):
        requests = self._fire(improve=False)
        self.assertEqual([r["path"] for r in requests], ["/api/v1/agents/unregister"])

    def test_a_failed_improve_does_not_block_the_unregister(self):
        state = ew.read_state(_state(self.tmp, url=self.server.url))
        real_post = ew._post

        def flaky(url, path, payload, **kwargs):
            if path == "/api/v1/improve":
                raise OSError("boom")
            return real_post(url, path, payload, **kwargs)

        with mock.patch.object(ew, "_post", side_effect=flaky):
            ew.fire(state)
        self.assertEqual([r["path"] for r in self.server.requests], ["/api/v1/agents/unregister"])


class TestLiveWatcher(unittest.TestCase):
    """The real detached chain: spawn the watcher, kill the parent, observe."""

    def setUp(self):
        self.server = _RecordingServer()
        self.addCleanup(self.server.close)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = tmp.name

    def _arm(self, parent_pid, **overrides):
        state_path = Path(self.tmp) / "watcher.json"
        params = {
            "state_path": state_path,
            "log_path": Path(self.tmp) / "watcher.log",
            "parent_pid": parent_pid,
            "url": self.server.url,
            "api_key": "k-live",
            "agent_session_name": "hermes",
            "dataset": "agent_sessions",
            "session_id": "hermes_live",
            "improve": True,
            "improve_timeout": 10.0,
            "poll_interval": 0.1,
        }
        params.update(overrides)
        ew.arm(**params)
        return state_path

    def test_an_unclean_death_improves_and_unregisters(self):
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        self.addCleanup(parent.kill)
        state_path = self._arm(parent.pid)

        parent.kill()
        parent.wait()

        self.assertTrue(
            self.server.wait_for(2),
            f"watcher never fired; log:\n{(Path(self.tmp) / 'watcher.log').read_text()}",
        )
        self.assertEqual(
            [r["path"] for r in self.server.requests],
            ["/api/v1/improve", "/api/v1/agents/unregister"],
        )
        self.assertEqual(self.server.requests[0]["body"]["session_ids"], ["hermes_live"])
        self.assertEqual(self.server.requests[0]["api_key"], "k-live")
        # The watcher cleans up after itself once it has acted.
        deadline = time.monotonic() + 5.0
        while state_path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(state_path.exists())

    def test_a_clean_disarm_means_the_watcher_never_acts(self):
        # The parent (this test process) stays alive; disarming must be enough.
        state_path = self._arm(os.getpid())
        # Give the watcher time to start and claim the state file.
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            state = ew.read_state(state_path)
            if state and state.get("watcher_pid"):
                break
            time.sleep(0.05)
        ew.disarm(state_path)
        time.sleep(0.6)  # several poll intervals
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
