import httpx
from fastapi.testclient import TestClient

from .conftest import SESSION_COMPLETED, SESSION_RUNNING, read_state


def client(app):
    return TestClient(app)


def test_ping(app):
    response = client(app).get("/ping")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_openapi_serves_manifest(app):
    payload = client(app).get("/openapi").json()
    assert payload["x-tapes-cassette"]["kind"] == "cassette/v1alpha1"
    assert "/api/sync" in payload["paths"]


def test_sync_ingests_completed_sessions_only(app, config, fake_cognee):
    response = client(app).post("/api/sync", json={"wait": True})
    assert response.status_code == 200
    body = response.json()
    assert body["accepted"] is True

    status = body["status"]
    assert status["state"] == "completed"
    assert status["fetched"] == 2
    assert status["ingested"] == 1  # completed session
    assert status["skipped"] == 1  # running session

    assert len(fake_cognee.added) == 1
    text, dataset = fake_cognee.added[0]
    assert dataset == "test_sessions"
    assert "SSO callback" in text
    assert fake_cognee.cognified == [["test_sessions"]]

    # Checkpoint advances only over completed sessions (the running one is
    # newer but must be re-checked next run).
    assert status["last_synced_at"] == "2026-08-20T11:00:00+00:00"
    state = read_state(config)
    assert state["sessions"][SESSION_COMPLETED["session"]["id"]]
    assert SESSION_RUNNING["session"]["id"] not in state["sessions"]
    assert state["pending_cognify"] is False


def test_second_sync_skips_unchanged_and_cognify(app, fake_cognee):
    test_client = client(app)
    test_client.post("/api/sync", json={"wait": True})
    body = test_client.post("/api/sync", json={"wait": True, "full": True}).json()

    assert body["status"]["ingested"] == 0
    assert body["status"]["unchanged"] == 1
    assert len(fake_cognee.added) == 1  # nothing re-added
    assert len(fake_cognee.cognified) == 1  # cognify not re-run


def test_sync_status_endpoint(app):
    test_client = client(app)
    assert test_client.post("/api/sync/status").json()["state"] == "idle"
    test_client.post("/api/sync", json={"wait": True})
    status = test_client.post("/api/sync/status").json()
    assert status["state"] == "completed"
    assert status["dataset"] == "test_sessions"


def test_sync_survives_export_failures(config, fake_cognee):
    from cognee_integration_tapes_cassette.server import create_app
    from cognee_integration_tapes_cassette.tapes_client import TapesClient

    from .conftest import make_tapes_transport

    # Only the completed session's export resolves; the other id 404s.
    transport = make_tapes_transport(
        exports={SESSION_COMPLETED["session"]["id"]: SESSION_COMPLETED}
    )
    tapes = TapesClient(config.tapes_base_url, client=httpx.AsyncClient(transport=transport))
    app = create_app(config, tapes=tapes)

    status = client(app).post("/api/sync", json={"wait": True}).json()["status"]
    assert status["state"] == "completed"
    assert status["ingested"] == 1
    assert status["skipped"] == 1


def test_search_returns_results(app, fake_cognee):
    response = client(app).post("/api/search", json={"query": "what broke login?", "top_k": 3})
    assert response.status_code == 200
    assert response.json() == {"results": ["stub answer"]}

    call = fake_cognee.search_calls[0]
    assert call["query_text"] == "what broke login?"
    assert call["datasets"] == ["test_sessions"]
    assert call["top_k"] == 3


def test_search_rejects_unknown_search_type(app):
    response = client(app).post(
        "/api/search", json={"query": "q", "search_type": "NOT_A_TYPE"}
    )
    assert response.status_code == 400
    assert "NOT_A_TYPE" in response.json()["detail"]


def test_search_requires_query(app):
    assert client(app).post("/api/search", json={}).status_code == 422
