"""Adapter contract tests: fake end to end, HTTP against a recorded fake server."""

import pytest
from cognee_backend_core import FakeAdapter, HttpCogneeAdapter


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.status_code < 400, f"HTTP {self.status_code}"


class RecordingClient:
    """Plays a canned response and records the request for assertions."""

    def __init__(self, response: FakeResponse):
        self.response = response
        self.requests = []

    async def request(self, method, url, **kwargs):
        self.requests.append({"method": method, "url": url, **kwargs})
        return self.response


async def test_fake_adapter_roundtrip(tmp_path):
    doc = tmp_path / "note.md"
    doc.write_text("Deploy checklist: run migrations before restarting.")
    adapter = FakeAdapter()
    await adapter.add([str(doc)])
    await adapter.cognify()
    hits = await adapter.chunks("migrations")
    assert hits and hits[0]["file_path"] == str(doc)
    answer = await adapter.answer("what comes before restarting?")
    assert "migrations" in answer


async def test_http_search_unwraps_envelopes():
    payload = [{"dataset_name": "d", "search_result": [{"text": "hit", "document_name": "a.md"}]}]
    client = RecordingClient(FakeResponse(200, payload))
    adapter = HttpCogneeAdapter("d", "http://server.test", "key", client=client)
    chunks = await adapter.chunks("query")
    assert chunks == [{"text": "hit", "document_name": "a.md"}]
    request = client.requests[0]
    assert request["url"] == "http://server.test/api/v1/search"
    assert request["json"]["searchType"] == "CHUNKS"
    assert request["headers"]["X-Api-Key"] == "key"


async def test_http_missing_dataset_is_empty_not_error():
    adapter = HttpCogneeAdapter(
        "d", "http://server.test", client=RecordingClient(FakeResponse(404, {}))
    )
    assert await adapter.chunks("q") == []
    assert await adapter.recall("q") == []


async def test_http_remember_posts_multipart_with_node_set():
    client = RecordingClient(FakeResponse(200, {"ok": True}))
    adapter = HttpCogneeAdapter("brain", "http://server.test", client=client)
    await adapter.remember("a note", node_set="handover")
    request = client.requests[0]
    assert request["url"] == "http://server.test/api/v1/remember"
    assert request["data"]["datasetName"] == "brain"
    assert request["data"]["node_set"] == "handover"
    assert request["data"]["run_in_background"] == "true"
    field, (filename, body, mime) = request["files"][0]
    assert field == "data" and body == b"a note"


async def test_http_server_error_propagates():
    adapter = HttpCogneeAdapter(
        "d", "http://server.test", client=RecordingClient(FakeResponse(500, {}))
    )
    with pytest.raises(AssertionError):
        await adapter.chunks("q")


def test_single_user_runtime_env(tmp_path, monkeypatch):
    from cognee_backend_core import single_user_runtime

    for key in ("CACHING", "ENABLE_BACKEND_ACCESS_CONTROL", "DATA_ROOT_DIRECTORY"):
        monkeypatch.delenv(key, raising=False)
    env = single_user_runtime(tmp_path / "store")
    import os

    assert os.environ["CACHING"] == "false"
    assert os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] == "false"
    assert env["SYSTEM_ROOT_DIRECTORY"].endswith("store/system")


async def test_http_search_all_scopes_chunks_but_not_answers():
    payload = [{"dataset_name": "any", "search_result": []}]
    client = RecordingClient(FakeResponse(200, payload))
    adapter = HttpCogneeAdapter("d", "http://server.test", search_all=True, client=client)
    await adapter.chunks("query")  # keystroke path: always dataset-scoped
    assert client.requests[0]["json"]["datasets"] == ["d"]
    await adapter.answer("query")  # answers span the tenant
    assert "datasets" not in client.requests[1]["json"]


async def test_answer_with_sources_attributes_contributing_datasets():
    payload = [
        {
            "dataset_name": "handover-inbox-vasilije",
            "search_result": ["I'm sorry, the graph contains only technical entities."],
        },
        {
            "dataset_name": "spotlight",
            "search_result": [
                "**Main competitors** - StayFinder leads the threat tiers in the north region."
            ],
        },
    ]
    adapter = HttpCogneeAdapter(
        "d",
        "http://server.test",
        search_all=True,
        client=RecordingClient(FakeResponse(200, payload)),
    )
    meta = await adapter.answer_with_sources("who are our competitors")
    assert meta["answer"].startswith("**Main competitors**")
    assert meta["sources"] == ["spotlight"]  # the refusal dataset is not a source
