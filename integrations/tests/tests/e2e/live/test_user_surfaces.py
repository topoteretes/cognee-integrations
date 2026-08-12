"""The three places a user actually meets their memory.

Recall working inside the plugin is necessary but not sufficient — the user only
ever sees it through a surface:

* the **status line**, which is how they know memory is alive at all;
* the **pre-compact anchor**, which is what survives a context compaction and
  keeps a long session coherent;
* **cognee-search.sh**, the escape hatch for asking the graph directly.

All three read the *session cache*, so none of them needs a cognify — which makes
this the cheapest file in the tier and the one worth running most often.
"""

from __future__ import annotations

import json

import pytest
from utils.live import read_last_recall
from utils.statusline import write_json

pytestmark = pytest.mark.live


def _enable_plugin(live_home) -> None:
    """claude-code's renderer self-evicts unless the plugin is enabled."""
    write_json(
        live_home / ".claude" / "settings.json",
        {"enabledPlugins": {"cognee-memory@cognee": True}},
    )


@pytest.fixture
def captured_session(started_session, nonce):
    """A session with one real turn already captured into the session cache."""
    session = started_session("surfaces")
    session.prompt(f"Remember that {nonce} runs on cluster edge-7.", turn_id="t1")
    session.answer(f"Noted: {nonce} runs on cluster edge-7.", turn_id="t1")
    return session


def test_status_line_shows_the_counts_from_a_real_recall(
    captured_session, live_suite, live_home, nonce
):
    """A recall that found something must be visible in the bar.

    The counts are the user's only signal that memory is working; a silent
    regression here looks exactly like a quiet session.
    """
    _enable_plugin(live_home)

    lookup = captured_session.recall(f"Where does {nonce} run?", turn_id="t2")
    assert lookup.ok, f"recall failed (rc={lookup.returncode}): {lookup.stderr[:500]}"

    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    recalled = sum(int(v or 0) for v in hits.values())
    assert recalled > 0, f"nothing recalled, so the bar has nothing to show: {hits}"

    bar = captured_session.run(
        "cognee_statusline_render.py", {"session_id": captured_session.session_id}
    )
    assert bar.ok, f"status line render failed (rc={bar.returncode}): {bar.stderr[:500]}"
    assert "cognee:" in bar.stdout, f"unrecognisable status line: {bar.stdout!r}"
    assert "recall " in bar.stdout, (
        f"the bar omitted the recall counts after a successful recall: {bar.stdout!r}"
    )
    # The rendered counts must not all be zero — that would misreport a real hit.
    assert "recall 0s/0t/0g/0a" not in bar.stdout, (
        f"the bar reported all-zero counts despite {recalled} hits: {bar.stdout!r}"
    )


def test_precompact_produces_an_anchor_carrying_the_session(captured_session, nonce, request):
    """Compaction drops the transcript; the anchor is what carries memory across it.

    KNOWN GAP, not a design choice: ``pre-compact.py`` has **no HTTP path**. It
    recalls via ``cognee.recall`` and falls back to ``get_session_manager()`` —
    both local-SDK only — while in server mode the session cache lives on the
    server. So session and trace entries come back empty, the derived query stays
    empty, the graph scopes are never queried, and the hook logs
    ``precompact_empty`` and prints nothing. Every other hook branches on HTTP vs
    SDK (``recall_via_http``, ``remember_entry_via_http``); this one never got one.

    The effect is that anyone running against a server — the setup this whole
    tier exercises — loses their anchor at exactly the moment compaction throws
    the transcript away, and nothing errors to say so.

    Strict xfail: it turns red as soon as pre-compact learns to recall over HTTP.
    Driving this hook is trivial here and effectively untestable through the real
    CLI, where triggering a compaction on demand is the hard part.
    """
    request.node.add_marker(
        pytest.mark.xfail(
            reason=(
                "pre-compact.py is local-SDK only, so it produces no anchor in "
                "HTTP/server mode (logs precompact_empty)"
            ),
            strict=True,
        )
    )

    run = captured_session.run(
        "pre-compact.py",
        {
            "hook_event_name": "PreCompact",
            "session_id": captured_session.session_id,
            "trigger": "auto",
        },
    )
    assert run.ok, f"pre-compact failed (rc={run.returncode}): {run.stderr[:600]}"

    anchor = run.stdout.strip()
    assert anchor, "pre-compact produced no anchor at all — nothing would survive compaction"
    assert nonce.lower() in anchor.lower(), (
        f"the anchor does not mention the session's subject ({nonce}):\n{anchor[:1200]}"
    )


def test_search_cli_finds_the_session_from_the_command_line(captured_session, nonce):
    """cognee-search.sh is the documented way to ask memory directly."""
    run = captured_session.run_shell(
        "cognee-search.sh", f"What runs on cluster edge-7 for {nonce}?", "5", "--session"
    )
    assert run.ok, f"cognee-search.sh failed (rc={run.returncode}): {run.stderr[:600]}"

    output = run.stdout
    assert output.strip(), f"search returned nothing at all; stderr was {run.stderr[:400]}"
    assert nonce.lower() in output.lower(), (
        f"the search CLI could not find the session's own content:\n{output[:1200]}"
    )


def test_search_cli_emits_parseable_output(captured_session, nonce):
    """Whatever it prints must be usable by a caller, not just human-readable."""
    run = captured_session.run_shell("cognee-search.sh", f"{nonce}", "3", "--session")
    assert run.ok, f"cognee-search.sh failed (rc={run.returncode}): {run.stderr[:600]}"

    text = run.stdout.strip()
    assert text, "no output to parse"
    # The wrapper prints either a JSON payload or plain text lines; both are fine,
    # but a stray traceback or shell error is not.
    assert "Traceback" not in text and "command not found" not in text, text[:600]
    if text.startswith(("{", "[")):
        json.loads(text)  # must be valid if it claims to be JSON
