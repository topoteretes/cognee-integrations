"""The only_context contract of cognee 1.6.0: memory is one request (SDK-741).

An ``only_context`` completion recall on cognee >= 1.6.0 returns ONE item per
dataset whose ``text`` is the full LLM input the completion would have received:
the conversation history (what the old ``session`` scope fetched), the question
with the retrieved graph context, and the session guidance block (the old
``session_context`` scope). So the prompt hook no longer fans out over four
scopes — it makes exactly one graph-scope request, with the session id attached
so the server can build the history and guidance layers, and injects the item
whole.

Contract:
  * the item's ``text`` is injected under ``=== Cognee memory ===`` in full — no
    truncation, because the context sits in the middle and the guidance at the
    end, so any cut would take exactly what memory is for; the server's
    ``system_prompt`` is not part of what the model sees;
  * an item from a pre-1.6.0 server (bare ``content``, no ``text``) still
    renders — old servers keep working;
  * a plain prompt makes exactly one recall call: ``scope=["graph"]``,
    ``HYBRID_COMPLETION``, ``only_context=True``, and a non-empty session id;
  * ``counts`` (the ``hits`` written to last_recall.json) still carries all five
    keys, the retired scopes at zero, while ``per_scope`` lists only what was
    dispatched.

All registered suites carry the contract identically, so all are exercised.
"""

from __future__ import annotations

import pytest
from utils.recall import SCOPES, assert_valid_per_scope, drive_recall

#: What a 1.6.0 server hands back for the graph scope with only_context: the
#: rendered prompt, well past the 1500 characters the hook used to cut at.
QUESTION = "The question is: `what did we decide about the retry policy?`"
CONTEXT_BLOCK = "Context:\n" + "\n".join(
    f"- Session ID: claude_{i:03d}: the retry policy backs off exponentially, fact {i}."
    for i in range(40)
)
GUIDANCE = "Guidance:\nPrefer the release branch; run ruff before committing."
FULL_TEXT = (
    f"User: earlier turn\nAssistant: earlier answer\n\n{QUESTION}\n\n{CONTEXT_BLOCK}\n\n{GUIDANCE}"
)
SYSTEM_PROMPT = "Answer the question using the provided context."

ITEM_160 = {"source": "graph", "text": FULL_TEXT, "system_prompt": SYSTEM_PROMPT}
ITEM_LEGACY = {"source": "graph", "content": "bare context"}


@pytest.fixture
def lookup(suite, hook_module):
    return hook_module(suite, "session-context-lookup.py")


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_a_160_item_is_injected_whole_under_the_memory_heading(lookup, monkeypatch):
    assert len(FULL_TEXT) > 1500, "the fixture must exceed the retired truncation cap"
    run = drive_recall(lookup, monkeypatch, recall={"graph": [ITEM_160]})

    context = _context(run)
    assert "=== Cognee memory ===" in context
    assert "=== Knowledge graph snapshot ===" not in context
    assert f"[cognee-memory]\n{FULL_TEXT}" in context, "the text must land whole and untouched"
    assert SYSTEM_PROMPT not in context
    assert run.detail("context_lookup_hit")["counts"]["graph_context"] == 1


def test_a_pre_160_item_with_bare_content_still_renders(lookup, monkeypatch):
    """An older server puts the retrieval context in ``content`` and has no
    ``text``; the hook must keep reading it."""
    run = drive_recall(lookup, monkeypatch, recall={"graph": [ITEM_LEGACY]})

    context = _context(run)
    assert "=== Cognee memory ===" in context
    assert "[cognee-memory]\nbare context" in context
    assert run.detail("context_lookup_hit")["counts"]["graph_context"] == 1


def test_a_plain_prompt_makes_exactly_one_recall_call(lookup, monkeypatch):
    """One graph-scope request, with the session id so the server can build
    the history and guidance layers into the item."""
    run = drive_recall(lookup, monkeypatch, recall={"graph": [ITEM_160]})

    assert run.calls == ["graph"], run.calls
    graph = run.kwargs["graph"]
    assert graph["scope"] == ["graph"]
    assert graph["search_type"] == "HYBRID_COMPLETION"
    assert graph["only_context"] is True
    assert graph.get("session_id"), f"the session id must be sent: {graph}"


def test_counts_keep_every_key_while_per_scope_lists_only_the_dispatched(lookup, monkeypatch):
    """The status line and last_recall.json read fixed keys; the retired scopes
    stay present at zero. ``per_scope`` is what actually ran."""
    run = drive_recall(lookup, monkeypatch, recall={"graph": [ITEM_160]})

    detail = run.detail("context_lookup_hit")
    counts = detail["counts"]
    assert set(counts) == {"session", "trace", "graph_context", "session_context", "code"}, counts
    assert counts["graph_context"] == 1
    assert all(counts[k] == 0 for k in ("session", "trace", "session_context", "code")), counts
    assert_valid_per_scope(detail["per_scope"], SCOPES)
    assert list(detail["per_scope"]) == ["graph"]
