"""Within one session, what was said and done reaches the next prompt's memory.

Per-turn prompts, answers and tool traces go straight to the *server's session
cache* via ``/remember/entry``. Prompt recall no longer searches that cache: it
is one graph-scope request, and on cognee >= 1.6.0 the graph item's text is the
whole LLM input — this session's conversation history, the question with the
retrieved graph context, and the session guidance block. Keeping recall to that
one item is deliberate (it bounds what gets injected), and it means in-session
history arrives only once the dataset has a graph: before the first cognify the
graph scope answers 404 and nothing is injected. So each test here first syncs
one unrelated turn into the graph (``synced_turn``), then asserts that a new
session's own capture rides along with its recall.

Assertions lean on the per-scope hit counts the plugin records in
``last_recall.json`` plus a term from the captured turn that is absent from the
recall question — the question itself is echoed back in the item, so a term
from it would match trivially.
"""

from __future__ import annotations

import pytest
from utils.live import read_last_recall

pytestmark = pytest.mark.live


def _seed_graph(synced_turn, nonce) -> None:
    synced_turn(
        "seed",
        f"Project {nonce}-seed replicates with raft.",
        f"Noted: {nonce}-seed replicates with raft.",
        f"How does {nonce}-seed replicate?",
        "raft",
    )


def _assert_graph_only(hits: dict) -> None:
    """Recall is graph-only: the retired raw-session buckets stay at zero."""
    raw = {k: hits.get(k) for k in ("session", "trace", "session_context")}
    assert not any(int(v or 0) for v in raw.values()), (
        f"recall injected raw session-cache entries; it should read the graph only: {hits}"
    )


def test_prompt_and_answer_are_recallable_in_the_same_session(
    synced_turn, started_session, live_suite, live_home, nonce
):
    _seed_graph(synced_turn, nonce)
    session = started_session("same")

    session.prompt(f"The deploy target for {nonce} is cluster edge-7.", turn_id="t1")
    session.answer(f"Understood — {nonce} deploys to cluster edge-7.", turn_id="t1")

    lookup = session.recall(f"Where does {nonce} deploy?", turn_id="t2")
    assert lookup.ok, f"recall hook failed (rc={lookup.returncode}): {lookup.stderr[:600]}"

    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    assert int(hits.get("graph_context") or 0) > 0, (
        f"nothing was recalled in-session against a built graph; per-scope hits were {hits}"
    )
    _assert_graph_only(hits)
    injected = lookup.additional_context().lower()
    assert "edge-7" in injected, (
        "the graph item did not carry this session's history:\n" + injected[:1500]
    )

    session.end()


def test_tool_trace_is_captured_and_recallable(
    synced_turn, started_session, live_suite, live_home, nonce
):
    """PostToolUse traces are captured, and the turn they belong to reaches the
    next prompt's memory. The trace itself reaches the model through the
    server-distilled guidance block, which is not deterministic enough to pin;
    the capture is asserted through the save counters instead."""
    _seed_graph(synced_turn, nonce)
    session = started_session("trace")

    session.prompt(f"Check the {nonce} service config.", turn_id="t1")
    session.tool(
        "Read",
        {"file_path": f"/srv/{nonce}/service.yaml"},
        "listen_port: 9931\nmode: strict",
        turn_id="t1",
    )
    session.answer(f"{nonce} listens on port 9931 in strict mode.", turn_id="t1")

    lookup = session.recall(f"What port did we find for {nonce}?", turn_id="t2")
    assert lookup.ok, f"recall hook failed (rc={lookup.returncode}): {lookup.stderr[:600]}"

    recall = read_last_recall(live_suite, live_home)
    saves = recall.get("saves_last_turn") or {}
    assert int(saves.get("trace") or 0) > 0, f"the tool trace was not captured: {saves}"
    hits = recall.get("hits") or {}
    assert int(hits.get("graph_context") or 0) > 0, (
        f"the captured turn was not recallable; per-scope hits were {hits}"
    )
    _assert_graph_only(hits)
    injected = lookup.additional_context().lower()
    assert "9931" in injected, (
        "the graph item did not carry this session's history:\n" + injected[:1500]
    )

    session.end()


def test_save_counters_track_what_was_captured(started_session, live_suite, live_home, nonce):
    """The counters behind the status line must reflect real captures.

    A silent capture regression would otherwise look identical to a quiet
    session: no error anywhere, just no memory later.
    """
    session = started_session("counts")

    session.prompt(f"Note that {nonce} uses raft.", turn_id="t1")
    session.tool("Bash", {"command": "echo hi"}, "hi", turn_id="t1")
    session.answer(f"Noted: {nonce} uses raft.", turn_id="t1")

    # The counters are drained by the next prompt's recall, which is what the bar
    # renders — so read them through that path.
    session.recall("anything at all", turn_id="t2")
    saves = read_last_recall(live_suite, live_home).get("saves_last_turn") or {}
    assert saves, "no save counters were recorded for the turn"
    assert sum(int(v or 0) for v in saves.values()) > 0, (
        f"a prompt, a tool trace and an answer were captured but counters say {saves}"
    )

    session.end()
