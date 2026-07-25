"""Deterministic, mocked tests for the web-widget chat-memory adapter + server.

These run in CI with no real LLM keys: the adapter is driven against a fake
cognee HTTP client, so the tests assert its *behavior* — session scoping,
opt-out, citation parsing, graph-over-session preference, and per-conversation
forget — without touching a provider, a cognee server, or the network.

Crucially, ``recall`` returns cognee's **real** shape: the answer text with an
appended ``Evidence:`` block (that is how ``include_references=True`` surfaces
sources), not a fabricated structured ``references`` list.
"""

from unittest.mock import AsyncMock

import pytest
from cognee_integration_web_widget.adapter import _EMPTY_ANSWER, ChatMemoryAdapter
from cognee_integration_web_widget.citations import split_evidence

# A graph completion exactly as recall(include_references=True) returns it:
# the answer prose followed by an appended, grounded "Evidence:" block.
GRAPH_ANSWER = "Cognee stores memory as a knowledge graph."
GRAPH_ENTRY = {
    "source": "graph",
    "dataset_name": "web:demo:docs",
    "score": 0.91,
    "text": (
        f"{GRAPH_ANSWER}\n\n"
        "Evidence:\n"
        "- chunk 1 of document guide.md (data_id: d1, chunk_id: c1): "
        '"Cognee turns raw data into a knowledge graph."'
    ),
}


# --- Pure logic (no client needed) ------------------------------------------


def test_session_id_convention(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="acme", visitor_id="v1", conversation_id="c1")
    assert conv.session_id == "web:acme:v1:c1"
    assert adapter.docs_dataset("acme") == "web:acme:docs"


def test_split_evidence_parses_bullets_and_strips_block():
    prose, citations = split_evidence(GRAPH_ENTRY["text"])
    # The Evidence block is stripped from the prose shown to the user.
    assert prose == GRAPH_ANSWER
    assert "Evidence:" not in prose
    # ...and each bullet becomes a citation with its document + ids.
    assert len(citations) == 1
    assert citations[0].document == "guide.md"
    assert citations[0].data_id == "d1"
    assert citations[0].chunk_id == "c1"
    assert citations[0].snippet == "Cognee turns raw data into a knowledge graph."


def test_split_evidence_without_block_yields_no_citations():
    prose, citations = split_evidence("You told me your name is Ada.")
    assert prose == "You told me your name is Ada."
    assert citations == []


# --- Adapter over the fake HTTP client --------------------------------------


async def test_answer_returns_clean_text_and_citations(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(top_k=5, client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    # The raw Evidence block never leaks into the displayed answer.
    assert answer.text == GRAPH_ANSWER
    assert "Evidence:" not in answer.text
    assert answer.session_id == "web:demo:v1:c1"
    assert [c.document for c in answer.citations] == ["guide.md"]
    assert answer.as_dict()["answer"] == GRAPH_ANSWER

    # recall must be session-scoped, docs-scoped, ask for references, and pass top_k.
    call = fake_client.recall.call_args
    assert call.args[0] == "What is cognee?"
    assert call.kwargs["session_id"] == "web:demo:v1:c1"
    assert call.kwargs["datasets"] == ["web:demo:docs"]
    assert call.kwargs["top_k"] == 5


async def test_answer_prefers_generated_completion_over_session_turns(fake_client):
    """A prior/echoed session turn must never be shown as the answer."""
    # recall returns session entries *before* the graph completion.
    fake_client.recall.return_value = [
        {"source": "session", "answer": "user: What is cognee?"},
        {"source": "session", "answer": "A stale answer from a previous turn."},
        GRAPH_ENTRY,
    ]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == GRAPH_ANSWER  # not the echoed question or stale turn


async def test_answer_opt_out_recalls_without_session(fake_client):
    fake_client.recall.return_value = [GRAPH_ENTRY]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    await adapter.answer(conversation=conv, query="hi", remember=False, use_docs=False)

    call = fake_client.recall.call_args
    assert call.kwargs["session_id"] is None  # nothing is persisted
    assert call.kwargs["datasets"] is None  # docs mode off


async def test_answer_graceful_when_dataset_missing_or_empty(fake_client):
    """A never-seeded docs dataset returns a 4xx the client maps to no results;
    the widget degrades to an empty-memory answer, not a 500."""
    fake_client.recall.return_value = []  # what the client returns on a 4xx / no hits
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == _EMPTY_ANSWER
    assert answer.citations == []
    # It still tried the docs-scoped recall.
    assert fake_client.recall.call_args.kwargs["datasets"] == ["web:demo:docs"]


async def test_refusal_answer_is_never_cited(fake_client):
    """A "no information" answer carries no Evidence block, so it is never cited."""
    fake_client.recall.return_value = [
        {"source": "graph", "text": "I don't have any information about that."}
    ]
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    answer = await adapter.answer(conversation=conv, query="What is cognee?")

    assert answer.text == "I don't have any information about that."
    assert answer.citations == []


async def test_ingest_docs_remembers_each_doc_without_session(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    await adapter.ingest_docs(site_id="acme", documents=["one", "", "  ", "two"])

    # Blank docs are skipped; each real doc is a shared (session-less) remember.
    assert fake_client.remember.await_count == 2
    for call in fake_client.remember.call_args_list:
        assert call.kwargs["dataset_name"] == "web:acme:docs"
        assert "session_id" not in call.kwargs


async def test_forget_clears_only_this_conversation(fake_client):
    adapter = ChatMemoryAdapter(client=fake_client)
    conv = adapter.conversation(site_id="demo", visitor_id="v1", conversation_id="c1")

    cleared = await adapter.forget(conversation=conv)

    assert cleared is True
    assert fake_client.forget.call_args.kwargs["dataset_name"] == "web:demo:v1:c1"


# --- Server flow (thin FastAPI proxy over the adapter) ----------------------


@pytest.fixture
def web_client(fake_client):
    """A TestClient over the widget server with the HTTP client faked."""
    from cognee_integration_web_widget import server as server_mod
    from fastapi.testclient import TestClient

    fake_client.recall = AsyncMock(return_value=[GRAPH_ENTRY])
    # The server builds its adapter at import time (real HTTP client); swap it.
    server_mod.adapter.client = fake_client
    with TestClient(server_mod.app) as test_client:
        yield test_client, fake_client


def test_chat_endpoint_returns_answer_and_citations(web_client):
    test_client, _ = web_client
    resp = test_client.post(
        "/api/chat", json={"message": "What is cognee?", "conversation_id": "c1"}
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["answer"] == GRAPH_ANSWER
    assert body["session_id"] == "web:demo:anonymous:c1"
    assert [c["document"] for c in body["citations"]] == ["guide.md"]


def test_chat_forget_command_is_not_answered(web_client):
    test_client, fake = web_client
    before = fake.recall.await_count
    resp = test_client.post("/api/chat", json={"message": "/forget", "conversation_id": "c1"})
    assert resp.status_code == 200
    assert resp.json()["citations"] == []
    # A /forget clears the conversation and is NOT sent through recall.
    assert fake.forget.call_args.kwargs["dataset_name"] == "web:demo:anonymous:c1"
    assert fake.recall.await_count == before


def test_forget_endpoint_clears_conversation(web_client):
    test_client, fake = web_client
    resp = test_client.post("/api/forget", json={"conversation_id": "c1"})
    body = resp.json()
    assert resp.status_code == 200
    assert body["cleared"] is True
    assert body["session_id"] == "web:demo:anonymous:c1"
    assert fake.forget.call_args.kwargs["dataset_name"] == "web:demo:anonymous:c1"
