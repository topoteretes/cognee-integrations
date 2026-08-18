"""One brain, two agents: what either integration learns, the other can recall.

This is the headline claim of shipping both plugins against one Cognee, and it is
only testable live, because it needs a real graph that two different plugins write
to and read from. Both suites default to the same dataset (``agent_sessions``), and
with cognee >= 1.4.1 the dataset is the scoping key, so "shared brain" reduces to:
same dataset, different plugin.

Driving the hooks directly is what makes this affordable. The CLI-based version
would need a Claude account *and* a Codex install, both authenticated, before it
could assert anything; here each side is one more ``LiveSession`` pointed at the
other suite's scripts inside the same temp HOME.

**Both directions are exercised.** They were sharply asymmetric when codex's bridge
was synchronous with no cognify poll; the port that landed in main gave codex the
background bridge, so the two write paths have largely converged. They are still
worth running in both directions: the improve path did *not* travel with that port
(``has_improve_pipeline_polling`` is still claude-code only), and this is the only
place either integration's writes are verified through the other's reader.

Note the suites keep separate *local* state (``~/.cognee-plugin/claude-code`` vs
``~/.cognee-plugin/codex``) — only the server-side graph is shared. That is exactly
what should be true, and this would catch a regression where one suite's memory
became invisible to the other.

These name their suites explicitly rather than riding the ``live_suite``
parametrization, which would put the same integration on both sides.
"""

from __future__ import annotations

import pytest
from utils.live import read_last_recall
from utils.suites import CLAUDE, CODEX

pytestmark = pytest.mark.live


@pytest.mark.parametrize(
    ("writer_suite", "reader_suite"),
    [
        pytest.param(CLAUDE, CODEX, id="claude-writes-codex-reads"),
        pytest.param(CODEX, CLAUDE, id="codex-writes-claude-reads"),
    ],
)
def test_either_agent_recalls_what_the_other_wrote(
    session_for, graph, nonce, live_home, writer_suite, reader_suite
):
    # ── one agent learns something ────────────────────────────────────────
    writer = session_for(writer_suite, f"{writer_suite.name}-writer")
    writer.prompt(f"For {nonce} we standardised on Paxos for leader election.", turn_id="t1")
    writer.answer(
        f"Recorded: {nonce} uses Paxos, with a majority quorum for leader election.",
        turn_id="t1",
    )
    writer.end()

    bridged = writer.wait_for_sync(deadline=600.0)
    assert bridged is not None, f"{writer_suite.name}'s session was never bridged to the graph"
    assert bridged.get("wrote") is True, f"nothing was written: {bridged}"

    # ── the other plugin connects ─────────────────────────────────────────
    # Booted BEFORE the graph is polled, and not merely for tidiness: the plugin
    # runs uvicorn in agent mode, so the server tears down once the last agent
    # disconnects — and ``end()`` unregisters. With the writer finished and no
    # reader yet, the poll below would race that shutdown and fail with
    # ECONNREFUSED on a write that had already landed.
    reader = session_for(reader_suite, f"{reader_suite.name}-reader")

    # The graph holds it before the other agent is asked — so a miss below is a
    # sharing failure, not a write that never landed.
    graph.wait_until_recalled(f"What did we standardise on for {nonce}?", "paxos", deadline=600.0)

    # ── the other plugin reads it ─────────────────────────────────────────
    lookup = reader.recall(f"What does {nonce} use for leader election?", turn_id="t1")
    assert lookup.ok, (
        f"{reader_suite.name} recall failed (rc={lookup.returncode}): {lookup.stderr[:600]}"
    )

    injected = lookup.additional_context().lower()
    assert injected, (
        f"{reader_suite.name} was handed no memory at all — hook stdout was {lookup.stdout[:500]!r}"
    )
    assert "paxos" in injected, (
        f"{reader_suite.name} could not see what {writer_suite.name} wrote to the "
        f"shared graph. Injected context was:\n{injected[:1200]}"
    )

    # Local state stays per-suite even though the graph is shared.
    for suite in (writer_suite, reader_suite):
        assert (live_home / ".cognee-plugin" / suite.state_subdir).exists(), (
            f"{suite.name} kept no local state of its own"
        )
    reader_hits = read_last_recall(reader_suite, live_home).get("hits") or {}
    assert sum(int(v or 0) for v in reader_hits.values()) > 0, (
        f"{reader_suite.name} recorded no recall hits of its own: {reader_hits}"
    )

    reader.end()
