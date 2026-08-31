"""The core promise: what you discussed in one session is there in the next.

Two turns in session A about a nonce project, then the session ends and the real
improve → cognify pipeline writes it into a real graph. A *fresh* session B — a
new session id on the same dataset, knowing nothing about A — must then be able
to recall it.

Everything is real: the plugin boots its own cognee server, the LLM extracts
entities, the graph is on disk, recall is an HTTP round-trip. The only thing
simulated is the host: the hooks are invoked directly with the payloads Claude
Code would send. That trade is deliberate — it removes the CLI, its OAuth and its
billing from the loop while leaving every line of plugin and server behaviour
under test. The "would the host actually call these hooks" half is covered
hermetically by unit/test_hooks_contract.py.

Two assertion layers, both deterministic:

  L1  the graph itself holds the memory (direct POST /api/v1/recall)
  L2  the plugin *injects* it into the fresh session's prompt — which is the
      actual product contract, and the thing a user would notice breaking

There is deliberately no "the model mentioned it" layer: no model runs here, and
how well a model phrases an answer was never this plugin's responsibility.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.live


def test_paxos_then_byzantine_recalled_in_a_fresh_session(
    started_session, graph, nonce, live_dataset
):
    # ── session A: two turns about the nonce project ──────────────────────
    a = started_session("a")

    a.prompt(f"For project {nonce} we are evaluating Paxos for leader election.", turn_id="t1")
    a.answer(
        f"For {nonce}, Paxos is a sound choice for leader election: it tolerates "
        "crash faults and needs a majority quorum.",
        turn_id="t1",
    )

    a.prompt(f"How would Byzantine consensus compare for {nonce}?", turn_id="t2")
    a.answer(
        f"Byzantine consensus for {nonce} tolerates malicious nodes, not just "
        "crashes, at the cost of more rounds and a 3f+1 quorum.",
        turn_id="t2",
    )

    end = a.end()
    assert end.ok, f"SessionEnd failed (rc={end.returncode}): {end.stderr[:800]}"

    # The hook returns in milliseconds — the write happens in a detached worker,
    # so wait for the worker to report it bridged the session.
    bridged = a.wait_for_sync(deadline=600.0)
    assert bridged is not None, "detached sync worker never reported sync_bridge_done"
    assert bridged.get("wrote") is True, f"session was not written to the graph: {bridged}"

    # ── session B: fresh session, same dataset, no knowledge of A ─────────
    b = started_session("b")
    assert b.session_id != a.session_id

    # L1: the graph holds it. Polling recall is the only honest readiness gate —
    # improve_fired can report ok with empty cognify/memify while the graph was
    # in fact written.
    body = graph.wait_until_recalled(
        f"What consensus protocols did we discuss for {nonce}?",
        "paxos",
        "byzantine",
        deadline=600.0,
    )
    assert nonce.lower() in body.lower(), (
        f"the graph returned consensus content but not the {nonce} scoping: {body[:800]}"
    )

    # L2: the plugin injects that memory into the fresh session's prompt. This is
    # the contract a user actually experiences.
    lookup = b.recall(f"Remind me what we chose for {nonce} and why.", turn_id="t1")
    assert lookup.ok, f"context lookup failed (rc={lookup.returncode}): {lookup.stderr[:800]}"

    injected = lookup.additional_context().lower()
    assert injected, (
        f"the fresh session was handed no memory at all — hook stdout was {lookup.stdout[:600]!r}"
    )
    missing = [term for term in ("paxos", "byzantine") if term not in injected]
    assert not missing, f"injected context is missing {missing}. Context was:\n{injected[:1500]}"

    b.end()


# Deliberately not here yet — two scenarios this run surfaced, both worth their
# own test rather than a rushed one:
#
# * Dataset isolation. A first attempt queried a dataset that was never created,
#   which probes "what does the server do with an unknown dataset" instead of the
#   real question. Doing it honestly means populating *two* datasets and showing
#   neither surfaces the other's nonce — a second cognify, so it belongs in its
#   own test rather than bolted onto this file.
# * Cold-start recall. With production timeouts (COGNEE_RECALL_TIMEOUT 2.5s,
#   COGNEE_RECALL_BUDGET 4s) the first graph query against a freshly booted
#   server times out and is correctly reported as "slow", so the prompt gets no
#   memory. This tier raises those limits to ask its own question, which means
#   the cold-start behaviour is currently untested. It deserves pinning: first
#   prompt degrades gracefully, a later prompt recalls.
