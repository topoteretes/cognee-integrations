"""One brain, two agents: what Claude Code learns, Codex can recall.

This is the headline claim of shipping both integrations against one Cognee — and
it is only testable live, because it needs a real graph that two different
plugins write to and read from. Both suites default to the same dataset
(``agent_sessions``), and with cognee >= 1.4.1 the dataset is the scoping key, so
"shared brain" reduces to: same dataset, different plugin.

Driving the hooks directly is what makes this affordable. The CLI-based version
would need a Claude account *and* a Codex install, both authenticated, before it
could assert anything; here it is one extra ``LiveSession`` pointed at the other
suite's scripts inside the same temp HOME.

Note the two suites keep separate *local* state (``~/.cognee-plugin/claude-code``
vs ``~/.cognee-plugin/codex``) — only the server-side graph is shared. That is
exactly what should be true, and this test would catch a regression where one
suite's memory became invisible to the other.
"""

from __future__ import annotations

import uuid

import pytest
from utils.live import LiveSession, build_live_env, read_last_recall
from utils.suites import CODEX

pytestmark = pytest.mark.live


@pytest.fixture
def codex_session(live_home, live_project, live_base_url, live_dataset, live_prereqs):
    """A Codex session sharing the Claude session's HOME, server and dataset."""

    def _make(name: str = "codex") -> LiveSession:
        env = build_live_env(
            home=live_home,
            project=live_project,
            base_url=live_base_url,
            dataset=live_dataset,
            llm_api_key=live_prereqs,
            suite=CODEX,
        )
        return LiveSession(
            suite=CODEX,
            home=live_home,
            project=live_project,
            env=env,
            session_id=f"live-{name}-{uuid.uuid4().hex[:8]}",
        )

    return _make


def test_codex_recalls_what_claude_code_wrote(
    started_session, codex_session, graph, nonce, live_home
):
    # ── Claude Code learns something ──────────────────────────────────────
    claude = started_session("claude-writer")
    claude.prompt(f"For {nonce} we standardised on Paxos for leader election.", turn_id="t1")
    claude.answer(
        f"Recorded: {nonce} uses Paxos, with a majority quorum for leader election.",
        turn_id="t1",
    )
    claude.end()

    bridged = claude.wait_for_sync(deadline=600.0)
    assert bridged is not None, "Claude Code's session was never bridged to the graph"
    assert bridged.get("wrote") is True, f"nothing was written: {bridged}"

    # The graph holds it before we ask the other agent — so a miss below is a
    # sharing failure, not a write that never landed.
    graph.wait_until_recalled(f"What did we standardise on for {nonce}?", "paxos", deadline=600.0)

    # ── Codex, a different plugin, reads it ───────────────────────────────
    codex = codex_session("codex-reader")
    start = codex.start()
    assert start.ok, f"Codex SessionStart failed (rc={start.returncode}): {start.stderr[:600]}"

    lookup = codex.recall(f"What does {nonce} use for leader election?", turn_id="t1")
    assert lookup.ok, f"Codex recall failed (rc={lookup.returncode}): {lookup.stderr[:600]}"

    injected = lookup.additional_context().lower()
    assert injected, f"Codex was handed no memory at all — hook stdout was {lookup.stdout[:500]!r}"
    assert "paxos" in injected, (
        "Codex could not see what Claude Code wrote to the shared graph. "
        f"Injected context was:\n{injected[:1200]}"
    )

    # Local state stays per-suite even though the graph is shared.
    assert (live_home / ".cognee-plugin" / "claude-code").exists()
    assert (live_home / ".cognee-plugin" / "codex").exists()
    codex_hits = read_last_recall(CODEX, live_home).get("hits") or {}
    assert sum(int(v or 0) for v in codex_hits.values()) > 0, (
        f"Codex recorded no recall hits of its own: {codex_hits}"
    )

    codex.end()
