"""Wire-contract tests for CogneeHttpClient over a stub transport (keyless)."""

import pytest
from cognee_integration_slack.http_client import CogneeHttpClient


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
    def __init__(self, response):
        self.calls = []
        self._response = response

    async def request(self, method, url, headers=None, **kwargs):
        self.calls.append({"method": method, "url": url, "headers": headers or {}, **kwargs})
        return self._response


async def test_add_posts_multipart_with_dataset_and_node_set():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    await client.add("hello", dataset_name="slack_C42", node_set=["C42"])

    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://x:8000/api/v1/add"
    assert call["headers"]["X-Api-Key"] == "k"
    assert call["data"] == {"datasetName": "slack_C42", "node_set": ["C42"]}
    _, content, _ = call["files"]["data"]
    assert content == b"hello"


async def test_cognify_posts_datasets():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(client=fake)
    await client.cognify(dataset_name="slack_C42")
    assert fake.calls[0]["url"].endswith("/api/v1/cognify")
    assert fake.calls[0]["json"] == {"datasets": ["slack_C42"]}


async def test_search_sends_type_scope_and_returns_list():
    fake = _FakeClient(_FakeResponse([{"text": "answer"}]))
    client = CogneeHttpClient(client=fake)
    results = await client.search(
        "q", search_type="CHUNKS", dataset_name="slack_C42", node_name=["C42"], top_k=5
    )
    assert results == [{"text": "answer"}]
    body = fake.calls[0]["json"]
    assert body["search_type"] == "CHUNKS"
    assert body["datasets"] == ["slack_C42"]
    assert body["node_name"] == ["C42"]
    assert body["top_k"] == 5


async def test_search_unwraps_results_key():
    fake = _FakeClient(_FakeResponse({"results": [{"text": "x"}]}))
    client = CogneeHttpClient(client=fake)
    assert await client.search("q", search_type="CHUNKS", dataset_name="d", top_k=5) == [
        {"text": "x"}
    ]


async def test_search_missing_dataset_4xx_returns_empty():
    fake = _FakeClient(_FakeResponse({"detail": "No datasets found."}, status_code=404))
    client = CogneeHttpClient(client=fake)
    assert await client.search("q", search_type="CHUNKS", dataset_name="d", top_k=5) == []


async def test_search_5xx_raises():
    fake = _FakeClient(_FakeResponse("boom", status_code=500))
    client = CogneeHttpClient(client=fake)
    with pytest.raises(RuntimeError):
        await client.search("q", search_type="CHUNKS", dataset_name="d", top_k=5)


async def test_forget_posts_dataset_and_4xx_is_noop():
    fake = _FakeClient(_FakeResponse({"detail": "not found"}, status_code=404))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="slack_C42")  # must not raise
    assert fake.calls[0]["json"] == {"dataset": "slack_C42", "everything": False}


async def test_no_api_key_omits_auth_header():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(api_key="", client=fake)
    await client.cognify(dataset_name="d")
    assert "X-Api-Key" not in fake.calls[0]["headers"]
