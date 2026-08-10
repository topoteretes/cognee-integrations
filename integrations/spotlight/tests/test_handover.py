"""Handover flow against a fake central cognee server.

The fake implements the four server routes the service touches (remember,
datasets, dataset data, raw) with the same shapes the real server returns, so
these tests pin the wire contract: senior shares -> note lands in the right
dataset -> junior's inbox lists it, ingests it into local memory, and tracks
seen state.
"""

import json
import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from spotlight_backend.adapters import FakeAdapter
from spotlight_backend.config import Settings
from spotlight_backend.handover import HandoverConfig, HandoverService
from spotlight_backend.server import create_app


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if payload is not None else "")

    def json(self):
        return self._payload

    def raise_for_status(self):
        assert self.status_code < 400, f"HTTP {self.status_code}"


class FakeCentralServer:
    """datasets -> data items -> raw bodies, plus multipart remember."""

    def __init__(self):
        self.datasets = {}  # name -> {"id", "items": [{"id","name","created_at","body"}]}

    async def request(self, method, url, headers=None, data=None, files=None, **kwargs):
        path = url.split("://", 1)[-1].split("/", 1)[1]
        if method == "POST" and path == "api/v1/remember":
            dataset = data["datasetName"]
            filename, body, _ = files[0][1]
            entry = self.datasets.setdefault(dataset, {"id": str(uuid.uuid4()), "items": []})
            entry["items"].append(
                {
                    "id": str(uuid.uuid4()),
                    "name": filename,
                    "created_at": f"2026-07-28T12:00:{len(entry['items']):02d}",
                    "body": body.decode("utf-8"),
                }
            )
            return FakeResponse(200, {"ok": True})
        if method == "GET" and path == "api/v1/datasets":
            return FakeResponse(200, [{"id": v["id"], "name": k} for k, v in self.datasets.items()])
        if method == "GET" and path.startswith("api/v1/datasets/"):
            parts = path.split("/")
            ds_id = parts[3]
            entry = next((v for v in self.datasets.values() if v["id"] == ds_id), None)
            if entry is None:
                return FakeResponse(404, {})
            if path.endswith("/raw"):
                data_id = parts[5]
                item = next((i for i in entry["items"] if i["id"] == data_id), None)
                return FakeResponse(200, text=item["body"]) if item else FakeResponse(404, {})
            return FakeResponse(
                200,
                [
                    {"id": i["id"], "name": i["name"], "created_at": i["created_at"]}
                    for i in entry["items"]
                ],
            )
        return FakeResponse(404, {})


def make_service(server, tmp_path, user, team="core", adapter=None):
    config = HandoverConfig(user=user, team=team, base_url="http://hub.test", api_key="k")
    return HandoverService(config=config, data_dir=tmp_path / user, adapter=adapter, client=server)


async def test_share_routes_to_correct_dataset(tmp_path):
    server = FakeCentralServer()
    senior = make_service(server, tmp_path, "vasilije")
    await senior.share("boris", "Deploy runbook", "Always run migrations first.")
    await senior.share("team:core", "Retro lesson", "Feature-flag risky changes.")
    await senior.share("org", "Security policy", "Rotate keys quarterly.")
    assert set(server.datasets) == {
        "handover-inbox-boris",
        "team-core-memory",
        "org-memory",
    }
    note = server.datasets["handover-inbox-boris"]["items"][0]
    assert "from: vasilije" in note["body"]
    assert "Always run migrations first." in note["body"]


async def test_inbox_lists_all_layers_and_ingests_locally(tmp_path):
    server = FakeCentralServer()
    senior = make_service(server, tmp_path, "vasilije")
    await senior.share("boris", "Deploy runbook", "Always run migrations first.")
    await senior.share("team:core", "Retro lesson", "Feature-flag risky changes.")
    await senior.share("org", "Security policy", "Rotate keys quarterly.")
    await senior.share("someone-else", "Not for boris", "private")

    local_memory = FakeAdapter()
    junior = make_service(server, tmp_path, "boris", adapter=local_memory)
    inbox = await junior.inbox()

    layers = {i["layer"] for i in inbox["items"]}
    assert layers == {"inbox", "team", "org"}
    assert inbox["unseen"] == 3
    assert not any("Not for boris" in i["name"] for i in inbox["items"])

    # the received learnings are now searchable in the junior's local memory
    hits = await local_memory.chunks("migrations")
    assert hits and "migrations first" in hits[0]["text"]


async def test_seen_state_persists(tmp_path):
    server = FakeCentralServer()
    senior = make_service(server, tmp_path, "vasilije")
    await senior.share("boris", "Deploy runbook", "Always run migrations first.")

    junior = make_service(server, tmp_path, "boris", adapter=FakeAdapter())
    inbox = await junior.inbox()
    assert inbox["unseen"] == 1
    junior.mark_seen([inbox["items"][0]["id"]])

    reloaded = make_service(server, tmp_path, "boris", adapter=FakeAdapter())
    assert (await reloaded.inbox())["unseen"] == 0


async def test_ingest_happens_once(tmp_path):
    server = FakeCentralServer()
    senior = make_service(server, tmp_path, "vasilije")
    await senior.share("boris", "Deploy runbook", "Always run migrations first.")

    adapter = FakeAdapter()
    junior = make_service(server, tmp_path, "boris", adapter=adapter)
    await junior.inbox()
    docs_after_first = len(adapter._docs)
    await junior.inbox()
    assert len(adapter._docs) == docs_after_first == 1


@pytest.fixture
async def api_client(tmp_path):
    server = FakeCentralServer()
    settings = Settings()
    settings.data_dir = tmp_path / "state"
    adapter = FakeAdapter()
    service = make_service(server, tmp_path, "boris", adapter=adapter)
    app = create_app(settings, adapter=adapter, handover=service)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, server


async def test_http_share_and_inbox_roundtrip(api_client, tmp_path):
    client, server = api_client
    response = await client.post(
        "/share",
        json={"to": "vasilije", "title": "TIL", "body": "The notch hides new menu bar items."},
    )
    assert response.json()["ok"] is True
    assert "handover-inbox-vasilije" in server.datasets

    # something addressed to boris shows up in boris's inbox over HTTP
    senior = make_service(server, tmp_path / "senior-side", "vasilije")
    await senior.share("boris", "Lesson", "Isolate cognee storage per app.")
    data = (await client.get("/inbox")).json()
    assert data["enabled"] is True
    assert data["unseen"] == 1
    assert "Isolate cognee storage" in data["items"][0]["body"]

    await client.post("/inbox/seen", json={"ids": [data["items"][0]["id"]]})
    assert (await client.get("/inbox")).json()["unseen"] == 0


async def test_local_engine_refuses_cross_user_shares(tmp_path):
    """The local single-user engine enforces no permissions, so delivering
    into another user's inbox is refused; only a real hub may do that."""
    from cognee_backend_core.adapters import LocalCogneeAdapter
    from spotlight_backend.handover import HandoverConfig, HandoverService

    service = HandoverService(
        config=HandoverConfig(user="vasilije", team="core", base_url="http://hub"),
        data_dir=tmp_path,
        adapter=LocalCogneeAdapter("spotlight"),
    )
    result = await service.share("boris", "runbook", "steps")
    assert result["ok"] is False and "hub" in result["error"]
    # own inbox would still be allowed (no cross-user write) — the guard
    # only rejects datasets that are not the sender's own
    assert service.dataset_for_recipient("vasilije") == service.inbox_dataset
