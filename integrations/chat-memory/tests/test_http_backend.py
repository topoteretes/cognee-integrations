"""Wire-contract tests for :class:`CogneeHttpMemoryBackend`, over a stub transport.

Keyless and network-free: an injected fake ``httpx``-style client records the
requests and returns canned cognee responses, so we assert the exact HTTP calls
the default backend makes and that provenance round-trips into citations —
without a running cognee server.
"""

import pytest
from cognee_integration_chat_memory import CogneeHttpMemoryBackend


class _FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class _FakeClient:
    """Records requests; routes by path to a canned response."""

    def __init__(self, recall_payload=None, forget_payload=None, status_code=200):
        self.calls = []
        self._recall_payload = recall_payload
        self._forget_payload = forget_payload
        self._status_code = status_code

    async def request(self, method, url, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, **kwargs})
        if url.endswith("/api/v1/recall"):
            return _FakeResponse(self._recall_payload, status_code=self._status_code)
        if url.endswith("/api/v1/forget"):
            payload = self._forget_payload if self._forget_payload is not None else {}
            return _FakeResponse(payload, status_code=self._status_code)
        return _FakeResponse(None)


@pytest.mark.asyncio
async def test_remember_posts_multipart_with_provenance():
    client = _FakeClient()
    backend = CogneeHttpMemoryBackend(base_url="http://x:8000", api_key="k", client=client)

    await backend.remember(
        "we ship on friday",
        dataset="chat:slack:t1:c1",
        session="slack:t1:c1:th1",
        external_metadata={"user": "U1", "permalink": "https://src/1"},
        item_id="id1",
    )

    call = client.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://x:8000/api/v1/remember"
    assert call["headers"]["X-Api-Key"] == "k"
    # Durable ingest: datasetName only, no session_id (which would hit the session cache).
    assert call["data"] == {"datasetName": "chat:slack:t1:c1"}
    # Provenance is encoded into the uploaded text so recall can resolve it.
    _, content, _ = call["files"]["data"]
    body = content.decode("utf-8")
    assert body.startswith("[cognee-chat-memory] user=U1 permalink=https://src/1")
    assert "we ship on friday" in body


@pytest.mark.asyncio
async def test_recall_parses_answer_and_source_citations():
    # A GRAPH_COMPLETION answer with an Evidence block whose snippet carries the
    # provenance header we embedded at remember-time.
    recall_payload = [
        {
            "source": "graph",
            "text": (
                "We ship on Friday.\n\n"
                "Evidence:\n"
                "- chunk 1 of document message.txt (data_id: abc): "
                '"[cognee-chat-memory] user=U1 permalink=https://src/1 we ship on friday"'
            ),
            "score": 0.9,
        }
    ]
    client = _FakeClient(recall_payload=recall_payload)
    backend = CogneeHttpMemoryBackend(client=client)

    citations = await backend.recall("when do we ship", dataset="d", session="s", top_k=5)

    # Primary synthesized answer, Evidence + provenance header stripped.
    assert citations[0].source == "graph"
    assert citations[0].text == "We ship on Friday."
    # A source citation resolved from the embedded provenance.
    sources = [c for c in citations if c.source == "graph_context"]
    assert any(c.permalink == "https://src/1" and c.user == "U1" for c in sources)

    # Recall request shape.
    body = client.calls[0]["json"]
    assert body["datasets"] == ["d"]
    assert "session_id" not in body  # dataset-scoped durable recall
    assert body["include_references"] is True
    assert body["search_type"] == "GRAPH_COMPLETION"


@pytest.mark.asyncio
async def test_recall_empty_returns_no_citations():
    backend = CogneeHttpMemoryBackend(client=_FakeClient(recall_payload=[]))
    assert await backend.recall("q", dataset="d", session="s", top_k=5) == []


@pytest.mark.asyncio
async def test_forget_scope_posts_forget():
    client = _FakeClient(forget_payload={"items_removed": 3})
    backend = CogneeHttpMemoryBackend(client=client)

    result = await backend.forget_scope(dataset="chat:slack:t1:c1")

    call = client.calls[0]
    assert call["url"].endswith("/api/v1/forget")
    assert call["json"] == {"dataset": "chat:slack:t1:c1", "everything": False}
    assert result["dataset"] == "chat:slack:t1:c1"
    assert result["status"] == "success"
    assert result["items_removed"] == 3


@pytest.mark.asyncio
async def test_forget_user_wipes_dataset_over_http():
    client = _FakeClient()
    backend = CogneeHttpMemoryBackend(client=client)

    result = await backend.forget_user(dataset="brain:u1", user="u1")

    assert client.calls[0]["url"].endswith("/api/v1/forget")
    assert result["user"] == "u1"
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_no_api_key_omits_auth_header():
    client = _FakeClient()
    backend = CogneeHttpMemoryBackend(api_key="", client=client)
    await backend.forget_scope(dataset="d")
    assert "X-Api-Key" not in client.calls[0]["headers"]


@pytest.mark.asyncio
async def test_recall_missing_dataset_4xx_returns_empty():
    # A dataset that doesn't exist yet (e.g. right after a forget) is a 4xx, which
    # must degrade to "nothing here yet" rather than raising.
    client = _FakeClient(status_code=404)
    backend = CogneeHttpMemoryBackend(client=client)
    assert await backend.recall("q", dataset="d", session="s", top_k=5) == []


@pytest.mark.asyncio
async def test_recall_5xx_raises():
    client = _FakeClient(status_code=500)
    backend = CogneeHttpMemoryBackend(client=client)
    with pytest.raises(RuntimeError):
        await backend.recall("q", dataset="d", session="s", top_k=5)


@pytest.mark.asyncio
async def test_forget_missing_dataset_4xx_is_noop():
    client = _FakeClient(status_code=404)
    backend = CogneeHttpMemoryBackend(client=client)
    result = await backend.forget_scope(dataset="d")  # must not raise
    assert result["dataset"] == "d"
