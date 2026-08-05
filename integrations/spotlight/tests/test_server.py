"""End-to-end backend tests over the HTTP surface, with the fake adapter.

This is the same wiring the macOS app hits, minus cognee: index a real temp
folder, poll status, search by filename and by content, ask for an answer.
"""

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from spotlight_backend.adapters import FakeAdapter
from spotlight_backend.config import Settings
from spotlight_backend.server import create_app


@pytest.fixture
def workspace(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text("Ship the cognee spotlight integration in Q3.")
    (docs / "recipes.txt").write_text("Pasta carbonara: eggs, pecorino, guanciale.")
    (docs / "huge.md").write_text("x")
    (docs / "skipped.bin").write_text("binary-ish")
    return docs


@pytest.fixture
async def client(workspace, tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=FakeAdapter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def index_and_wait(client, path):
    response = await client.post("/index", json={"paths": [str(path)]})
    assert response.status_code == 202
    assert response.json()["started"] is True
    status = {}
    for _ in range(100):
        status = (await client.get("/index/status")).json()
        if status["state"] in ("idle", "error"):
            break
        await asyncio.sleep(0.01)
    assert status["state"] == "idle", status
    return status


async def test_health(client):
    data = (await client.get("/health")).json()
    assert data["status"] == "ok"
    assert data["mode"] == "fake"


async def test_index_then_filename_search(client, workspace):
    status = await index_and_wait(client, workspace)
    assert status["indexed_files"] == 3  # .bin filtered out

    data = (await client.get("/search", params={"q": "roadmap"})).json()
    top = data["results"][0]
    assert top["title"] == "roadmap.md"
    assert top["source"] == "filename"
    # content hit on the same file enriches, not duplicates
    assert sum(r["path"] == top["path"] for r in data["results"]) == 1


async def test_semantic_content_search(client, workspace):
    await index_and_wait(client, workspace)
    data = (await client.get("/search", params={"q": "carbonara"})).json()
    paths = [r["path"] for r in data["results"]]
    assert str(workspace / "recipes.txt") in paths
    hit = next(r for r in data["results"] if r["path"].endswith("recipes.txt"))
    assert "pecorino" in hit["snippet"] or hit["source"] == "filename"


async def test_answer_mode(client, workspace):
    await index_and_wait(client, workspace)
    data = (await client.get("/search", params={"q": "carbonara", "mode": "answer"})).json()
    assert data["answer"] and "recipes.txt" in data["answer"]


async def test_empty_query(client):
    data = (await client.get("/search", params={"q": "  "})).json()
    assert data["results"] == []


async def test_incremental_reindex(client, workspace):
    await index_and_wait(client, workspace)
    # second run with no changes: nothing new to add
    status = await index_and_wait(client, workspace)
    assert status["total"] == 0


async def test_answer_mode_multiword_question(client, workspace):
    await index_and_wait(client, workspace)
    params = {"q": "what goes in carbonara", "mode": "answer"}
    data = (await client.get("/search", params=params)).json()
    assert data["answer"] and "recipes.txt" in data["answer"]


class FlakyAdapter(FakeAdapter):
    """Rejects one specific file, like cognee rejecting a .docx without its loader."""

    async def add(self, paths):
        if any(p.endswith("huge.md") for p in paths):
            raise ValueError("No loader found for file 'huge.md'")
        await super().add(paths)


async def test_unsupported_file_skipped_not_fatal(workspace, tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=FlakyAdapter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        status = await index_and_wait(client, workspace)
        assert status["skipped"] == 1
        assert "huge.md" in status["last_skip"]
        assert status["indexed_files"] == 2  # the rest of the batch still landed
        data = (await client.get("/search", params={"q": "carbonara"})).json()
        assert any(r["path"].endswith("recipes.txt") for r in data["results"])


class StalledAdapter(FakeAdapter):
    """Semantic search that never returns, like cognee's stores mid-cognify."""

    async def chunks(self, query, top_k=8):
        await asyncio.sleep(30)
        return []


async def test_filename_results_survive_stalled_semantic_search(workspace, tmp_path, monkeypatch):
    from spotlight_backend import server as server_module

    monkeypatch.setattr(server_module, "SEMANTIC_TIMEOUT_SECONDS", 0.1)
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=StalledAdapter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await index_and_wait(client, workspace)
        data = (await client.get("/search", params={"q": "roadmap"})).json()
        assert data["results"], "filename hits must not wait on semantic search"
        assert data["results"][0]["source"] == "filename"


class CognifyFailsOnceAdapter(FakeAdapter):
    """cognify dies on the first run (crash / bad loader), then recovers."""

    def __init__(self):
        super().__init__()
        self.cognify_calls = 0

    async def cognify(self):
        self.cognify_calls += 1
        if self.cognify_calls == 1:
            raise RuntimeError("boom")
        await super().cognify()


async def test_interrupted_cognify_reruns_without_new_files(workspace, tmp_path):
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    adapter = CognifyFailsOnceAdapter()
    app = create_app(settings, adapter=adapter)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/index", json={"paths": [str(workspace)]})
        assert response.status_code == 202
        status = {}
        for _ in range(100):
            status = (await client.get("/index/status")).json()
            if status["state"] in ("idle", "error"):
                break
            await asyncio.sleep(0.01)
        assert status["state"] == "error"

        # no new files, but the pending marker forces cognify to run again
        status = await index_and_wait(client, workspace)
        assert status["total"] == 0
        assert adapter.cognify_calls == 2
        assert adapter.cognified


async def test_semantic_zero_skips_chunks(client, workspace):
    await index_and_wait(client, workspace)
    data = (await client.get("/search", params={"q": "roadmap", "semantic": "0"})).json()
    assert data["results"] and all(r["source"] == "filename" for r in data["results"])


async def test_sources_describe_themselves(tmp_path, monkeypatch):
    monkeypatch.setenv("SPOTLIGHT_MOCK_SOURCES", "slack,gdrive")
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=FakeAdapter())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/sources")).json()
    by_name = {s["name"]: s for s in data["sources"]}
    assert set(by_name) == {"folders", "slack", "gdrive"}
    # every source describes its own rendering — nothing for the app to hardcode
    for source in data["sources"]:
        assert source["label"] and source["icon"]
    assert by_name["slack"]["label"] == "Slack"
    assert by_name["gdrive"]["icon"] == "externaldrive"
    assert by_name["folders"]["ok"] is None  # no sync has run yet
    # sources report what they indexed: folder roots / staged documents
    for source in data["sources"]:
        assert "items" in source and "count" in source
