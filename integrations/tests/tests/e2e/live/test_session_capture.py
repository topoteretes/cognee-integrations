"""Within one session, what was said and done is captured — and the next
prompt's memory is the retrieved graph context, not the session's own words.

Per-turn prompts, answers and tool traces go straight to the *server's session
cache* via ``/remember/entry``; the session's end bridges them into the graph.
Prompt recall is one graph-scope request sent WITHOUT the session id, and on
cognee >= 1.6.0 the item it returns is the whole LLM input the completion
would have received. ``_recall_text`` keeps only the retrieved context of that
item (SDK-904): the question template goes, this session's own bridged
passages go, the rest is capped. The agent already holds its conversation, so
the in-session turn must NOT reappear in the injected memory — that is the
contract pinned here, next to the proof that the turn was captured at all.

Recall injects nothing until the dataset has a graph: before the first cognify
the graph scope answers 404. So each test first syncs one unrelated turn into
the graph (``synced_turn``), which doubles as the positive control — its
content is what the trimmed item must still carry.

Assertions lean on structure: the per-scope hit counts and save counters the
plugin records in ``last_recall.json``, the ``recall_context_trimmed`` event
with its parse flag, and a term from the captured turn that is absent from the
recall question (the raw item echoes the question back, so a term from it
would match trivially either way).
"""

from __future__ import annotations

import pytest
from utils.live import hook_events, read_last_recall

pytestmark = pytest.mark.live

#: The two forms the trim step logs: parsed (trimmed) or fail-open (unparsed).
TRIM_EVENTS = ("recall_context_trimmed", "recall_context_unparsed")


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


def _last_trim(suite, home) -> tuple[str, dict] | None:
    """The trim step's record for the most recent recall, or None if it never ran."""
    trims = [(e, d) for e, d in hook_events(suite, home) if e in TRIM_EVENTS]
    return trims[-1] if trims else None


def _assert_trimmed_to_context(suite, home, injected: str, own_term: str, seed_term: str) -> None:
    """The graph item was parsed and cut down to the retrieved context.

    Three things must hold at once. The trim step ran and *parsed* the item: an
    unparsed one is injected whole, fail-open, so a negative check on the
    session's own term would pass for the wrong reason. The seeded turn's term
    is still there: trimming must not eat the context it exists to deliver. And
    the session's own term is gone: the agent already has that conversation.
    """
    trim = _last_trim(suite, home)
    assert trim is not None, "the recall never reached the trim step (no recall_context_* event)"
    event, stats = trim
    assert event == "recall_context_trimmed" and stats.get("parsed"), (
        f"the graph item was not recognised as a 1.6 only_context prompt and was injected "
        f"whole (fail-open): {event} {stats}"
    )
    assert seed_term in injected, (
        "trimming dropped the retrieved context the seed put in the graph:\n" + injected[:1500]
    )
    assert own_term not in injected, (
        "the session's own turn was re-injected; recall must carry retrieved context only:\n"
        + injected[:1500]
    )


def test_prompt_and_answer_are_captured_and_recall_stays_graph_context(
    synced_turn, started_session, live_suite, live_home, nonce
):
    _seed_graph(synced_turn, nonce)
    session = started_session("same")

    session.prompt(f"The deploy target for {nonce} is cluster edge-7.", turn_id="t1")
    session.answer(f"Understood — {nonce} deploys to cluster edge-7.", turn_id="t1")

    lookup = session.recall(f"Where does {nonce} deploy?", turn_id="t2")
    assert lookup.ok, f"recall hook failed (rc={lookup.returncode}): {lookup.stderr[:600]}"

    recall = read_last_recall(live_suite, live_home)
    # The counters are drained by this recall, so they are the record of what
    # the turn before it persisted.
    saves = recall.get("saves_last_turn") or {}
    assert int(saves.get("prompt") or 0) > 0, f"the prompt was not captured: {saves}"
    assert int(saves.get("answer") or 0) > 0, f"the answer was not captured: {saves}"

    hits = recall.get("hits") or {}
    assert int(hits.get("graph_context") or 0) > 0, (
        f"nothing was recalled in-session against a built graph; per-scope hits were {hits}"
    )
    _assert_graph_only(hits)
    injected = lookup.additional_context().lower()
    _assert_trimmed_to_context(live_suite, live_home, injected, "edge-7", "raft")

    session.end()


def test_tool_trace_is_captured_and_recall_stays_graph_context(
    synced_turn, started_session, live_suite, live_home, nonce
):
    """PostToolUse traces are captured alongside the turn they belong to, and
    the next prompt's memory is still the retrieved graph context: the trace
    lives in the session cache, which recall writes to but never injects from.
    """
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
        f"nothing was recalled in-session against a built graph; per-scope hits were {hits}"
    )
    _assert_graph_only(hits)
    injected = lookup.additional_context().lower()
    _assert_trimmed_to_context(live_suite, live_home, injected, "9931", "raft")

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
