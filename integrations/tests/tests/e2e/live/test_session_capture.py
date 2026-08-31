"""Within one session, what was said and done is recallable immediately.

This is the other half of the memory promise, and it works differently from
cross-session recall: per-turn prompts, answers and tool traces go straight to
the *server's session cache* via ``/remember/entry``, so they are queryable
without waiting for improve or cognify. No graph build, no LLM extraction — which
also makes these the cheapest live tests to run.

Assertions lean on the per-scope hit counts the plugin records in
``last_recall.json`` rather than on the prose a semantic search returns: "the
trace scope found something" is a structural fact, whereas which sentence comes
back is not something this plugin controls.
"""

from __future__ import annotations

import pytest
from utils.live import read_last_recall

pytestmark = pytest.mark.live


def test_prompt_and_answer_are_recallable_in_the_same_session(
    started_session, live_suite, live_home, nonce
):
    session = started_session("same")

    session.prompt(f"The deploy target for {nonce} is cluster edge-7.", turn_id="t1")
    session.answer(f"Understood — {nonce} deploys to cluster edge-7.", turn_id="t1")

    lookup = session.recall(f"Where does {nonce} deploy?", turn_id="t2")
    assert lookup.ok, f"recall hook failed (rc={lookup.returncode}): {lookup.stderr[:600]}"

    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    assert hits, "the recall recorded no per-scope counts at all"
    assert sum(int(v or 0) for v in hits.values()) > 0, (
        f"nothing was recalled in-session; per-scope hits were {hits}"
    )

    session.end()


def test_tool_trace_is_captured_and_recallable(started_session, live_suite, live_home, nonce):
    """PostToolUse traces are memory too — they are what "what did you just do"
    questions are answered from."""
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

    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    assert sum(int(v or 0) for v in hits.values()) > 0, (
        f"the captured turn was not recallable; per-scope hits were {hits}"
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
