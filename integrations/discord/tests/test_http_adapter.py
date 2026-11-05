import asyncio
import json

import httpx
from cognee_integration_discord.adapter import CogneeHttpAdapter


def run(coro):
    return asyncio.run(coro)


def _adapter(handler):
    """CogneeHttpAdapter wired to an httpx MockTransport (no real server)."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = CogneeHttpAdapter("http://cognee:8000", api_key="test-key", client=client)
    return adapter, client


def test_recall_posts_json_to_recall_endpoint_and_scopes_dataset():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json=[{"text": "Deploy is at 5pm. https://discord.com/channels/1/2/3"}],
        )

    adapter, client = _adapter(handler)
    try:
        result = run(
            adapter.recall("when is deploy", dataset="discord-guild-1", session="s", top_k=3)
        )
    finally:
        run(client.aclose())

    assert captured["url"].endswith("/api/v1/recall")
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["body"]["datasets"] == ["discord-guild-1"]
    assert captured["body"]["session_id"] == "s"
    assert captured["body"]["top_k"] == 3
    assert result.answer.startswith("Deploy is at 5pm.")
    assert "https://discord.com/channels/1/2/3" in result.sources[0]


def test_recall_normalizes_search_result_field():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"search_result": ["line one", "line two"]}])

    adapter, client = _adapter(handler)
    try:
        result = run(adapter.recall("q", dataset="d", session="s"))
    finally:
        run(client.aclose())

    assert result.answer == "line one\nline two"


def test_remember_posts_multipart_to_remember_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["content_type"] = request.headers.get("content-type", "")
        captured["body"] = request.content
        return httpx.Response(200, json={"status": "ok"})

    adapter, client = _adapter(handler)
    try:
        run(adapter.remember("hello world", dataset="discord-guild-1", session="s"))
    finally:
        run(client.aclose())

    assert captured["url"].endswith("/api/v1/remember")
    assert captured["content_type"].startswith("multipart/form-data")
    assert b"discord-guild-1" in captured["body"]
    assert b"hello world" in captured["body"]


def test_forget_posts_dataset_to_forget_endpoint():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"status": "ok"})

    adapter, client = _adapter(handler)
    try:
        run(adapter.forget(dataset="discord-guild-1"))
    finally:
        run(client.aclose())

    assert captured["url"].endswith("/api/v1/forget")
    assert captured["body"] == {"dataset": "discord-guild-1", "everything": False}


def test_http_error_raises():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    adapter, client = _adapter(handler)
    try:
        raised = False
        try:
            run(adapter.forget(dataset="d"))
        except httpx.HTTPStatusError:
            raised = True
    finally:
        run(client.aclose())

    assert raised
