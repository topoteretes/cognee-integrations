"""Memory degrades; it never breaks the agent.

The plugin sits on the hot path of every prompt and every tool call, so its
failure mode matters more than its success mode: a cognee outage must cost the
user their memory, never their session. These scenarios kill the server underneath
a live session and assert the hooks stay quiet and successful, that the write is
buffered rather than dropped, and that a cold server's slow first query is
classified honestly instead of tripping the breaker.

No cognify here — nothing needs the graph — so these are cheap to run.

**Local backend only.** Every scenario works by killing the server underneath a
running session, which is only meaningful for a server this machine booted. Against
a cloud tenant the kill is impossible (and the attempt would target whatever local
process holds the tenant's port), so the whole module is marked ``local_only`` and
deselected there. This is the one part of the tier that cloud genuinely cannot
cover, which is worth stating plainly rather than discovering from a red run.
"""

from __future__ import annotations

import time

import pytest
from utils.live import (
    hook_events,
    kill_server,
    pending_entries,
    read_last_recall,
    server_health,
)

pytestmark = [pytest.mark.live, pytest.mark.local_only]


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
    started_session, live_suite, live_home, live_port, live_base_url, nonce
):
    """A turn captured during an outage must survive it.

    The obvious guard is not enough on its own. ``store-to-session.py`` checks
    ``server_usable()`` before writing, but the ready marker has a 30s TTL, so a
    server that dies inside that window leaves it returning True: the hook attempts
    a real write, the write raises, and for a long time the ``except`` branch only
    logged ``stop_store_error``. The turn was buffered nowhere and lost — precisely
    what the warmup spillway exists to prevent.

    Both failure paths now buffer, and both discriminate: transport failures and
    5xx are replayed, a 4xx is dropped loudly. That distinction is not fussiness —
    ``drain_warmup_entries`` stops at the first entry it cannot send and only trims
    what it drained, so an entry that can never succeed would sit at the head of the
    queue and block everything behind it indefinitely.

    Was a strict xfail; the fix made it XPASS on both suites.
    """
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


def test_buffered_turns_are_replayed_once_the_server_returns(
    started_session, live_suite, live_home, live_port, live_base_url, nonce
):
    """The spillway, end to end: buffer while down, replay when back.

    This is the path the mid-outage test above does *not* reach.
    ``server_usable()`` only reports False once the ready marker has gone stale
    (30s TTL, ``_SERVER_READY_TTL_SECONDS``) **and** a probe fails — so the test
    waits out the marker before capturing. That is exactly the real warmup case:
    the plugin knows the server is unavailable, so it buffers instead of trying.

    Then a fresh session boots the server again and its prompt hook drains the
    buffer, which is what makes an outage cost nothing permanent.
    """
    session = started_session("spillway")
    kill_server(live_base_url, live_port)

    # Wait out the ready marker so server_usable() actually reports False.
    time.sleep(35)
    assert server_health(live_base_url) is None, "server came back on its own"

    session.prompt(f"Captured while down: {nonce} uses a 5-node quorum.", turn_id="t1")
    session.answer(f"{nonce}: 5-node quorum.", turn_id="t1")

    buffered = pending_entries(live_suite, live_home)
    events = [event for event, _ in hook_events(live_suite, live_home)]
    assert buffered, (
        "the turn was not buffered even with a stale marker and a dead server — "
        f"the spillway never engaged. Events: {events[-10:]}"
    )
    assert "store_buffered_warming" in events, (
        f"no store_buffered_warming event recorded. Events: {events[-10:]}"
    )

    # Bring the server back. Only SessionStart boots one, so a second session
    # plays the part of the user opening a new terminal after a restart.
    started_session("revived")

    # The drain must then be driven by the *stranded* session, not the new one:
    # the warmup buffer is per-session (``_bridge_file(session_id)``), so each
    # session replays only what it buffered. This mirrors the real recovery —
    # the terminal that was open when the server died prompts again and its own
    # backlog goes out. The drain also rides on the prompt hook, so polling the
    # file alone would never see it move, and a drain that failed during the
    # outage arms a ~60s backoff worth prompting past.
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        session.prompt("Back online — anything, to trigger the drain.", turn_id="t2")
        if not pending_entries(live_suite, live_home):
            break
        time.sleep(15)

    leftover = pending_entries(live_suite, live_home)
    drain_events = [
        event
        for event, _ in hook_events(live_suite, live_home)
        if "drain" in event or "warmup" in event
    ]
    assert not leftover, (
        f"{len(leftover)} entr(ies) never replayed after the server returned. "
        f"Drain-related events: {drain_events[-8:]}. First leftover: {leftover[0]}"
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
