"""Shared fakes: a scripted cognee 1.6.5 over httpx.MockTransport, and a fake Agent."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Optional

import httpx
import pytest
from cognee_integration_browser_use import (
    CogneeBrowserMemory,
    CogneeHttpBackend,
    ReceiptStore,
    resolve_config,
)

FIXTURES = Path(__file__).parent / "contract" / "fixtures"
API_KEY = "ck_test_key_0123456789"
BASE_URL = "http://cognee.test:8000"
DATASET_ID = "35c36577-01aa-5707-929c-0fee137c9031"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text())


class FakeCognee:
    """In-memory stand-in for cognee 1.6.5 wired as an httpx MockTransport."""

    def __init__(self, overrides: Optional[dict[str, Callable[..., httpx.Response]]] = None):
        self.calls: list[dict[str, Any]] = []
        self.datasets: list[dict[str, str]] = [{"id": DATASET_ID, "name": "browser_use_probe"}]
        self.data: list[dict[str, Any]] = []
        self.pipeline = "DATASET_PROCESSING_COMPLETED"
        self.recall: Any = fixture("recall-chunks.json")
        self.overrides = overrides or {}
        self.transport = httpx.MockTransport(self.handle)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method
        body: Any = None
        content = request.read()
        if request.headers.get("content-type", "").startswith("application/json") and content:
            body = json.loads(content)
        self.calls.append(
            {
                "method": method,
                "path": path,
                "query": dict(request.url.params),
                "body": body,
                "raw": content,
                "headers": dict(request.headers),
            }
        )
        key = f"{method} {path}"
        if key in self.overrides:
            return self.overrides[key](request=request, body=body, state=self)
        if path != "/health" and request.headers.get("x-api-key") != API_KEY:
            return httpx.Response(401, json=fixture("unauthorized.json")["body"])
        if key == "GET /health":
            return httpx.Response(
                200, json={"status": "ready", "health": "healthy", "version": "1.6.5"}
            )
        if key == "GET /api/v1/datasets":
            return httpx.Response(200, json=self.datasets)
        if key == "POST /api/v1/datasets":
            existing = next((d for d in self.datasets if d["name"] == body["name"]), None)
            if existing:
                return httpx.Response(200, json=existing)
            created = {
                "id": f"11111111-2222-4333-8444-{len(self.datasets):012d}",
                "name": body["name"],
            }
            self.datasets.append(created)
            return httpx.Response(200, json=created)
        if key == "POST /api/v1/remember":
            raw = content.decode("utf-8", errors="replace")
            name = _multipart_filename(raw)
            dataset_name = _multipart_field(raw, "datasetName")
            ds = next((d for d in self.datasets if d["name"] == dataset_name), None)
            if ds is None:
                ds = {
                    "id": f"22222222-2222-4333-8444-{len(self.datasets):012d}",
                    "name": dataset_name,
                }
                self.datasets.append(ds)
            if not any(d["name"] == name for d in self.data):
                self.data.append(
                    {
                        "id": f"d03f564c-ff7b-4810-ac63-{len(self.data):012d}",
                        "name": name,
                        "datasetId": ds["id"],
                        "text": _multipart_file_text(raw),
                    }
                )
            payload = fixture("remember-background.json")
            payload.update({"dataset_id": ds["id"], "dataset_name": dataset_name})
            return httpx.Response(200, json=payload)
        if key == "POST /api/v1/recall":
            missing = [
                n for n in body["datasets"] if not any(d["name"] == n for d in self.datasets)
            ]
            if missing:
                return httpx.Response(
                    404,
                    json={
                        "detail": f"Dataset(s) not found: '{missing[0]}'. [DatasetNotFoundError]"
                    },
                )
            return httpx.Response(
                200, json=self.recall(body) if callable(self.recall) else self.recall
            )
        if key == "GET /api/v1/datasets/status":
            return httpx.Response(200, json={request.url.params.get("dataset"): self.pipeline})
        if key == "POST /api/v1/forget":
            self.data = [d for d in self.data if d["id"] != body["dataId"]]
            return httpx.Response(
                200,
                json={
                    "data_id": body["dataId"],
                    "dataset_id": body["datasetId"],
                    "status": "success",
                },
            )
        if method == "GET" and path.startswith("/api/v1/datasets/") and path.endswith("/data"):
            dataset_id = path.split("/")[4]
            limit = int(request.url.params.get("limit", 100))
            offset = int(request.url.params.get("offset", 0))
            rows = [
                {k: v for k, v in d.items() if k != "text"}
                for d in self.data
                if d["datasetId"] == dataset_id
            ]
            return httpx.Response(200, json=rows[offset : offset + limit])
        return httpx.Response(404, json={"detail": "not found"})


def _multipart_filename(raw: str) -> str:
    marker = 'filename="'
    start = raw.find(marker)
    if start < 0:
        return ""
    end = raw.find('"', start + len(marker))
    name = raw[start + len(marker) : end]
    return name[:-4] if name.endswith(".txt") else name


def _multipart_field(raw: str, field: str) -> str:
    marker = f'name="{field}"'
    start = raw.find(marker)
    if start < 0:
        return ""
    chunk = raw[start:].split("\r\n\r\n", 1)[1]
    return chunk.split("\r\n", 1)[0]


def _multipart_file_text(raw: str) -> str:
    marker = 'name="data"'
    start = raw.find(marker)
    if start < 0:
        return ""
    chunk = raw[start:].split("\r\n\r\n", 1)[1]
    return chunk.split("\r\n--", 1)[0]


@pytest.fixture
def fake() -> FakeCognee:
    return FakeCognee()


def make_memory(fake: FakeCognee, **overrides: Any) -> CogneeBrowserMemory:
    config = resolve_config({}, base_url=BASE_URL, api_key=API_KEY, **overrides)
    backend = CogneeHttpBackend(
        base_url=BASE_URL, api_key=API_KEY, request_timeout_s=2.0, transport=fake.transport
    )
    return CogneeBrowserMemory(config, backend=backend, store=ReceiptStore(None))


class FakeHistory:
    def __init__(
        self,
        *,
        final: Optional[str] = "Supplier A ships in 3 days.",
        done: bool = True,
        success: Optional[bool] = True,
        steps: int = 4,
        urls: Optional[list[str]] = None,
        names: Optional[list[str]] = None,
        contents: Optional[list[str]] = None,
    ):
        self._final, self._done, self._success, self._steps = final, done, success, steps
        self._urls = (
            urls
            if urls is not None
            else [
                "https://supplier-a.example/catalog?token=secret123",
                "https://supplier-a.example/catalog",
            ]
        )
        self._names = names or []
        self._contents = contents or []

    def final_result(self):
        return self._final

    def is_done(self):
        return self._done

    def is_successful(self):
        return self._success

    def number_of_steps(self):
        return self._steps

    def urls(self):
        return list(self._urls)

    def action_names(self):
        return list(self._names)

    def extracted_content(self):
        return list(self._contents)


class FakeAgent:
    """Mimics ``Agent.run(max_steps, on_step_start, on_step_end)`` with no browser or LLM."""

    def __init__(
        self,
        *,
        steps: int = 3,
        history: Optional[FakeHistory] = None,
        fail_with: Optional[BaseException] = None,
        hang: bool = False,
        tools: Any = None,
    ):
        self.steps = steps
        self.history = history or FakeHistory()
        self.fail_with = fail_with
        self.hang = hang
        self.tools = tools
        self.state = type("State", (), {"n_steps": 0})()
        self.browser_session = type("Session", (), {})()
        self.browser_session.get_current_page_url = self._url
        self.events: list[str] = []

    async def _url(self):
        return f"https://supplier-a.example/page/{self.state.n_steps}?sig=abc"

    async def run(self, max_steps: int = 100, on_step_start=None, on_step_end=None):
        for i in range(min(self.steps, max_steps)):
            if on_step_start:
                await on_step_start(self)
            self.events.append(f"step{i}")
            self.state.n_steps = i + 1
            if self.hang:
                await asyncio.sleep(3600)
            if on_step_end:
                await on_step_end(self)
            if self.fail_with is not None and i == 1:
                raise self.fail_with
        return self.history
