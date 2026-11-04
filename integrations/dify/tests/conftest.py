"""Shared test scaffolding: plugin import path and a strict fake Cognee server."""

import json
import sys
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

PLUGIN_ROOT = Path(__file__).resolve().parent.parent
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))


@pytest.fixture
def plugin_root() -> Path:
    return PLUGIN_ROOT


class FakeCognee:
    """Routes requests to handlers keyed by ``"METHOD /path"``; anything else fails the test.

    Handlers receive the ``httpx.Request`` and return an ``httpx.Response`` (or a
    ``(status, json_body)`` tuple). Every request is recorded in ``requests``.
    """

    def __init__(self, real_client=httpx.Client):
        self.routes: dict[str, Callable] = {}
        self.requests: list[httpx.Request] = []
        self._real_client = real_client

    def on(self, method: str, path: str, handler=None, *, status: int = 200, json_body=None):
        if handler is None:

            def handler(_request):
                return status, json_body

        self.routes[f"{method.upper()} {path}"] = handler
        return self

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        key = f"{request.method} {request.url.path}"
        if key not in self.routes:
            raise AssertionError(f"unexpected request {key} (known: {sorted(self.routes)})")
        result = self.routes[key](request)
        if isinstance(result, httpx.Response):
            return result
        status, body = result
        return httpx.Response(status, json=body) if body is not None else httpx.Response(status)

    def calls(self, method: str, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.method == method.upper() and r.url.path == path]

    def client(self, **kwargs) -> httpx.Client:
        kwargs.setdefault("follow_redirects", True)
        return self._real_client(transport=httpx.MockTransport(self.handle), **kwargs)


@pytest.fixture
def fake_cognee(monkeypatch) -> FakeCognee:
    """A fake server behind every client the plugin builds.

    ``make_client`` is imported by name into each tool module, so the patch
    goes one level down, on the ``httpx.Client`` class the factory calls.
    """
    server = FakeCognee(real_client=httpx.Client)
    monkeypatch.setattr(httpx, "Client", server.client)
    return server


def login_ok(_request):
    return 200, {"access_token": "tok"}


def form_fields(request: httpx.Request) -> dict[str, list[str]]:
    """Decode a urlencoded or multipart request body into field -> values."""
    body = request.content.decode("utf-8", errors="replace")
    content_type = request.headers.get("content-type", "")
    fields: dict[str, list[str]] = {}
    if content_type.startswith("multipart/form-data"):
        boundary = content_type.split("boundary=", 1)[1]
        for part in body.split("--" + boundary)[1:-1]:
            header, _, value = part.partition("\r\n\r\n")
            name = header.split('name="', 1)[1].split('"', 1)[0]
            fields.setdefault(name, []).append(value.rstrip("\r\n"))
        return fields
    for pair in body.split("&"):
        if not pair:
            continue
        name, _, value = pair.partition("=")
        fields.setdefault(name, []).append(httpx.URL(f"http://x/?{pair}").params.get(name, value))
    return fields


def json_body(request: httpx.Request):
    return json.loads(request.content or b"null")


class Runtime:
    """Stand-in for a tool's runtime; carries the provider credentials."""

    def __init__(self, **credentials):
        self.credentials = {
            "base_url": "http://cognee.test",
            "api_key": "key-1",
            "user_email": "",
            "user_password": "",
            **credentials,
        }


def invoke(tool_cls, runtime: Runtime, params: dict):
    """Run a tool's ``_invoke`` without the Dify plugin runtime; returns its messages."""
    from dify_plugin.entities.tool import ToolInvokeMessage

    tool = tool_cls.__new__(tool_cls)
    tool.runtime = runtime
    tool.response_type = ToolInvokeMessage
    return list(tool._invoke(params))


def variables(messages) -> dict:
    from dify_plugin.entities.tool import ToolInvokeMessage

    return {
        m.message.variable_name: m.message.variable_value
        for m in messages
        if m.type == ToolInvokeMessage.MessageType.VARIABLE
    }


def text_of(messages) -> str:
    from dify_plugin.entities.tool import ToolInvokeMessage

    return "\n".join(
        m.message.text for m in messages if m.type == ToolInvokeMessage.MessageType.TEXT
    )
