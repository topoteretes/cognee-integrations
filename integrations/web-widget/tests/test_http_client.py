"""Wire-contract tests for CogneeHttpClient over a stub transport.

Keyless and network-free: an injected fake ``httpx``-style client records the
requests and returns canned responses, so we assert the exact HTTP calls the
widget makes and that a missing dataset (4xx) degrades to "nothing here yet"
while a failing backend (5xx) surfaces.
"""

import pytest
from cognee_integration_web_widget.http_client import CogneeHttpClient


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

    await client.remember("hello docs", dataset_name="web:acme:docs")

    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "http://x:8000/api/v1/remember"
    assert call["headers"]["X-Api-Key"] == "k"
    assert call["data"] == {"datasetName": "web:acme:docs"}
    _, content, _ = call["files"]["data"]
    assert content == b"hello docs"


async def test_remember_includes_session_id_when_set():
    fake = _FakeClient(_FakeResponse())
    client = CogneeHttpClient(client=fake)

    await client.remember("hi", dataset_name="d", session_id="web:demo:v1:c1")

    assert fake.calls[0]["data"] == {"datasetName": "d", "session_id": "web:demo:v1:c1"}


async def test_recall_returns_results_and_sends_expected_body():
    fake = _FakeClient(_FakeResponse([{"text": "answer", "source": "graph"}]))
    client = CogneeHttpClient(client=fake)

    results = await client.recall("q", datasets=["web:demo:docs"], session_id="s", top_k=5)

    assert results == [{"text": "answer", "source": "graph"}]
    body = fake.calls[0]["json"]
    assert body["query"] == "q"
    assert body["datasets"] == ["web:demo:docs"]
    assert body["session_id"] == "s"
    assert body["top_k"] == 5
    assert body["include_references"] is True
    assert body["search_type"] == "GRAPH_COMPLETION"
    assert body["scope"] == ["graph", "code"]
    assert "system_prompt" not in body


async def test_recall_unwraps_results_key():
    fake = _FakeClient(_FakeResponse({"results": [{"text": "x"}]}))
    client = CogneeHttpClient(client=fake)
    assert await client.recall("q") == [{"text": "x"}]


async def test_recall_missing_dataset_4xx_returns_empty():
    fake = _FakeClient(_FakeResponse({"detail": "No datasets found."}, status_code=404))
    client = CogneeHttpClient(client=fake)
    assert await client.recall("q", datasets=["d"]) == []


async def test_recall_5xx_raises():
    fake = _FakeClient(_FakeResponse("boom", status_code=500))
    client = CogneeHttpClient(client=fake)
    with pytest.raises(RuntimeError):
        await client.recall("q")


async def test_forget_posts_dataset():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="web:demo:v1:c1")
    assert fake.calls[0]["json"] == {"dataset": "web:demo:v1:c1", "everything": False}


async def test_forget_missing_dataset_4xx_is_noop():
    fake = _FakeClient(_FakeResponse({"detail": "not found"}, status_code=404))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="d")  # must not raise


async def test_forget_is_best_effort_and_swallows_5xx():
    # cognee returns a 500 (internal AttributeError) when forgetting a dataset
    # that was never created (a session-only conversation). Forget is idempotent
    # and best-effort, so that is logged and swallowed, not raised.
    fake = _FakeClient(_FakeResponse("boom", status_code=500))
    client = CogneeHttpClient(client=fake)
    await client.forget(dataset_name="d")  # must not raise
    assert fake.calls[0]["json"] == {"dataset": "d", "everything": False}


async def test_no_api_key_omits_auth_header():
    fake = _FakeClient(_FakeResponse({"status": "ok"}))
    client = CogneeHttpClient(api_key="", client=fake)
    await client.forget(dataset_name="d")
    assert "X-Api-Key" not in fake.calls[0]["headers"]


class _PagedClient:
    """Serves ``items`` the way cognee's dataset-data endpoint does: a page of
    ``limit`` from ``offset``, or the same first page when ``ignore_offset``."""

    def __init__(self, items, fail_at_offset=None, ignore_offset=False):
        self.calls = []
        self._items = items
        self._fail_at_offset = fail_at_offset
        self._ignore_offset = ignore_offset

    async def request(self, method, url, headers=None, params=None, **kwargs):
        self.calls.append({"method": method, "url": url, "params": params})
        limit, offset = params["limit"], params["offset"]
        if offset == self._fail_at_offset:
            return _FakeResponse({"detail": "boom"}, status_code=500)
        start = 0 if self._ignore_offset else offset
        return _FakeResponse(self._items[start : start + limit])


def _items(n):
    return [{"id": f"id-{i}", "name": f"page-{i}"} for i in range(n)]


async def test_dataset_data_pages_past_the_first_page(monkeypatch):
    monkeypatch.setattr("cognee_integration_web_widget.http_client.DATASET_PAGE_SIZE", 100)
    fake = _PagedClient(_items(286))
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    items = await client.dataset_data("ds-1")

    assert [i["id"] for i in items] == [f"id-{i}" for i in range(286)]
    assert [c["params"] for c in fake.calls] == [
        {"limit": 100, "offset": 0},
        {"limit": 100, "offset": 100},
        {"limit": 100, "offset": 200},
    ]
    assert fake.calls[0]["url"] == "http://x:8000/api/v1/datasets/ds-1/data"


async def test_dataset_data_exact_multiple_ends_on_an_empty_page(monkeypatch):
    monkeypatch.setattr("cognee_integration_web_widget.http_client.DATASET_PAGE_SIZE", 100)
    fake = _PagedClient(_items(200))
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    assert len(await client.dataset_data("ds-1")) == 200
    assert [c["params"]["offset"] for c in fake.calls] == [0, 100, 200]


async def test_dataset_data_missing_dataset_returns_empty():
    fake = _FakeClient(_FakeResponse({"detail": "not found"}, status_code=404))
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    assert await client.dataset_data("ds-missing") == []


async def test_dataset_data_failure_after_the_first_page_raises(monkeypatch):
    monkeypatch.setattr("cognee_integration_web_widget.http_client.DATASET_PAGE_SIZE", 100)
    fake = _PagedClient(_items(286), fail_at_offset=100)
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    with pytest.raises(RuntimeError, match="offset 100"):
        await client.dataset_data("ds-1")


async def test_dataset_data_server_ignoring_offset_does_not_loop(monkeypatch):
    monkeypatch.setattr("cognee_integration_web_widget.http_client.DATASET_PAGE_SIZE", 100)
    fake = _PagedClient(_items(150), ignore_offset=True)
    client = CogneeHttpClient("http://x:8000", "k", client=fake)

    items = await client.dataset_data("ds-1")

    assert len(items) == 100
    assert len(fake.calls) == 2


def test_recall_scope_env_override(monkeypatch):
    import importlib

    import cognee_integration_web_widget.http_client as http_client

    monkeypatch.setenv("WIDGET_RECALL_SCOPE", "graph")
    try:
        assert importlib.reload(http_client).RECALL_SCOPE == ("graph",)
    finally:
        monkeypatch.delenv("WIDGET_RECALL_SCOPE")
        importlib.reload(http_client)
    assert http_client.RECALL_SCOPE == ("graph", "code")


def test_recall_search_type_and_system_prompt_overrides(monkeypatch):
    import importlib

    import cognee_integration_web_widget.http_client as http_client

    monkeypatch.setenv("WIDGET_SEARCH_TYPE", "")
    monkeypatch.setenv("WIDGET_SYSTEM_PROMPT", "Only use the context.")
    try:
        module = importlib.reload(http_client)
        assert module.RECALL_SEARCH_TYPE is None
        assert module.RECALL_SYSTEM_PROMPT == "Only use the context."
    finally:
        monkeypatch.delenv("WIDGET_SEARCH_TYPE")
        monkeypatch.delenv("WIDGET_SYSTEM_PROMPT")
        importlib.reload(http_client)
    assert http_client.RECALL_SEARCH_TYPE == "GRAPH_COMPLETION"
    assert http_client.RECALL_SYSTEM_PROMPT is None


async def test_recall_omits_search_type_when_auto(monkeypatch):
    import cognee_integration_web_widget.http_client as http_client

    monkeypatch.setattr(http_client, "RECALL_SEARCH_TYPE", None)
    monkeypatch.setattr(http_client, "RECALL_SYSTEM_PROMPT", "Only use the context.")
    fake = _FakeClient(_FakeResponse([]))
    await http_client.CogneeHttpClient(client=fake).recall("q")
    body = fake.calls[0]["json"]
    assert "search_type" not in body
    assert body["system_prompt"] == "Only use the context."
