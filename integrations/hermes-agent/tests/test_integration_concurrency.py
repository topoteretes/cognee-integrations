"""Integration tests for the core motivation of local-server mode: a real server
spawn, and concurrent operations that must NOT raise "database is locked".

These require the full cognee stack (and LLM creds for the write path), so they
are **opt-in**: set ``COGNEE_RUN_INTEGRATION=1`` to run them. They are skipped in
the default unit run (and in CI, which has neither cognee installed nor creds).
The unit suite in ``test_server_mode.py`` covers the routing logic with mocks;
this file covers the behavior that can only be observed against a live server.

Run locally (needs cognee installed + LLM creds for the write path):
    COGNEE_RUN_INTEGRATION=1 uv run pytest tests/test_integration_concurrency.py
"""

import importlib.util
import os
import socket
import sys
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_RUN = os.environ.get("COGNEE_RUN_INTEGRATION") == "1"
_HAS_COGNEE = importlib.util.find_spec("cognee") is not None
_REASON = "set COGNEE_RUN_INTEGRATION=1 and install cognee to run integration tests"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _looks_like_lock_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "database is locked" in text or "database table is locked" in text


@unittest.skipUnless(_RUN and _HAS_COGNEE, _REASON)
class TestRealServerSpawn(unittest.TestCase):
    def test_ensure_local_server_spawns_and_serves_health(self):
        from cognee_integration_hermes import server_bootstrap as sb

        port = _free_port()
        url = sb.ensure_local_server(port, boot_timeout=90.0)
        self.assertEqual(url, f"http://127.0.0.1:{port}")
        self.assertTrue(sb.health_ok(url), "server should answer /health after spawn")


@unittest.skipUnless(_RUN and _HAS_COGNEE, _REASON)
class TestConcurrentNoLocking(unittest.TestCase):
    def test_concurrent_remember_recall_no_database_locked(self):
        from cognee_integration_hermes import CogneeMemoryProvider

        os.environ.setdefault("COGNEE_LOCAL_PORT", str(_free_port()))
        provider = CogneeMemoryProvider()
        provider.initialize("integration-concurrency")

        errors: list[BaseException] = []

        def worker(i: int) -> None:
            try:
                provider.handle_tool_call(
                    "cognee_remember",
                    {"content": f"concurrency probe {i}: the sky is blue"},
                )
                provider.handle_tool_call("cognee_recall", {"query": "what colour is the sky"})
            except BaseException as exc:  # noqa: BLE001 — we inspect every failure
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(worker, range(16)))

        lock_errors = [e for e in errors if _looks_like_lock_error(e)]
        self.assertEqual(lock_errors, [], f"got DB-lock errors under concurrency: {lock_errors}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
