"""End-to-end backend tests over the HTTP surface, with the fake adapter.

This is the same wiring the macOS app hits, minus cognee: index a real temp
folder, poll status, search by filename and by content, ask for an answer.
"""

import asyncio

import pytest
from desktop_backend.adapters import FakeAdapter
from desktop_backend.config import Settings
from desktop_backend.server import create_app
from httpx import ASGITransport, AsyncClient


@pytest.fixture
def workspace(tmp_path):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "roadmap.md").write_text("Ship the cognee desktop integration in Q3.")
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
    from desktop_backend import server as server_module

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


async def test_capture_writes_note_and_indexes(client, tmp_path):
    response = await client.post(
        "/capture",
        json={"text": "Deploy needs the staging flag.", "source": "quick-capture"},
    )
    assert response.status_code == 202
    body = response.json()
    assert body["ok"] is True

    capture_dir = tmp_path / "state" / "capture"
    notes = list(capture_dir.glob("*.md"))
    assert len(notes) == 1
    content = notes[0].read_text()
    assert "Deploy needs the staging flag." in content
    assert "captured from: quick-capture" in content

    # the note indexes like any document and is findable by filename
    for _ in range(100):
        status = (await client.get("/index/status")).json()
        if status["state"] in ("idle", "error"):
            break
        await asyncio.sleep(0.01)
    assert status["state"] == "idle"
    data = (await client.get("/search", params={"q": "deploy", "semantic": "0"})).json()
    assert any(r["path"] == str(notes[0]) for r in data["results"])


async def test_capture_rejects_empty_text(client, tmp_path):
    response = await client.post("/capture", json={"text": "   "})
    assert response.status_code == 202
    assert response.json()["ok"] is False
    assert not (tmp_path / "state" / "capture").exists()


async def test_sources_describe_themselves(tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_DESKTOP_MOCK_SOURCES", "slack,gdrive")
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
    assert by_name["slack"]["label"] == "Slack (demo)"
    assert by_name["gdrive"]["icon"] == "externaldrive"
    assert by_name["folders"]["ok"] is None  # no sync has run yet
    # sources report what they indexed: folder roots / staged documents
    for source in data["sources"]:
        assert "items" in source and "count" in source
    # ...and what "connected" actually covers (channels, repos, roots)
    assert by_name["slack"]["scope"] == ["#product", "#eng-incidents"]
    assert by_name["gdrive"]["scope"] == ["My Drive — shared docs"]


async def test_whisper_surfaces_related_memory(client, workspace):
    await index_and_wait(client, workspace)
    data = (await client.get("/whisper", params={"q": "Pasta carbonara: eggs"})).json()
    assert data["related"] and "pecorino" in data["related"][0]
    assert data["conflicts"] == []
    # too-short notes stay silent
    short = (await client.get("/whisper", params={"q": "carbonara"})).json()
    assert short == {"related": [], "conflicts": []}


async def test_digest_counts_new_agent_learnings(tmp_path):
    class TenantDouble:
        class _Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        dataset = "main"

        async def chunks(self, query, top_k=8):
            return []

        async def _request(self, method, path, **kwargs):
            if path == "/api/v1/datasets":
                return self._Response([{"name": "agent_sessions", "id": "ds1"}])
            return self._Response(
                [
                    {"name": "old-learning", "created_at": "2026-08-01T10:00:00Z"},
                    {"name": "fresh-learning", "created_at": "2026-08-06T09:00:00Z"},
                ]
            )

    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=TenantDouble())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # since Aug 3rd 2026 (1785700000): only the fresh learning counts
        data = (await client.get("/digest", params={"since": 1785700000})).json()
    assert data["count"] == 1
    assert data["titles"] == ["fresh-learning"]


async def test_files_lists_the_index_with_filtering(client, workspace):
    await index_and_wait(client, workspace)
    data = (await client.get("/files")).json()
    assert data["total"] == 3
    names = {f["name"] for f in data["files"]}
    assert {"roadmap.md", "recipes.txt", "huge.md"} <= names
    assert all(f["path"] and f["mtime"] for f in data["files"])

    filtered = (await client.get("/files", params={"q": "recipes"})).json()
    assert [f["name"] for f in filtered["files"]] == ["recipes.txt"]
    assert filtered["matched"] == 1 and filtered["total"] == 3


async def test_forget_removes_file_from_index(client, workspace):
    await index_and_wait(client, workspace)
    target = str(workspace / "recipes.txt")
    response = (await client.post("/files/forget", json={"path": target})).json()
    assert response["ok"] is True and response["removed"] == 1
    # the fake adapter has no tenant, so the graph copy is honestly "kept"
    assert response["graph"] == "kept"

    data = (await client.get("/files")).json()
    assert all(f["path"] != target for f in data["files"])
    assert data["total"] == 2
    # filename search no longer surfaces it
    hits = (await client.get("/search", params={"q": "recipes", "semantic": "0"})).json()
    assert all(r["path"] != target for r in hits["results"])
    # forgetting again is a truthful no-op
    again = (await client.post("/files/forget", json={"path": target})).json()
    assert again["ok"] is False and "not in the index" in again["detail"]


async def test_forget_root_unwatches_and_keeps_graph(client, workspace):
    await index_and_wait(client, workspace)
    response = (await client.post("/files/forget", json={"path": str(workspace)})).json()
    assert response["ok"] is True and response["removed"] == 3
    assert response["graph"] == "kept"  # bulk graph deletion stays manual
    status = (await client.get("/index/status")).json()
    assert str(workspace) not in (status["roots"] or [])
    assert (await client.get("/files")).json()["total"] == 0


async def test_forget_directly_indexed_file_takes_the_file_path(client, tmp_path):
    lone = tmp_path / "lone-note.md"
    lone.write_text("a note indexed on its own")
    await index_and_wait(client, lone)
    response = (await client.post("/files/forget", json={"path": str(lone)})).json()
    # it was technically a root, but a single-file one: per-item semantics
    assert response["ok"] is True and response["removed"] == 1
    assert "root unwatched" not in response["detail"]
    assert (await client.get("/files")).json()["total"] == 0


async def test_index_with_extension_filter_sticks_to_the_root(client, tmp_path):
    docs = tmp_path / "mixed"
    docs.mkdir()
    (docs / "contract.pdf").write_bytes(b"%PDF-1.4 fake")
    (docs / "notes.md").write_text("markdown that should stay out")
    (docs / "report.docx").write_bytes(b"PK fake docx")

    response = await client.post(
        "/index", json={"paths": [str(docs)], "extensions": ["pdf", ".docx"]}
    )
    assert response.status_code == 202
    for _ in range(100):
        status = (await client.get("/index/status")).json()
        if status["state"] in ("idle", "error"):
            break
        await asyncio.sleep(0.01)
    assert status["state"] == "idle"

    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert names == {"contract.pdf", "report.docx"}  # .md filtered out
    assert status["root_filters"][str(docs)] == [".pdf", ".docx"]

    # a later plain reindex keeps honoring the root's filter
    (docs / "later.md").write_text("still filtered")
    (docs / "later.pdf").write_bytes(b"%PDF later")
    await index_and_wait(client, docs)
    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert "later.pdf" in names and "later.md" not in names


async def test_forgotten_file_stays_forgotten_across_resyncs(client, workspace):
    await index_and_wait(client, workspace)
    target = str(workspace / "recipes.txt")
    assert (await client.post("/files/forget", json={"path": target})).json()["ok"]

    # the watched-folder re-sync must NOT resurrect it
    await index_and_wait(client, workspace)
    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert "recipes.txt" not in names

    # explicitly indexing that exact file again is consent — tombstone clears
    await index_and_wait(client, target)
    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert "recipes.txt" in names


async def test_root_labels_persist_and_report(client, tmp_path):
    work = tmp_path / "work-docs"
    work.mkdir()
    (work / "q3.md").write_text("work things")
    response = await client.post("/index", json={"paths": [str(work)], "label": "work"})
    assert response.status_code == 202
    for _ in range(100):
        status = (await client.get("/index/status")).json()
        if status["state"] in ("idle", "error"):
            break
        await asyncio.sleep(0.01)
    assert status["root_labels"][str(work)] == "work"


async def test_agents_relays_and_labels_connections(tmp_path):
    class TenantDouble:
        class _Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        dataset = "main"

        async def chunks(self, query, top_k=8):
            return []

        async def _request(self, method, path, **kwargs):
            if path == "/api/v1/integrations/status":
                return self._Response(
                    {
                        "plugins": [
                            {
                                "key": "desktop",
                                "connected": True,
                                "lastActiveAt": "2026-08-23T10:16:00Z",
                                "source": "identity",
                            },
                            {"key": "codex", "connected": False},
                        ]
                    }
                )
            assert path == "/api/v1/agents/connections"
            return self._Response(
                {
                    "agents": [
                        {
                            "session_id": "claude_abc",
                            "type": "api",
                            "status": "active",
                            "last_active_at": "2026-08-23T09:30:00Z",
                            "datasets": [{"name": "agent_sessions"}],
                        },
                        {
                            "session_id": "codex_xyz",
                            "type": "api",
                            "status": "inactive",
                            "last_active_at": "2026-08-20T00:00:00Z",
                            "datasets": [],
                        },
                    ]
                }
            )

    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=TenantDouble())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/agents")).json()
    assert [a["label"] for a in data["agents"]] == ["Claude Code", "Codex"]
    assert data["agents"][0]["status"] == "active"
    assert data["agents"][0]["datasets"] == ["agent_sessions"]
    # provisioned identities (cognee >= 1.5.1) ride along, connected first
    assert data["plugins"][0] == {
        "key": "desktop",
        "label": "Cognee Desktop",
        "connected": True,
        "last_active_at": "2026-08-23T10:16:00Z",
        "source": "identity",
    }


async def test_agents_empty_without_a_tenant(client):
    assert (await client.get("/agents")).json() == {"agents": []}


async def test_pause_stops_resync_resume_restarts(client, workspace):
    await index_and_wait(client, workspace)
    response = (
        await client.post("/roots/pause", json={"path": str(workspace), "paused": True})
    ).json()
    assert response["ok"] and str(workspace) in response["paused"]

    # a new file lands while paused: the re-sync must not pick it up
    (workspace / "while-paused.md").write_text("added while paused")
    await index_and_wait(client, workspace)
    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert "while-paused.md" not in names
    # still searchable though
    hits = (await client.get("/search", params={"q": "roadmap", "semantic": "0"})).json()
    assert hits["results"]

    # resume → the file arrives on the next run
    await client.post("/roots/pause", json={"path": str(workspace), "paused": False})
    await index_and_wait(client, workspace)
    names = {f["name"] for f in (await client.get("/files")).json()["files"]}
    assert "while-paused.md" in names


async def test_changes_feed_filters_and_maps(tmp_path):
    class TenantDouble:
        class _Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        dataset = "main"

        async def chunks(self, query, top_k=8):
            return []

        async def _request(self, method, path, **kwargs):
            assert path.startswith("/api/v1/activity/pipeline-runs")
            return self._Response(
                [
                    {
                        "pipeline_name": "cognify_pipeline",
                        "status": "completed",
                        "dataset_name": "team-core-memory",
                        "owner_email": "boris@example.com",
                        "created_at": "2026-09-01T10:00:00Z",
                    },
                    {
                        "pipeline_name": "add_pipeline",
                        "status": "failed",
                        "dataset_name": "spotlight",
                        "owner_email": "vasilije@example.com",
                        "created_at": "2026-09-01T09:59:00Z",
                    },
                    {
                        "pipeline_name": "old_run",
                        "status": "completed",
                        "dataset_name": "spotlight",
                        "owner_email": "vasilije@example.com",
                        "created_at": "2026-08-01T00:00:00Z",
                    },
                ]
            )

    settings = Settings()
    settings.data_dir = tmp_path / "state"
    app = create_app(settings, adapter=TenantDouble())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        data = (await client.get("/changes", params={"since": 1787000000})).json()
    # failed runs and rows older than since (2026-08-17) are dropped
    assert len(data["changes"]) == 1
    change = data["changes"][0]
    assert change["who"] == "boris@example.com"
    assert change["dataset"] == "team-core-memory"
    assert change["what"] == "cognify_pipeline"
