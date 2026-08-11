"""Memory degrades; it never breaks the agent.

The plugin sits on the hot path of every prompt and every tool call, so its
failure mode matters more than its success mode: a cognee outage must cost the
user their memory, never their session. These scenarios kill the server underneath
a live session and assert the hooks stay quiet and successful, that the write is
buffered rather than dropped, and that a cold server's slow first query is
classified honestly instead of tripping the breaker.

No cognify here — nothing needs the graph — so these are cheap to run.
"""

from __future__ import annotations

import pytest
from utils.live import (
    hook_events,
    kill_server,
    pending_entries,
    read_last_recall,
    server_health,
)

pytestmark = pytest.mark.live


def test_hooks_stay_successful_when_the_server_dies_mid_session(
    started_session, live_suite, live_home, live_port, live_base_url, nonce
):
    """The agent must not notice. Every hook exits 0 with the backend gone."""
    session = started_session("outage")

    session.prompt(f"Before the outage: {nonce} uses raft.", turn_id="t1")
    session.answer(f"Noted: {nonce} uses raft.", turn_id="t1")

    # kill_server waits for the port to go quiet: SIGTERM lets uvicorn finish
    # in-flight work, so probing immediately after kill still sees 200.
    killed = kill_server(live_base_url, live_port)
    assert killed, "expected a running server to kill — the test proves nothing otherwise"
    assert server_health(live_base_url) is None

    # Every hook on the hot path, against a backend that is simply gone.
    after_prompt = session.prompt(f"During the outage: {nonce} also needs quorum 3.", turn_id="t2")
    after_tool = session.tool("Bash", {"command": "echo hi"}, "hi", turn_id="t2")
    after_answer = session.answer("Quorum 3 noted.", turn_id="t2")
    after_recall = session.recall(f"What do we know about {nonce}?", turn_id="t3")

    for label, run in (
        ("UserPromptSubmit", after_prompt),
        ("PostToolUse", after_tool),
        ("Stop", after_answer),
        ("context lookup", after_recall),
    ):
        assert run.ok, f"{label} failed during the outage (rc={run.returncode}): {run.stderr[:500]}"

    # A recall with no backend yields no memory — and says so, rather than raising.
    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    assert sum(int(v or 0) for v in hits.values()) == 0, (
        f"recall claimed hits with the server down: {hits}"
    )


def test_writes_during_an_outage_are_buffered_not_dropped(
    started_session, live_suite, live_home, live_port, live_base_url, nonce, request
):
    """A turn captured during an outage must survive it.

    KNOWN GAP, not a design choice. ``store-to-session.py`` buffers to the warmup
    spillway only when ``server_usable()`` is already False — a *stale* ready
    marker plus a failed probe. The marker has a 30s TTL, so a server that dies
    inside that window leaves ``server_usable()`` returning True: the hook
    attempts a real write, the write raises, and the ``except`` branch only logs
    ``stop_store_error``. The entry is buffered nowhere and the turn is lost,
    which is precisely what the spillway exists to prevent.

    Strict xfail so this turns red the moment the failure path learns to buffer —
    the fix is to call ``append_warmup_entry`` in that ``except`` branch, as the
    not-usable path already does.
    """
    request.node.add_marker(
        pytest.mark.xfail(
            reason=(
                "mid-outage Stop write is not buffered when the ready marker is "
                "still fresh (store-to-session.py except branch only logs)"
            ),
            strict=True,
        )
    )

    session = started_session("buffer")
    session.prompt(f"Pre-outage note for {nonce}.", turn_id="t1")
    session.answer("Noted.", turn_id="t1")

    kill_server(live_base_url, live_port)

    session.prompt(f"Mid-outage note for {nonce}: the quorum is 5.", turn_id="t2")
    session.answer(f"{nonce} quorum is 5.", turn_id="t2")

    events = [event for event, _ in hook_events(live_suite, live_home)]
    buffered = pending_entries(live_suite, live_home)
    assert buffered, (
        "nothing was buffered while the server was down — that turn is lost. "
        f"last events: {events[-8:]}"
    )


def test_a_slow_cold_query_is_classified_slow_not_down(
    started_session, live_env, live_suite, live_home, graph, nonce
):
    """Production recall timeouts are tight on purpose; exceeding them is not an outage.

    With ``COGNEE_RECALL_TIMEOUT`` at its real 2.5s default, the first graph query
    against a freshly booted server does not finish in time. The plugin must treat
    that as "slow" — no memory this prompt, no breaker trip, no failure state that
    would redden the status line — rather than concluding the server is down.

    This pins the behaviour the rest of this tier deliberately tunes away: the
    other tests raise the timeouts so they can ask about memory instead of speed.
    """
    # Write something worth finding, with the tier's generous timeouts.
    writer = started_session("cold-writer")
    writer.prompt(f"For {nonce} we chose Paxos for leader election.", turn_id="t1")
    writer.answer(f"{nonce}: Paxos, majority quorum.", turn_id="t1")
    writer.end()
    assert writer.wait_for_sync(deadline=600.0) is not None
    graph.wait_until_recalled(f"What did we choose for {nonce}?", "paxos", deadline=600.0)

    # Now restore the production budget for a fresh session's first prompt. The
    # env dict is shared with the sessions, so mutating it here is what a real
    # deployment's defaults would give us.
    live_env["COGNEE_RECALL_TIMEOUT"] = "2.5"
    live_env["COGNEE_RECALL_BUDGET"] = "4"

    cold = started_session("cold-reader")
    lookup = cold.recall(f"Remind me what {nonce} uses.", turn_id="t1")
    assert lookup.ok, f"a slow recall must not fail the hook: {lookup.stderr[:500]}"

    events = dict(hook_events(live_suite, live_home))
    # Whatever happened, it must not have been read as an outage.
    assert "recall_breaker_open" not in events, "a slow query tripped the circuit breaker"
    slow_verdicts = [
        detail
        for event, detail in hook_events(live_suite, live_home)
        if event == "recall_error" and detail.get("verdict") == "slow"
    ]
    down_verdicts = [
        detail
        for event, detail in hook_events(live_suite, live_home)
        if event == "recall_error" and detail.get("verdict") in ("down", "unreachable")
    ]
    assert not down_verdicts, f"a healthy-but-slow server was judged down: {down_verdicts}"

    # Either it beat the deadline (fine) or it was recorded as slow (also fine).
    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    recalled = sum(int(v or 0) for v in hits.values())
    assert recalled > 0 or slow_verdicts, (
        f"recall returned nothing and logged no slow verdict — unexplained miss. hits={hits}"
    )

    cold.end()
