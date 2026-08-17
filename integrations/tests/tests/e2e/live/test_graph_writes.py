"""What the graph must and must not contain after repeated writes.

Two properties that only a real graph can demonstrate:

* **Isolation** — the dataset is the scoping key (cognee >= 1.4.1), so two
  projects sharing a machine must not see each other's memory. Tested by
  populating *both* datasets and showing neither surfaces the other's nonce.
  An earlier attempt queried a dataset that was never created, which probes
  "what happens with an unknown dataset" instead of the actual boundary.
* **One final sync per session** — SessionEnd, the exit-watcher's PID poll and a
  manual sync can all fire around one shutdown, so the plugin claims a
  once-per-session marker before doing the work. Without it each would re-drive a
  full drain + improve. Note this is the plugin's guarantee; whether the *graph*
  ends up with one copy of a fact is cognee's own content-hash concern and is
  deliberately not asserted here.

These are the expensive tests in the tier: each writes to the graph, so each pays
for a real cognify.
"""

from __future__ import annotations

import pytest
from utils.live import GraphClient, hook_events, wait_for_event

pytestmark = pytest.mark.live


def test_two_datasets_do_not_leak_into_each_other(
    started_session, live_env, live_base_url, live_home, nonce
):
    """Both datasets get real content; neither may answer with the other's."""
    other_dataset = f"{live_env['COGNEE_PLUGIN_DATASET']}_b"
    nonce_b = f"{nonce}-B"

    # An open session held for the whole test, purely to keep the server alive.
    #
    # The plugin boots uvicorn in agent mode, which tears the server down once the
    # last agent disconnects — and `end()` unregisters (`active_agents: 0`). So
    # after the final session ends there is nothing holding the port open, and the
    # graph assertions below get ECONNREFUSED even though `sync_bridge_done`
    # reported `wrote: True`. The writes were never the problem; the verification
    # was racing the shutdown, and it only passed before because ending session A
    # happened to be followed by session B starting.
    #
    # A real user hitting this would have their own terminal still open, which is
    # exactly what this session stands in for.
    anchor = started_session("ds-anchor")

    # ── dataset A ─────────────────────────────────────────────────────────
    a = started_session("ds-a")
    a.prompt(f"Project {nonce} uses Paxos for leader election.", turn_id="t1")
    a.answer(f"{nonce}: Paxos, majority quorum.", turn_id="t1")
    a.end()
    assert a.wait_for_sync(deadline=600.0) is not None, "dataset A was never bridged"

    graph_a = GraphClient(
        base_url=live_base_url,
        dataset=live_env["COGNEE_PLUGIN_DATASET"],
        home=live_home,
    )
    graph_a.wait_until_recalled(f"What does {nonce} use?", "paxos", deadline=600.0)

    # ── dataset B, same server and HOME, different dataset ───────────────
    live_env["COGNEE_PLUGIN_DATASET"] = other_dataset
    b = started_session("ds-b")
    b.prompt(f"Project {nonce_b} uses Byzantine consensus with untrusted nodes.", turn_id="t1")
    b.answer(f"{nonce_b}: Byzantine consensus, 3f+1 quorum.", turn_id="t1")
    b.end()
    assert b.wait_for_sync(deadline=600.0) is not None, "dataset B was never bridged"

    graph_b = GraphClient(base_url=live_base_url, dataset=other_dataset, home=live_home)
    graph_b.wait_until_recalled(f"What does {nonce_b} use?", "byzantine", deadline=600.0)

    # ── neither dataset may answer with the other's content ──────────────
    from_a = graph_a.recall(f"What do you know about {nonce_b}?")
    assert nonce_b.lower() not in from_a.lower(), (
        f"dataset A leaked dataset B's project {nonce_b}:\n{from_a[:900]}"
    )

    from_b = graph_b.recall(f"What do you know about {nonce}?")
    # nonce is a prefix of nonce_b, so compare against B's own marker being absent
    # by checking for A's distinctive fact instead.
    assert "paxos" not in from_b.lower(), (
        f"dataset B leaked dataset A's Paxos decision:\n{from_b[:900]}"
    )

    # Released only now that every graph assertion is done.
    anchor.end()


def test_a_session_runs_exactly_one_final_sync(
    started_session, graph, live_suite, live_home, nonce
):
    """A repeated SessionEnd must not start a second final-sync worker.

    This is the guarantee the *plugin* owns — ``_claim_final_sync_once`` allows
    "exactly one detached final sync worker per session" via a claim marker. It
    matters because SessionEnd, the exit-watcher's PID poll and a manual sync can
    all fire around the same shutdown; without the claim they would each re-drive
    a full drain + improve for the same session.

    Deliberately *not* asserting "the graph holds one copy": in improve mode
    de-duplication is cognee's own content-hash concern, and counting substring
    occurrences in a semantic recall response measures neither (the same text can
    legitimately appear as both a session-cache and a graph node). What is
    assertable here is the plugin's claim behaviour, plus the fact surviving.
    """
    session = started_session("twice")
    session.prompt(f"For {nonce} the retry budget is 7 attempts.", turn_id="t1")
    session.answer(f"{nonce}: retry budget 7.", turn_id="t1")

    session.end()
    assert session.wait_for_sync(deadline=600.0) is not None, "first sync never completed"
    graph.wait_until_recalled(f"What is the retry budget for {nonce}?", "7", deadline=600.0)

    # Second SessionEnd for the very same session, nothing captured in between.
    again = session.end()
    assert again.ok, f"second SessionEnd failed (rc={again.returncode}): {again.stderr[:500]}"

    # Wait for the *claim decision*, not for a sync: the claim happens inside the
    # detached worker after COGNEE_SYNC_START_DELAY, and wait_for_sync would
    # return instantly on the first sync's own sync_bridge_done.
    refused = wait_for_event(
        live_suite, live_home, "final_sync_once_already_claimed", deadline=120.0
    )
    events = [event for event, _ in hook_events(live_suite, live_home)]
    assert refused is not None, (
        "the repeated SessionEnd's worker never hit the once-marker — the guard "
        f"never engaged, so a duplicate drain+improve can run. Events: {events[-12:]}"
    )

    claimed = events.count("final_sync_once_claimed")
    assert claimed == 1, (
        f"expected exactly one final-sync claim for the session, saw {claimed}. "
        f"A second claim means a second worker did the whole sync again. Events: {events[-12:]}"
    )

    # And the memory itself survived being synced twice.
    body = graph.recall(f"What is the retry budget for {nonce}?")
    assert "7" in body, f"the fact vanished after a second sync:\n{body[:800]}"
