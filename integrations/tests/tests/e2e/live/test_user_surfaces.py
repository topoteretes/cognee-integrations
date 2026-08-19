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


def _enable_plugin(suite, live_home) -> None:
    """claude-code's renderer self-evicts unless the plugin is enabled.

    An evicted renderer prints nothing, which would make the assertions below
    vacuous rather than red. codex has no enablement gate.
    """
    if suite.name != "claude-code":
        return
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


@pytest.fixture
def recalled_session(captured_session, live_suite, live_home, nonce):
    """A session that has just performed a recall which genuinely found something.

    Both status-line tests need this precondition, and it is worth asserting
    separately: if the recall found nothing, a bar with no counts would be correct
    and the test would be measuring the wrong thing.
    """
    _enable_plugin(live_suite, live_home)

    lookup = captured_session.recall(f"Where does {nonce} run?", turn_id="t2")
    assert lookup.ok, f"recall failed (rc={lookup.returncode}): {lookup.stderr[:500]}"

    hits = read_last_recall(live_suite, live_home).get("hits") or {}
    recalled = sum(int(v or 0) for v in hits.values())
    assert recalled > 0, f"nothing recalled, so the bar has nothing to show: {hits}"
    return captured_session, recalled


def test_the_status_line_renders_against_a_real_server(recalled_session, live_suite):
    """Whatever shape the bar takes, it must render and name the dataset.

    Both integrations have a bar; only its form differs (claude-code styles a
    terminal line, codex emits plain text for the model's context). This is the
    part that must hold for both — a renderer that crashes or self-evicts against a
    live server costs the user their only signal that memory is alive.
    """
    session, _recalled = recalled_session

    bar = session.run("cognee_statusline_render.py", {"session_id": session.session_id})
    assert bar.ok, f"status line render failed (rc={bar.returncode}): {bar.stderr[:500]}"
    assert "Traceback" not in bar.stderr, f"renderer raised:\n{bar.stderr[:800]}"
    assert "cognee:" in bar.stdout, f"unrecognisable status line: {bar.stdout!r}"


def test_the_status_line_shows_the_counts_from_a_real_recall(recalled_session, live_suite):
    """A recall that found something must be visible in the counts segment.

    The counts are the user's only quantitative signal that memory is working; a
    silent regression here looks exactly like a quiet session.

    claude-code only: codex's bar is a short plain-text string for the model's
    context and carries no diagnostics strip.
    """
    if not live_suite.has_rich_statusline:
        pytest.skip(f"{live_suite.name}: the bar is plain text and has no counts segment")

    session, recalled = recalled_session
    bar = session.run("cognee_statusline_render.py", {"session_id": session.session_id})
    assert bar.ok, f"status line render failed (rc={bar.returncode}): {bar.stderr[:500]}"

    assert "recall " in bar.stdout, (
        f"the bar omitted the recall counts after a successful recall: {bar.stdout!r}"
    )
    # The rendered counts must not all be zero — that would misreport a real hit.
    assert "recall 0s/0t/0g/0a" not in bar.stdout, (
        f"the bar reported all-zero counts despite {recalled} hits: {bar.stdout!r}"
    )


def test_precompact_produces_an_anchor_carrying_the_session(
    captured_session, live_suite, nonce, request
):
    """Compaction drops the transcript; the anchor is what carries memory across it.

    Every suite now recalls over HTTP, so each must produce an anchor against a real
    server. That was not always true: claude-code's pre-compact was local-SDK only
    — ``cognee.recall`` plus a ``get_session_manager()`` fallback — while in server
    mode the session cache lives on the server. Session and trace entries came back
    empty, the derived query stayed empty, the graph scopes were never queried, and
    the hook logged ``precompact_empty`` and printed nothing. Anyone running against
    a server lost their anchor at exactly the moment compaction discarded the
    transcript, with nothing erroring to say so.

    This test is what caught it, and the fix was a port of the branch codex had all
    along. It stays a live test because it is the only place the difference shows:
    against a mock the local path looks fine.

    ``has_precompact_http`` is still a flag rather than an assumption — a future
    integration could arrive without the branch, and this would then say so.
    """
    if not live_suite.has_precompact_http:
        request.node.add_marker(
            pytest.mark.xfail(
                reason=(
                    f"{live_suite.name}: pre-compact.py is local-SDK only, so it "
                    "produces no anchor in HTTP/server mode (logs precompact_empty)"
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
