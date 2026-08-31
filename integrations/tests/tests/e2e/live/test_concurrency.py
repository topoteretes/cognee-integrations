"""Two agent sessions on one machine must not clobber each other.

Running two terminals against the same cognee is the normal case, not an edge
case, and several plugin mechanisms exist only because of it: the per-session
improve lock, the buffer mutex, per-session state files, and the once-per-session
final-sync claim. A regression in any of them looks like "one terminal's memory
went missing" — which nobody would attribute to concurrency.

Both sessions share the server and the dataset (that *is* the shared-brain
model); what must stay separate is their bookkeeping.
"""

from __future__ import annotations

import concurrent.futures

import pytest
from utils.live import hook_events, wait_for_event_count

pytestmark = pytest.mark.live


def test_two_concurrent_sessions_both_land(started_session, graph, nonce):
    """Interleaved turns from two sessions must both reach the graph."""
    nonce_a = f"{nonce}-A"
    nonce_b = f"{nonce}-B"

    # Start serially so only one boot happens, then interleave the turns.
    a = started_session("conc-a")
    b = started_session("conc-b")
    assert a.session_id != b.session_id

    # Held open past both ends, so the graph assertions have a server to talk to:
    # uvicorn runs in agent mode and tears down once the last agent disconnects,
    # and ``end()`` unregisters. Without this the polls below race that shutdown.
    anchor = started_session("conc-anchor")

    def drive(session, tag: str) -> None:
        session.prompt(f"Project {tag} uses a {tag} quorum.", turn_id="t1")
        session.tool("Bash", {"command": f"echo {tag}"}, tag, turn_id="t1")
        session.answer(f"{tag}: quorum recorded.", turn_id="t1")

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(drive, a, nonce_a), pool.submit(drive, b, nonce_b)]
        for future in futures:
            future.result()  # re-raise anything either thread hit

    for session, tag in ((a, nonce_a), (b, nonce_b)):
        failures = [r for r in session.runs if not r.ok]
        assert not failures, (
            f"{tag} had failing hooks: {[(r.script, r.returncode) for r in failures]}"
        )

    # End both, again concurrently — the moment the per-session improve lock and
    # the once-claim marker are actually contended.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        ends = [pool.submit(a.end), pool.submit(b.end)]
        for future in ends:
            assert future.result().ok

    assert a.wait_for_sync(deadline=900.0) is not None, "no session reported a bridge"

    # Both sessions' content must be in the shared dataset.
    graph.wait_until_recalled(f"What quorum does {nonce_a} use?", nonce_a, deadline=900.0)
    graph.wait_until_recalled(f"What quorum does {nonce_b} use?", nonce_b, deadline=900.0)

    anchor.end()


def test_each_session_claims_its_own_final_sync(started_session, live_suite, live_home, nonce):
    """Distinct sessions must each get a claim — the marker is per session.

    The once-claim exists to stop *one* session syncing twice; if it were keyed
    too broadly, the second session would be refused and its memory would never
    be bridged. That failure is silent, which is why it is worth pinning.
    """
    a = started_session("claim-a")
    a.prompt(f"{nonce}-A note.", turn_id="t1")
    a.answer("noted A", turn_id="t1")

    b = started_session("claim-b")
    b.prompt(f"{nonce}-B note.", turn_id="t1")
    b.answer("noted B", turn_id="t1")

    a.end()
    b.end()

    # Wait on the *count*, not on presence: each claim happens inside a detached
    # worker after COGNEE_SYNC_START_DELAY, and session A's claim is already in
    # the log by the time B's worker starts.
    claims = wait_for_event_count(
        live_suite, live_home, "final_sync_once_claimed", 2, deadline=300.0
    )
    events = [event for event, _ in hook_events(live_suite, live_home)]
    assert claims >= 2, (
        f"two distinct sessions produced only {claims} final-sync claim(s) — one "
        f"session's sync was refused by the other's marker. The token is the "
        f"session key, so each session must get its own. Events: {events[-14:]}"
    )
