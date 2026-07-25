"""Unit tests for the cognee-backed adapter over a fake HTTP client.

Every cognee call (add / cognify / search / forget) goes through an injected fake
client — no cognee, no network, no keys. The critical test is the citation
round-trip: the CHUNKS search returns a chunk whose *text* carries the provenance
header written at ingest, and the resulting Answer citation must recover the
channel / ts / permalink / author from that header.
"""

import asyncio

from cognee_integration_slack.cognee_memory import (
    CogneeChatMemory,
    _encode_provenance,
    _first_text,
    _normalize_chunk_payloads,
)
from cognee_integration_slack.memory_adapter import Answer, ConversationRef

REF = ConversationRef(team_id="T1", channel_id="C42")


class FakeCogneeClient:
    """Records add/cognify/forget calls; returns canned search results by type."""

    def __init__(self, *, search_results=None, search_error=None):
        self.add_calls: list[dict] = []
        self.cognify_calls: list[str] = []
        self.forget_calls: list[str] = []
        self.search_calls: list[dict] = []
        # {search_type: results}
        self._search_results = search_results or {}
        self._search_error = search_error

    async def add(self, text, *, dataset_name, node_set=None):
        self.add_calls.append({"text": text, "dataset_name": dataset_name, "node_set": node_set})

    async def cognify(self, *, dataset_name):
        self.cognify_calls.append(dataset_name)

    async def search(self, query, *, search_type, dataset_name, node_name=None, top_k=10):
        self.search_calls.append(
            {
                "query": query,
                "search_type": search_type,
                "dataset_name": dataset_name,
                "node_name": node_name,
                "top_k": top_k,
            }
        )
        if self._search_error is not None:
            raise self._search_error
        return list(self._search_results.get(search_type, []))

    async def forget(self, *, dataset_name):
        self.forget_calls.append(dataset_name)


def _chunk_payload(text: str, chunk_index: int = 0) -> dict:
    """A dict shaped like a real search(CHUNKS) result item (only text is used)."""
    return {
        "id": f"chunk-{chunk_index}",
        "text": text,
        "document_id": f"doc-{chunk_index}",
        "document_name": "message.txt",
        "chunk_index": chunk_index,
        "belongs_to_set": ["C42"],
    }


# --------------------------------------------------------------------------- #
# ingest / flush                                                              #
# --------------------------------------------------------------------------- #


def test_ingest_adds_with_provenance_and_node_set():
    client = FakeCogneeClient()
    memory = CogneeChatMemory(client, top_k=5)
    asyncio.run(
        memory.ingest(
            REF,
            ts="1700000000.000100",
            text="We decided to ship on Friday.",
            permalink="https://slack.example/archives/C42/p1700000000000100",
            author="alice",
        )
    )

    assert len(client.add_calls) == 1
    call = client.add_calls[0]
    assert call["dataset_name"] == "slack_C42"
    assert call["node_set"] == ["C42"]
    # The stored text carries the citation provenance + the original message.
    assert call["text"].startswith("[cognee-slack] channel=C42 ts=1700000000.000100 author=alice")
    assert "permalink=https://slack.example/archives/C42/p1700000000000100" in call["text"]
    assert "We decided to ship on Friday." in call["text"]


def test_ingest_does_not_cognify():
    client = FakeCogneeClient()
    memory = CogneeChatMemory(client)
    asyncio.run(memory.ingest(REF, ts="1.0", text="hi", permalink="https://x", author="bob"))
    assert client.cognify_calls == []


def test_flush_triggers_cognify_for_channel_dataset():
    client = FakeCogneeClient()
    memory = CogneeChatMemory(client)
    asyncio.run(memory.flush(REF))
    assert client.cognify_calls == ["slack_C42"]


# --------------------------------------------------------------------------- #
# answer — the citation round-trip                                            #
# --------------------------------------------------------------------------- #


def test_answer_round_trip_recovers_citation_from_chunk_provenance():
    ts = "1700000000.000100"
    permalink = "https://slack.example/archives/C42/p1700000000000100"
    stored = _encode_provenance(
        "We decided to ship on Friday.",
        channel_id="C42",
        ts=ts,
        author="alice",
        permalink=permalink,
    )
    client = FakeCogneeClient(
        search_results={
            "GRAPH_COMPLETION": ["The team decided to ship on Friday."],
            "CHUNKS": [_chunk_payload(stored)],
        }
    )
    memory = CogneeChatMemory(client, top_k=5)

    answer = asyncio.run(memory.answer(REF, query="what did we decide?"))

    assert isinstance(answer, Answer)
    assert answer.text == "The team decided to ship on Friday."
    assert len(answer.citations) == 1
    cite = answer.citations[0]
    assert cite.ok is True
    assert cite.permalink == permalink
    assert cite.channel_id == "C42"
    assert cite.ts == ts
    assert cite.author == "alice"
    assert cite.snippet == "We decided to ship on Friday."

    # Two searches, right types / scope / top_k.
    assert len(client.search_calls) == 2
    by_type = {c["search_type"]: c for c in client.search_calls}
    assert set(by_type) == {"GRAPH_COMPLETION", "CHUNKS"}
    assert by_type["CHUNKS"]["dataset_name"] == "slack_C42"
    assert by_type["CHUNKS"]["node_name"] == ["C42"]
    assert by_type["CHUNKS"]["top_k"] == 5
    assert by_type["GRAPH_COMPLETION"]["top_k"] == 5


def test_answer_dedupes_multiple_chunks_from_one_message():
    permalink = "https://slack.example/x"
    stored = _encode_provenance(
        "shared message", channel_id="C42", ts="1.0", author="alice", permalink=permalink
    )
    client = FakeCogneeClient(
        search_results={
            "GRAPH_COMPLETION": ["answer"],
            "CHUNKS": [_chunk_payload(stored, 0), _chunk_payload(stored, 1)],
        }
    )
    memory = CogneeChatMemory(client)
    answer = asyncio.run(memory.answer(REF, query="q"))
    assert len(answer.citations) == 1
    assert answer.citations[0].permalink == permalink


def test_answer_chunk_without_provenance_degrades_to_text():
    # A chunk with no provenance header → plain-text citation, never a broken link.
    client = FakeCogneeClient(
        search_results={
            "GRAPH_COMPLETION": ["here is what I found"],
            "CHUNKS": [_chunk_payload("orphan chunk text with no header")],
        }
    )
    memory = CogneeChatMemory(client)
    answer = asyncio.run(memory.answer(REF, query="q"))
    assert answer.text == "here is what I found"
    assert len(answer.citations) == 1
    cite = answer.citations[0]
    assert cite.ok is False
    assert cite.permalink == ""
    assert cite.snippet == "orphan chunk text with no header"


def test_answer_blank_permalink_degrades_to_text():
    stored = _encode_provenance(
        "stored snippet", channel_id="C42", ts="1.0", author="alice", permalink=""
    )
    client = FakeCogneeClient(
        search_results={"GRAPH_COMPLETION": ["a"], "CHUNKS": [_chunk_payload(stored)]}
    )
    memory = CogneeChatMemory(client)
    answer = asyncio.run(memory.answer(REF, query="q"))
    cite = answer.citations[0]
    assert cite.ok is False
    assert cite.permalink == ""
    assert cite.snippet == "stored snippet"
    assert cite.author == "alice"


def test_answer_returns_empty_when_channel_has_no_data():
    # Fresh/empty channel: the client maps a missing dataset (4xx) to [] for both
    # searches, so answer() degrades to a calm empty Answer.
    client = FakeCogneeClient(search_results={})  # both searches return []
    memory = CogneeChatMemory(client)
    answer = asyncio.run(memory.answer(REF, query="what did we decide?"))
    assert isinstance(answer, Answer)
    assert answer.text == ""
    assert answer.citations == []


def test_answer_handles_access_control_wrapper_shape():
    # Prove _first_text / _normalize handle the ENABLE_BACKEND_ACCESS_CONTROL shape:
    # search returns [{"dataset_id":..., "search_result": <result>}].
    stored = _encode_provenance(
        "wrapped msg",
        channel_id="C42",
        ts="1.0",
        author="alice",
        permalink="https://slack.example/x",
    )
    client = FakeCogneeClient(
        search_results={
            "GRAPH_COMPLETION": [
                {"dataset_id": "d1", "dataset_name": "slack_C42", "search_result": "wrapped answer"}
            ],
            "CHUNKS": [
                {
                    "dataset_id": "d1",
                    "dataset_name": "slack_C42",
                    "search_result": [_chunk_payload(stored)],
                }
            ],
        }
    )
    memory = CogneeChatMemory(client)
    answer = asyncio.run(memory.answer(REF, query="q"))
    assert answer.text == "wrapped answer"
    assert len(answer.citations) == 1
    assert answer.citations[0].permalink == "https://slack.example/x"


# --------------------------------------------------------------------------- #
# forget                                                                      #
# --------------------------------------------------------------------------- #


def test_forget_deletes_channel_dataset():
    client = FakeCogneeClient()
    memory = CogneeChatMemory(client)
    asyncio.run(memory.forget(REF))
    assert client.forget_calls == ["slack_C42"]


# --------------------------------------------------------------------------- #
# shape helpers (direct)                                                       #
# --------------------------------------------------------------------------- #


def test_normalize_chunk_payloads_flat_and_wrapped():
    flat = [{"document_id": "a"}, {"document_id": "b"}]
    assert _normalize_chunk_payloads(flat) == flat

    wrapped = [{"dataset_id": "d1", "search_result": [{"document_id": "a"}]}]
    assert _normalize_chunk_payloads(wrapped) == [{"document_id": "a"}]

    assert _normalize_chunk_payloads(None) == []


def test_first_text_variants():
    assert _first_text(["hello"]) == "hello"
    assert _first_text("hello") == "hello"
    assert _first_text([{"search_result": "wrapped"}]) == "wrapped"
    assert _first_text([]) == ""
