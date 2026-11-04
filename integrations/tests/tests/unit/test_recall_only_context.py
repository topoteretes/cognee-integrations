"""The only_context contract after SDK-904: graph context only, guidance apart.

An ``only_context`` completion recall on cognee >= 1.6.0 returns ONE item per
dataset whose ``text`` is the full LLM input the completion would have received.
Sent WITH a session id that string opens with the session's last ten Q&A pairs
in full — the agent's own previous turns, which already sit in its context
window — and it is wrapped in the question template meant for cognee's own LLM.
So the prompt hook now:

  * sends the graph-scope request WITHOUT the session id, so no history comes
    back, and cuts the question template away (``_recall_text``), injecting
    only the retrieved context, shrunk (own-session passages dropped, passages
    capped, empty entity bullets pruned) under ``=== Cognee memory ===``;
  * sends a second, parallel ``session_context`` request WITH the session id
    for the server-rendered guidance block, injected under
    ``=== Active agent guidance ===``;
  * injects an item it cannot parse (a pre-1.6.0 bare context, an unknown
    template) whole — fail-open — and logs ``recall_context_unparsed``;
  * keeps ``counts`` on all five keys, with ``per_scope`` listing what was
    dispatched: ``graph`` and ``session_context``.

All registered suites carry the contract identically, so all are exercised.
"""

from __future__ import annotations

import pytest
from utils.recall import SCOPES, assert_valid_per_scope, drive_recall

PROMPT = "what did we decide about the retry policy?"
OWN_SESSION = "sid"  # what drive_recall's ``_load_session_id`` returns

#: What a 1.6.0 server hands back for the graph scope with only_context and NO
#: session id: the question template around the retrieved context.
PASSAGE_OTHER = (
    "Session ID: claude_other\n\nQuestion: retry policy?\n\nAnswer: exponential backoff."
)
PASSAGE_OWN = (
    f"Session ID: {OWN_SESSION}\n\nQuestion: earlier turn\n\nAnswer: the agent's own answer."
)
CONTEXT = (
    "## Relevant passages\n"
    f"{PASSAGE_OTHER}\n---\n{PASSAGE_OWN}\n\n"
    "## Relevant entities\n"
    "### retry policy\n"
    "- retry policy is a concept.\n"
    "- retry policy belongs to set session_learnings.\n"
    "- retry policy backs off exponentially with a 30s ceiling.\n\n"
    "## Related facts\n"
    "- The release branch pins the retry policy."
)
TEMPLATE_HEAD = (
    f"The question is: `{PROMPT}`\n"
    "Answer using this sectioned context. Keep the answer brief and do not use "
    "information outside the context.\n\nContext:\n`"
)
FULL_TEXT = f"{TEMPLATE_HEAD}{CONTEXT}`"
SYSTEM_PROMPT = "Answer the question using the provided context."

ITEM_160 = {"source": "graph", "text": FULL_TEXT, "system_prompt": SYSTEM_PROMPT}
ITEM_LEGACY = {"source": "graph", "content": "bare context"}
GUIDANCE_BLOCK = (
    "## Active session guidance\nWhen guidance conflicts, prefer the later item.\n"
    "### Preferences\n- Run ruff before committing."
)
ITEM_GUIDANCE = {"source": "session_context", "content": GUIDANCE_BLOCK, "context_profile": "qa"}


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    # These pin the trimmed text itself; the credit tags woven into it are
    # covered by test_memory_credit.py.
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setenv("COGNEE_PLUGIN_CITE_MEMORY", "off")  # after the harness scrubs COGNEE_*
    return module


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_only_the_retrieved_context_lands_under_the_memory_heading(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_160]})

    context = _context(run)
    assert "=== Cognee memory" in context
    assert "[cognee-memory]\n## Relevant" in context, context
    # The question template and its answering instructions never reach the agent.
    assert "The question is:" not in context
    assert "Answer using this sectioned context" not in context
    assert SYSTEM_PROMPT not in context
    # Retrieved material from elsewhere survives, in all three sections.
    assert PASSAGE_OTHER in context
    assert "- retry policy backs off exponentially with a 30s ceiling." in context
    assert "## Related facts\n- The release branch pins the retry policy." in context
    assert run.detail("context_lookup_hit")["counts"]["graph_context"] == 1


def test_the_sessions_own_passages_and_empty_bullets_are_dropped(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_160]})

    context = _context(run)
    assert "the agent's own answer" not in context, "own-session passages are already in context"
    assert "is a concept." not in context and "belongs to set" not in context
    trimmed = run.detail("recall_context_trimmed")
    assert trimmed["parsed"] is True
    assert trimmed["passages_in"] == 2 and trimmed["passages_dropped"] == 1
    assert trimmed["entity_lines_removed"] == 2
    assert trimmed["chars_out"] < trimmed["chars_in"]


def test_a_pre_160_item_with_bare_content_still_renders(lookup, monkeypatch):
    """An older server puts the retrieval context in ``content`` and has no
    ``text``; the hook must keep reading it."""
    run = drive_recall(lookup, monkeypatch, recall={"graph": [ITEM_LEGACY]})

    context = _context(run)
    assert "=== Cognee memory" in context
    assert "[cognee-memory]\nbare context" in context
    assert run.detail("context_lookup_hit")["counts"]["graph_context"] == 1
    assert not run.fired("recall_context_trimmed") and not run.fired("recall_context_unparsed")


def test_an_item_the_trimmer_cannot_read_is_injected_whole(lookup, monkeypatch):
    """Fail-open: a template this plugin does not know costs tokens, never memory."""
    odd = {"source": "graph", "text": "Here is what I found:\n- the retry policy backs off"}
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [odd]})

    assert "[cognee-memory]\nHere is what I found:\n- the retry policy backs off" in _context(run)
    unparsed = run.detail("recall_context_unparsed")
    assert unparsed is not None and unparsed["parsed"] is False
    assert run.detail("context_lookup_hit")["counts"]["graph_context"] == 1


def test_a_plain_prompt_makes_two_requests_and_only_guidance_carries_the_session(
    lookup, monkeypatch
):
    """The graph recall goes WITHOUT the session id — with it the server prepends
    the conversation history — and the guidance request goes WITH it."""
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_160]})

    assert sorted(run.calls) == sorted(SCOPES), run.calls
    graph = run.kwargs["graph"]
    assert graph["scope"] == ["graph"]
    assert graph["search_type"] == "HYBRID_COMPLETION"
    assert graph["only_context"] is True
    assert not graph.get("session_id"), f"the graph recall must not carry the session: {graph}"
    guidance = run.kwargs["session_context"]
    assert guidance["scope"] == ["session_context"]
    assert guidance["session_id"] == OWN_SESSION
    assert guidance["context_profile"] == "qa"
    assert guidance["search_type"] is None


def test_the_guidance_block_is_injected_under_its_own_heading(lookup, monkeypatch):
    run = drive_recall(
        lookup,
        monkeypatch,
        prompt=PROMPT,
        recall={"graph": [ITEM_160], "session_context": [ITEM_GUIDANCE]},
    )

    context = _context(run)
    assert "=== Active agent guidance ===" in context
    assert f"[agent-guidance]\n{GUIDANCE_BLOCK}" in context
    assert context.index("=== Cognee memory") < context.index("=== Active agent guidance ===")
    counts = run.detail("context_lookup_hit")["counts"]
    assert counts["graph_context"] == 1 and counts["session_context"] == 1


def test_a_preference_block_trailing_the_graph_item_stands_in_when_guidance_is_empty(
    lookup, monkeypatch
):
    """Without a session id the server can still append the durable-preference
    block after the template; it is used only when the guidance request brought
    nothing, since that request renders the same lines."""
    with_prefs = {"source": "graph", "text": f"{FULL_TEXT}\n\n{GUIDANCE_BLOCK}"}

    alone = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [with_prefs]})
    assert _context(alone).count("Run ruff before committing") == 1
    assert alone.detail("context_lookup_hit")["counts"]["session_context"] == 1

    other = {"source": "session_context", "content": "## Active session guidance\n- other"}
    both = drive_recall(
        lookup,
        monkeypatch,
        prompt=PROMPT,
        recall={"graph": [with_prefs], "session_context": [other]},
    )
    assert "Run ruff before committing" not in _context(both)
    assert "- other" in _context(both)


def test_guidance_alone_is_still_a_hit(lookup, monkeypatch):
    run = drive_recall(
        lookup, monkeypatch, prompt=PROMPT, recall={"session_context": [ITEM_GUIDANCE]}
    )
    assert "=== Active agent guidance ===" in _context(run)
    assert run.detail("context_lookup_hit")["counts"]["session_context"] == 1


def test_counts_keep_every_key_while_per_scope_lists_only_the_dispatched(lookup, monkeypatch):
    """The status line and last_recall.json read fixed keys; the retired scopes
    stay present at zero. ``per_scope`` is what actually ran."""
    run = drive_recall(lookup, monkeypatch, prompt=PROMPT, recall={"graph": [ITEM_160]})

    detail = run.detail("context_lookup_hit")
    counts = detail["counts"]
    assert set(counts) == {"session", "trace", "graph_context", "session_context", "code"}, counts
    assert counts["graph_context"] == 1
    assert all(counts[k] == 0 for k in ("session", "trace", "session_context", "code")), counts
    assert_valid_per_scope(detail["per_scope"], SCOPES)
    assert list(detail["per_scope"]) == list(SCOPES)
