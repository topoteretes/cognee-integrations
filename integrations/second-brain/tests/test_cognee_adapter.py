"""The real cognee adapter, driven by a FAKE HTTP client (no cognee, no network).

Proves the HTTP-backed adapter maps ingest/answer/forget onto the cognee server
endpoints, strips the Evidence block, resolves citations back to the source
message, and stays graceful on an empty/missing brain. The refusal case here is
the same headline guard as test_citation_guard, but exercised end-to-end through
the adapter over HTTP (the note's text is quoted in the Evidence block, yet a
refusal cites nothing).
"""

import uuid

from cognee_integration_second_brain.cognee_adapter import CogneeChatMemoryAdapter
from cognee_integration_second_brain.interface import Conversation, Message, dataset_for

_NOTE = "I parked the car on level 3 of the garage"


class _FakeHttpClient:
    """Records calls and returns a canned recall payload; mirrors CogneeHttpClient."""

    def __init__(self, recall_results=None):
        self.calls = []
        self._recall_results = recall_results or []

    async def remember(self, text, *, dataset_name, session_id=None):
        self.calls.append(("remember", {"text": text, "dataset_name": dataset_name}))

    async def recall(self, query, *, dataset_name, top_k=15, session_id=None):
        self.calls.append(
            ("recall", {"query": query, "dataset_name": dataset_name, "top_k": top_k})
        )
        return self._recall_results

    async def forget(self, *, dataset_name):
        self.calls.append(("forget", {"dataset_name": dataset_name}))


def _convo(transport="telegram", source="chat1", canonical="alice"):
    return Conversation(
        transport=transport,
        source=source,
        canonical_user=canonical,
        external_user="tg1",
        msg_ref=f"{transport}://{source}/1",
    )


def _grounded(answer: str) -> dict:
    """A graph-completion result whose text carries an Evidence block quoting the note."""
    data_id = uuid.uuid5(uuid.NAMESPACE_DNS, "note")
    return {
        "text": f'{answer}\n\nEvidence:\n- chunk 1 of document x (data_id: {data_id}): "{_NOTE}"',
        "source": "graph",
    }


def test_cognee_adapter_constructs_without_network():
    # Default client does a lazy httpx import inside each request, so construction
    # touches no network and needs no cognee installed.
    CogneeChatMemoryAdapter()


def test_dataset_is_per_user_brain():
    assert dataset_for(_convo(canonical="abc-canonical")) == "brain:abc-canonical"


async def test_ingest_posts_remember_with_dataset_only():
    fake = _FakeHttpClient()
    adapter = CogneeChatMemoryAdapter(client=fake)
    await adapter.ingest(_convo(), Message(text=_NOTE, ts="2026-06-12T09:00:00"))

    name, kwargs = fake.calls[0]
    assert name == "remember"
    assert kwargs["text"] == _NOTE
    assert kwargs["dataset_name"] == "brain:alice"


async def test_answer_recalls_strips_evidence_and_cites_source():
    fake = _FakeHttpClient(recall_results=[_grounded("On level 3.")])
    adapter = CogneeChatMemoryAdapter(client=fake, top_k=7)
    convo = _convo()
    # Record the source message so the citation can resolve back to it.
    await adapter.ingest(convo, Message(text=_NOTE, ts="2026-06-12T09:00:00"))

    answer = await adapter.answer(convo, "where did I park?")

    recall_call = next(kwargs for name, kwargs in fake.calls if name == "recall")
    assert recall_call["dataset_name"] == "brain:alice"
    assert recall_call["top_k"] == 7

    assert answer.text == "On level 3."
    assert "Evidence:" not in answer.text
    assert len(answer.citations) == 1
    assert answer.citations[0].source_transport == "telegram"
    assert answer.citations[0].content == _NOTE


async def test_refusal_over_http_is_never_cited():
    # The Evidence block still quotes the note (the hard case), but a refusal adds
    # no term beyond the query, so it cites nothing.
    refusal = "There is no information about where you parked in the provided context."
    fake = _FakeHttpClient(recall_results=[_grounded(refusal)])
    adapter = CogneeChatMemoryAdapter(client=fake)
    convo = _convo()
    await adapter.ingest(convo, Message(text=_NOTE, ts="2026-06-12T09:00:00"))

    answer = await adapter.answer(convo, "where did I park?")
    assert answer.text == refusal
    assert answer.citations == []


async def test_empty_recall_is_graceful_empty_memory():
    # A missing/empty brain: the client maps a 4xx to [] -> the bot says so, not error.
    fake = _FakeHttpClient(recall_results=[])
    adapter = CogneeChatMemoryAdapter(client=fake)
    answer = await adapter.answer(_convo(), "anything?")
    assert "do not have anything" in answer.text.lower()
    assert answer.citations == []


async def test_forget_clears_dataset_and_drops_citations():
    fake = _FakeHttpClient()
    adapter = CogneeChatMemoryAdapter(client=fake)
    convo = _convo()
    await adapter.ingest(convo, Message(text=_NOTE, ts="2026-06-12T09:00:00"))

    # forget accepts a Conversation or a raw canonical user id (used by /forget me).
    await adapter.forget("alice")

    forget_call = next(kwargs for name, kwargs in fake.calls if name == "forget")
    assert forget_call["dataset_name"] == "brain:alice"
    # Citation map for that brain is dropped, so a later recall can't resurface it.
    assert adapter._citations.get("brain:alice") is None
