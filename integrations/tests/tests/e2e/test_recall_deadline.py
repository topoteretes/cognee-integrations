"""A server that accepts and never answers must not hold the prompt.

The fan-out pushes every scope's blocking request onto a worker thread and
awaits them together; ``asyncio.run`` then waits for those workers at shutdown.
Since the one-request memory contract of cognee 1.6.0 (SDK-741) a plain prompt
makes exactly one such request, the graph scope. The one way this could stall a
prompt is a worker that does not come back at the deadline — so this drives the
real hook, as a subprocess, against a socket that accepts the connection and
then says nothing, and pins:

  * the hook exits 0 (memory degrades, the agent never notices);
  * the memory request was dispatched, timed out at the deadline, and was
    classified ``slow`` — never ``down``, never a written health verdict;
  * the recall's own aggregate ``elapsed_ms`` stays within the budget, and the
    whole process returns promptly rather than hanging on its worker threads.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest
from utils.hooklog import hook_events

#: The recall budget handed to the hook, in seconds: the request's deadline.
#: Well above MIN_SCOPE_TIMEOUT so the scope is dispatched, well below the
#: default so the test is quick.
DEADLINE_S = 1.0


class _BlackHole:
    """Accepts TCP connections and holds them open without ever replying."""

    def __init__(self) -> None:
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self._held: list[socket.socket] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, name="black-hole", daemon=True)
        self.accepted = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._sock.getsockname()[1]}"

    def start(self) -> "_BlackHole":
        self._thread.start()
        return self

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self.accepted += 1
            self._held.append(conn)  # never read, never written, never closed here

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=2.0)
        for conn in self._held:
            try:
                conn.close()
            except OSError:
                pass
        self._sock.close()


@pytest.fixture
def black_hole():
    server = _BlackHole().start()
    try:
        yield server
    finally:
        server.close()


def test_a_silent_server_costs_one_deadline_and_the_hook_still_exits_clean(
    suite, run_hook, black_hole, payloads, temp_home, assert_clean_real_home
):
    started = time.monotonic()
    result = run_hook(
        suite,
        "session-context-lookup.py",
        stdin=payloads.user_prompt(prompt="what did we decide about the retry policy?"),
        service_url=black_hole.url,
        env={"COGNEE_RECALL_BUDGET": str(DEADLINE_S)},
        # Generous: a hang here is exactly the bug. A healthy run is interpreter
        # start-up plus one deadline.
        timeout=60.0,
    )
    wall = time.monotonic() - started
    assert result.returncode == 0, result.stderr

    events = hook_events(suite, temp_home)
    errors = [d for e, d in events if e == "recall_error"]
    assert len(errors) == 1, f"expected the one memory request to time out once: {errors}"
    assert [tuple(d["scope"]) for d in errors] == [("graph",)], errors
    assert all(d["verdict"] == "slow" for d in errors), (
        f"a server that accepts but never answers is slow, not down: {errors}"
    )
    assert not [d for e, d in events if e == "recall_server_down"], events
    assert not [d for e, d in events if e == "recall_budget_exceeded"], events
    assert black_hole.accepted >= 1, (
        f"the memory request must have reached the socket: {black_hole.accepted}"
    )

    summary = next(
        (d for e, d in events if e in ("context_lookup_empty", "context_lookup_hit")), None
    )
    assert summary is not None, events
    per_scope = summary["per_scope"]
    assert all(not r.get("skipped") for r in per_scope.values()), per_scope
    # The request ran for about one deadline — and so did the recall as a
    # whole: nothing else was in flight for it to wait on.
    for label, record in per_scope.items():
        assert DEADLINE_S * 1000 * 0.9 <= record["elapsed_ms"] <= DEADLINE_S * 1000 * 2, (
            label,
            record,
        )
    assert summary["elapsed_ms"] < DEADLINE_S * 1000 * 2, (
        f"the recall took more than two deadlines against one request: {summary}"
    )
    # And the process did not linger on its worker threads after the deadline.
    assert wall < DEADLINE_S + 20.0, f"hook took {wall:.1f}s against a {DEADLINE_S}s deadline"
