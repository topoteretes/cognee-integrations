"""COGNEE_RECALL_ANSWER: the prompt hook asks the server for its answer.

By default the per-prompt graph recall is an ``only_context`` request: the server
returns what it retrieved and the agent writes the answer. A deployment whose
retrieved context is far larger than the injection limit loses most of it to the
budget, so the agent answers from a fragment.

With ``COGNEE_RECALL_ANSWER=true`` the graph request asks for the server's
completion with its sources (``only_context=false``, ``include_references=true``)
and the answer skips the context trimmer. It goes through the same budget as any
other recalled text, which drops whole lines from the end, so an answer over the
injection limit loses its trailing source list first. Nothing else changes: the
guidance request still asks for context, and with the variable unset every
request is the one sent before.
"""

from __future__ import annotations

import pytest
from utils.recall import drive_recall, load_lookup

PROMPT = "what did the parties agree about the exit amount?"

#: Longer than the per-passage cap, with a markdown rule and a source list: the
#: shapes the context trimmer would cut or split if it ran on an answer.
ANSWER = (
    "The parties agreed an exit amount of 775,500.\n\n"
    + "It is the accrued delay damages for the whole delay period. " * 40
    + "\n\n---\n\n**Computed check:** 5,500 x 141 = 775,500.\n\n"
    "Evidence:\n\n"
    '- chunk 2 of document 7.1 (chunk_id: bac74858): "SOURCE: 7.1 TITLE: Suspension Agreement"\n'
    '- chunk 4 of document 7.1 (chunk_id: 343acf5a): "SOURCE: 7.1 TITLE: Suspension Agreement"'
)
ITEM_ANSWER = {"source": "graph", "text": ANSWER, "dataset_name": "contracts"}
ITEM_CONTEXT = {
    "source": "graph",
    "text": f"The question is: `{PROMPT}`\n\nContext:\n`## Relevant passages\na passage`",
}


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code answer mode")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "off")  # after the harness scrubs COGNEE_*
    return module


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_unset_keeps_the_context_request(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_CONTEXT]})

    graph = run.kwargs["graph"]
    assert graph["only_context"] is True
    assert graph["include_references"] is False
    assert graph["evidence_mode"] == "context"
    assert any(event == "recall_context_trimmed" for event, _ in run.events)


@pytest.mark.parametrize("value", ["true", "1", "on", " YES "])
def test_answer_mode_asks_the_graph_scope_for_an_answer_with_sources(lookup, monkeypatch, value):
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", value)
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_ANSWER]})

    graph = run.kwargs["graph"]
    assert graph["only_context"] is False
    assert graph["include_references"] is True
    # Evidence items replace a result's text, and an answer carries its own sources.
    assert graph["evidence_mode"] is None
    # Unchanged: pinned type, no session id, so no session write and no history.
    assert graph["search_type"] == "HYBRID_COMPLETION"
    assert graph["session_id"] == ""


def test_answer_mode_leaves_the_guidance_request_alone(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", "true")
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_ANSWER]})

    guidance = run.kwargs["session_context"]
    assert guidance["only_context"] is True
    assert guidance["include_references"] is False


def _positions(context: str, lines: list[str]) -> list[int]:
    return [context.find(line) for line in lines]


def test_the_answer_is_not_trimmed_as_a_context(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", "true")
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_ANSWER]})

    # Every line arrives, in order: the paragraph over the per-passage cap, the
    # markdown rule the passage splitter would cut at, and the source list.
    lines = [line for line in ANSWER.splitlines() if line.strip()]
    positions = _positions(_context(run), lines)
    assert -1 not in positions and positions == sorted(positions)
    events = [event for event, _ in run.events]
    assert "recall_context_trimmed" not in events
    assert "recall_context_unparsed" not in events


def test_an_answer_over_the_injection_limit_loses_its_last_lines_first(lookup, monkeypatch):
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", "true")
    body = [f"Finding {n}: " + "the clause applies to this period. " * 12 for n in range(1, 16)]
    sources = [
        f'- chunk {n} of document 7.{n}: "SOURCE: 7.{n} ' + "x" * 300 + '"' for n in range(1, 16)
    ]
    long_answer = "\n\n".join(body) + "\n\nEvidence:\n\n" + "\n".join(sources)
    assert len(long_answer) > 9000
    item = {"source": "graph", "text": long_answer}
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [item]})

    context = _context(run)
    assert len(context) <= 9000
    assert all(paragraph in context for paragraph in body)
    assert sources[0] in context and sources[-1] not in context


@pytest.mark.parametrize("value", ["", "false", "0", "off", "maybe"])
def test_anything_but_a_true_value_is_off(lookup, monkeypatch, value):
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", value)
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_CONTEXT]})

    assert run.kwargs["graph"]["only_context"] is True
    assert run.kwargs["graph"]["include_references"] is False


def test_include_references_reaches_the_wire_only_when_asked(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code answer mode")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **kw: None)
    sent = []

    def fake_request(path, payload=None, **kwargs):
        sent.append(dict(payload or {}))
        return []

    monkeypatch.setattr(common, "_json_http_request", fake_request)

    def recall(**kwargs):
        common.recall_via_http(
            "q", session_id="", top_k=5, scope=["graph"], dataset="ds", timeout=5.0, **kwargs
        )
        return sent[-1]

    assert "include_references" not in recall()
    body = recall(only_context=False, include_references=True)
    assert body["include_references"] is True and body["only_context"] is False


#: Two answers to two prompts of one session that share their conclusion, their
#: table header, the "Evidence:" heading and a source line.
TABLE_ANSWER = (
    "The fee is not payable.\n\n"
    + "It is the accrued delay damages for the whole delay period. " * 30
    + "\n\n| Item | Amount |\n|---|---|\n| Daily rate | 5,500 |\n| Days | 141 |\n\n"
    "Evidence:\n\n"
    '- chunk 2 of document 7.1: "SOURCE: 7.1 TITLE: Suspension Agreement"\n'
    '- chunk 4 of document 7.1: "SOURCE: 7.1 TITLE: Suspension Agreement"'
)
SECOND_ANSWER = (
    "The fee is not payable.\n\n"
    + "The cap applies to the sum of all delay damages under the agreement. " * 30
    + "\n\n| Item | Amount |\n|---|---|\n| Cap | 10% |\n\n"
    "Evidence:\n\n"
    '- chunk 2 of document 7.1: "SOURCE: 7.1 TITLE: Suspension Agreement"'
)


@pytest.fixture
def session_lookup(suite, hook_module, monkeypatch):
    """The hook with its session key set, so the cloud injection ledger runs."""
    if suite.name != "claude-code":
        pytest.skip("Claude Code answer mode")
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "off")
    monkeypatch.setenv("COGNEE_RECALL_ANSWER", "true")
    return module


def _answer(module, monkeypatch, prompt, text) -> str:
    item = {"source": "graph", "text": text}
    return _context(drive_recall(module, monkeypatch, prompt=prompt, recall={"graph": [item]}))


def test_the_answer_keeps_its_line_breaks(session_lookup, monkeypatch):
    # A table row per line and a source per line: joined with a blank line
    # between every line they are no longer a table or a list.
    context = _answer(session_lookup, monkeypatch, PROMPT, TABLE_ANSWER)

    assert TABLE_ANSWER in context


def test_a_later_answer_keeps_the_lines_it_shares_with_an_earlier_one(session_lookup, monkeypatch):
    first = _answer(session_lookup, monkeypatch, PROMPT, TABLE_ANSWER)
    second = _answer(session_lookup, monkeypatch, PROMPT + " and the cap?", SECOND_ANSWER)

    assert TABLE_ANSWER in first
    # The ledger drops entries an earlier prompt injected; an answer is not a
    # list of entries, so its conclusion, table header and sources stay.
    assert SECOND_ANSWER in second


def test_the_same_answer_twice_is_injected_twice(session_lookup, monkeypatch):
    _answer(session_lookup, monkeypatch, PROMPT, TABLE_ANSWER)

    assert TABLE_ANSWER in _answer(session_lookup, monkeypatch, PROMPT, TABLE_ANSWER)


def test_fenced_code_in_an_answer_stays_whole(session_lookup, monkeypatch):
    fenced = "```python\ndef exit_amount():\n\n    return 5_500 * 141\n```"
    text = "Computed as follows.\n\n" + fenced + "\n\n" + "The delay period ran. " * 60

    assert text.strip() in _answer(session_lookup, monkeypatch, PROMPT, text)


def test_a_context_returned_in_answer_mode_is_still_rendered_as_a_context(
    session_lookup, monkeypatch
):
    # A server without an LLM answers a completion request with the context.
    context = (
        "## Relevant entities\n### exit amount (concept)\n- the amount due on exit\n\n"
        "## Relevant passages\nThe parties agreed an exit amount of 775,500."
    )

    injected = _answer(session_lookup, monkeypatch, PROMPT, context)

    assert "=== Cognee memory: entities ===" in injected
    assert "=== Cognee memory: passages ===" in injected
