"""The per-prompt recall against a real server (local or cloud), under load.

Since cognee 1.6.0 a prompt's memory is one graph-scope request (plus the code
lane on identifier-shaped prompts), so there is no scope fan-out left whose
overlap could be timed. What only a real backend can still answer:

* **Does a burst of recalls get throttled or refused?** A 429 (or any scope
  error) on a healthy server is the regression to catch — and the cloud tenant
  is where it would show first.
* **Do several sessions recalling at once contend on the graph store?** A
  locked or contended local store would surface as a scope error or an empty
  recall.

Both backends run the burst. The parallel-sessions scenario is local-only:
against a cloud tenant several sessions share the tenant's rate limits, which
is the burst question again rather than the graph-store question it asks here.
"""

from __future__ import annotations

import concurrent.futures

import pytest
from utils.live import hook_events

pytestmark = pytest.mark.live

#: Recalls issued back to back in one session, which is what a user typing five
#: prompts produces.
BURSTS = 5


def _recall_summaries(suite, home) -> list[dict]:
    return [
        d
        for e, d in hook_events(suite, home)
        if e in ("context_lookup_hit", "context_lookup_empty")
    ]


def _scope_errors(suite, home) -> list[dict]:
    return [d for e, d in hook_events(suite, home) if e == "recall_error"]


def test_a_burst_of_concurrent_scopes_is_neither_throttled_nor_refused(
    started_session, live_suite, live_home, nonce
):
    """Five prompts' worth of fan-out on a healthy server: zero scope errors."""
    session = started_session("fanout")
    session.prompt(f"Project {nonce} uses a three-node quorum.", turn_id="t1")
    session.answer(f"Noted: {nonce} uses a three-node quorum.", turn_id="t1")

    for i in range(BURSTS):
        run = session.recall(f"What do we know about {nonce}? (round {i})", turn_id=f"r{i}")
        assert run.ok, f"recall {i} failed (rc={run.returncode}): {run.stderr[:500]}"

    errors = _scope_errors(live_suite, live_home)
    throttled = [d for d in errors if "429" in str(d.get("error", ""))]
    assert not throttled, f"the concurrent fan-out was rate-limited: {throttled}"
    # A fresh dataset's graph scope may answer 404 until the first cognify; that
    # is recorded separately (recall_graph_not_built), so anything here is real.
    assert not errors, f"scope errors on a healthy server: {errors}"

    summaries = _recall_summaries(live_suite, live_home)
    assert len(summaries) >= BURSTS, summaries
    for summary in summaries[-BURSTS:]:
        skipped = [k for k, r in summary["per_scope"].items() if r.get("skipped")]
        assert not skipped, f"scopes were never dispatched: {skipped} in {summary}"


@pytest.mark.local_only
def test_parallel_sessions_recall_at_once_without_errors(
    synced_turn, started_session, live_suite, live_home, nonce
):
    """Three terminals prompting at the same moment: concurrent graph-store reads.

    Recall reads the graph only, so the dataset gets one real sync first —
    without a graph every recall answers 404 and never touches the store this
    test is about. Each session then captures its own fact: every hook must
    exit clean, every scope must have run, no scope may report an error (a
    locked or contended store would surface here), and every one of the
    concurrent recalls must actually return memory.
    """
    synced_turn(
        "fanout-par-seed",
        f"Project {nonce} is released by the platform team.",
        f"Noted: {nonce} releases are owned by the platform team.",
        f"Who releases {nonce}?",
        "platform",
    )

    sessions = [started_session(f"fanout-par-{i}") for i in range(3)]
    for i, session in enumerate(sessions):
        session.prompt(f"Session {i} of {nonce} ships on Tuesdays.", turn_id="t1")
        session.answer(f"Noted: session {i} of {nonce} ships on Tuesdays.", turn_id="t1")

    def recall(pair):
        i, session = pair
        return session.recall(f"When does session {i} of {nonce} ship?", turn_id="t2")

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(sessions)) as pool:
        runs = list(pool.map(recall, enumerate(sessions)))

    for i, run in enumerate(runs):
        assert run.ok, f"parallel recall {i} failed (rc={run.returncode}): {run.stderr[:500]}"

    errors = _scope_errors(live_suite, live_home)
    assert not errors, f"scope errors under parallel sessions: {errors}"
    summaries = _recall_summaries(live_suite, live_home)[-len(sessions) :]
    assert len(summaries) == len(sessions), summaries
    for summary in summaries:
        assert all(not r.get("skipped") for r in summary["per_scope"].values()), summary
        found = sum(int(r.get("hits") or 0) for r in summary["per_scope"].values())
        assert found > 0, f"a concurrent recall came back empty against a built graph: {summary}"
