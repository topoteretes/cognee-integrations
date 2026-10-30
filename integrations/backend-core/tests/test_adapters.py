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
    import pytest

    # the attestation is mandatory: this posture disables access control
    with pytest.raises(ValueError, match="multi-tenant|single_user|access control"):
        single_user_runtime(tmp_path / "store")
    env = single_user_runtime(tmp_path / "store", _i_am_single_tenant=True)
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
            "dataset_name": "main",
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
    assert meta["sources"] == ["main"]  # the refusal dataset is not a source


async def test_http_adapter_context_manager_closes_client():
    from cognee_backend_core.adapters import HttpCogneeAdapter

    class ClosableClient:
        closed = False

        async def aclose(self):
            self.closed = True

    client = ClosableClient()
    async with HttpCogneeAdapter("main", "http://hub") as adapter:
        adapter._own_client = client
    assert client.closed
    assert adapter._own_client is None


async def test_local_adapter_scope_all_spans_datasets_with_exclusions(monkeypatch):
    """Local search-all runs one search per dataset (a merged local search
    has no dataset envelopes), keeps attribution, and honors exclusions —
    the same contract as the HTTP adapter against a tenant."""
    import sys
    import types

    from cognee_backend_core.adapters import LocalCogneeAdapter

    per_dataset = {
        "main": ["I'm sorry, the knowledge-graph does not contain that."],
        "github-acme-rockets": [
            "The retry logic was added in PR #9 after the fuel gauge incident, "
            "with a review note to keep attempts bounded and the parachute "
            "deploy sequence idempotent across all failure modes."
        ],
        "handover-inbox-someone-else": ["private mail"],
    }
    searched: list[list[str]] = []

    async def fake_search(**kwargs):
        searched.append(kwargs["datasets"])
        return per_dataset.get(kwargs["datasets"][0], [])

    class DS:
        def __init__(self, name):
            self.name = name

    async def fake_get_datasets(user_id):
        return [DS(n) for n in per_dataset]

    async def fake_get_default_user():
        return types.SimpleNamespace(id="u1")

    mods = {
        "cognee": types.ModuleType("cognee"),
        "cognee.api": types.ModuleType("cognee.api"),
        "cognee.api.v1": types.ModuleType("cognee.api.v1"),
        "cognee.api.v1.search": types.ModuleType("cognee.api.v1.search"),
        "cognee.modules": types.ModuleType("cognee.modules"),
        "cognee.modules.data": types.ModuleType("cognee.modules.data"),
        "cognee.modules.data.methods": types.ModuleType("cognee.modules.data.methods"),
        "cognee.modules.users": types.ModuleType("cognee.modules.users"),
        "cognee.modules.users.methods": types.ModuleType("cognee.modules.users.methods"),
    }
    mods["cognee"].SearchType = {"GRAPH_COMPLETION": "GRAPH_COMPLETION", "CHUNKS": "CHUNKS"}
    mods["cognee.api.v1.search"].search = fake_search
    mods["cognee.modules.data.methods"].get_datasets = fake_get_datasets
    mods["cognee.modules.users.methods"].get_default_user = fake_get_default_user
    for name, mod in mods.items():
        monkeypatch.setitem(sys.modules, name, mod)

    adapter = LocalCogneeAdapter(
        "main",
        search_all=True,
        exclude_predicate=lambda n: n.startswith("handover-inbox-"),
    )
    meta = await adapter.answer_with_sources("what changed in the rockets repo")
    # one search per non-excluded dataset, never the excluded inbox
    assert sorted(d[0] for d in searched) == ["github-acme-rockets", "main"]
    assert "retry logic" in meta["answer"]
    assert meta["sources"] == ["github-acme-rockets"]  # the refusal is not a source

    # scoped search still pins to the adapter's own dataset
    searched.clear()
    await adapter.chunks("query")
    assert searched == [["main"]]
