import json

import httpx
import pytest
from cognee_integration_tapes_cassette.config import Config
from cognee_integration_tapes_cassette.server import create_app
from cognee_integration_tapes_cassette.tapes_client import TapesClient

SESSION_COMPLETED = {
    "session": {
        "id": "11111111-1111-1111-1111-111111111111",
        "name": "fix-login-bug",
        "display_title": "Fix login bug",
        "harness_id": "claude-code",
        "started_at": "2026-08-20T10:00:00Z",
        "rollup": {"status": "completed", "model": "claude-fable-5"},
    },
    "traces": [
        {
            "trace": {"user_prompt": "Why does login fail for SSO users?"},
            "spans": [
                {
                    "kind": "llm",
                    "call_kind": "main",
                    "seq": 2,
                    "output": [
                        {"type": "text", "text": "The SSO callback drops the session cookie."},
                    ],
                },
                {
                    "kind": "llm",
                    "call_kind": "main",
                    "seq": 1,
                    "output": [
                        {"type": "thinking", "text": "secret reasoning"},
                        {
                            "type": "tool_use",
                            "tool_name": "Bash",
                            "tool_input": {"command": "grep -r set_cookie auth/"},
                        },
                    ],
                },
                {
                    "kind": "llm",
                    "call_kind": "subagent",
                    "seq": 3,
                    "output": [{"type": "text", "text": "SUBAGENT NOISE"}],
                },
            ],
        }
    ],
}

SESSION_RUNNING = {
    "session": {
        "id": "22222222-2222-2222-2222-222222222222",
        "name": "in-progress",
        "rollup": {"status": "running", "model": "claude-fable-5"},
    },
    "traces": [
        {
            "trace": {"user_prompt": "still going"},
            "spans": [
                {
                    "kind": "llm",
                    "call_kind": "main",
                    "seq": 1,
                    "output": [{"type": "text", "text": "working on it"}],
                }
            ],
        }
    ],
}

LIST_ITEMS = [
    {
        "id": SESSION_COMPLETED["session"]["id"],
        "last_seen_at": "2026-08-20T11:00:00Z",
    },
    {
        "id": SESSION_RUNNING["session"]["id"],
        "last_seen_at": "2026-08-21T09:00:00Z",
    },
]

EXPORTS = {
    SESSION_COMPLETED["session"]["id"]: SESSION_COMPLETED,
    SESSION_RUNNING["session"]["id"]: SESSION_RUNNING,
}


class FakeCognee:
    """Records add/cognify/search calls instead of touching real storage."""

    def __init__(self):
        self.added = []
        self.cognified = []
        self.search_calls = []
        self.search_results = ["stub answer"]

    async def add(self, data, dataset_name):
        self.added.append((data, dataset_name))

    async def cognify(self, datasets):
        self.cognified.append(datasets)

    async def search(self, **kwargs):
        self.search_calls.append(kwargs)
        return list(self.search_results)


def make_tapes_transport(list_items=None, exports=None):
    list_items = LIST_ITEMS if list_items is None else list_items
    exports = EXPORTS if exports is None else exports

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            return httpx.Response(200, json={"items": list_items, "next_cursor": None})
        for session_id, payload in exports.items():
            if request.url.path == f"/v1/sessions/{session_id}/export":
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(handler)


@pytest.fixture
def config(tmp_path):
    return Config(
        tapes_base_url="http://tapes.test",
        dataset_name="test_sessions",
        host="127.0.0.1",
        port=9900,
        state_path=tmp_path / "state.json",
        storage_root=None,
    )


@pytest.fixture
def fake_cognee(monkeypatch):
    fake = FakeCognee()
    from cognee_integration_tapes_cassette import ingest

    monkeypatch.setattr(ingest, "cognee", fake)
    return fake


@pytest.fixture
def app(config, fake_cognee):
    transport = make_tapes_transport()
    tapes = TapesClient(config.tapes_base_url, client=httpx.AsyncClient(transport=transport))
    return create_app(config, tapes=tapes)


def read_state(config):
    return json.loads(config.state_path.read_text())
