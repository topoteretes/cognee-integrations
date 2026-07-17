"""Wire-contract tests for CogneeHttpClient over a stub transport.

Keyless and network-free: an injected fake ``httpx``-style client records the
requests and returns canned responses, so we assert the exact HTTP calls the bot
makes and that a missing dataset (4xx) degrades to "nothing here yet" while a
failing backend (5xx) surfaces.
"""

import pytest
from cognee_integration_telegram.http_client import CogneeHttpClient


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


async def test_remember_posts_multipart_with_dataset_and_key():
    fake = _FakeClient(_FakeResponse())
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    await client.remember("Ada: hello", dataset_name="telegram_dm_7")

    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://x:8000/api/v1/remember"
    assert call["headers"]["X-Api-Key"] == "k"
    assert call["data"] == {"datasetName": "telegram_dm_7"}
    _, content, _ = call["files"]["data"]
    assert content == b"Ada: hello"


async def test_recall_returns_results_and_sends_expected_body():
    fake = _FakeClient(_FakeResponse([{"text": "answer", "source": "graph"}]))
    client = CogneeHttpClient(client=fake)

    results = await client.recall("q", dataset_name="telegram_dm_7")

    assert results == [{"text": "answer", "source": "graph"}]
    body = fake.calls[0]["json"]
    assert body["datasets"] == ["telegram_dm_7"]
    assert body["include_references"] is True
    assert body["search_type"] == "GRAPH_COMPLETION"


async def test_recall_unwraps_results_key():
    fake = _FakeClient(_FakeResponse({"results": [{"text": "x"}]}))
    client = CogneeHttpClient(client=fake)
    assert await client.recall("q", dataset_name="d") == [{"text": "x"}]


async def test_recall_missing_dataset_4xx_returns_empty():
    fake = _FakeClient(_FakeResponse({"detail": "No datasets found."}, status_code=404))
    client = CogneeHttpClient(client=fake)
    assert await client.recall("q", dataset_name="d") == []


async def test_recall_5xx_raises():
    fake = _FakeClient(_FakeResponse("boom", status_code=500))
    client = CogneeHttpClient(client=fake)
    with pytest.raises(RuntimeError):
        await client.recall("q", dataset_name="d")


async def test_forget_posts_dataset():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="telegram_dm_7")
    body = fake.calls[0]["json"]
    assert body == {"dataset": "telegram_dm_7", "everything": False}


async def test_forget_missing_dataset_4xx_is_noop():
    fake = _FakeClient(_FakeResponse({"detail": "not found"}, status_code=404))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="d")  # must not raise


async def test_forget_5xx_raises():
    fake = _FakeClient(_FakeResponse("boom", status_code=500))
    client = CogneeHttpClient(client=fake)
    with pytest.raises(RuntimeError):
        await client.forget(dataset_name="d")


async def test_no_api_key_omits_auth_header():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(api_key="", client=fake)
    await client.forget(dataset_name="d")
    assert "X-Api-Key" not in fake.calls[0]["headers"]
