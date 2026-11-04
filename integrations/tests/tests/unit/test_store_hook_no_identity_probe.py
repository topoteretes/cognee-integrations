"""The capture hooks never block on identity probes (#270).

``store-to-session.py`` runs on every PostToolUse and Stop. It used to call
``load_resolved()`` with its defaults, which queries ``/agents/connections/me``
and then ``/users/me`` with a 10s timeout each — on a slow backend, up to ~20s
before an entry was written or buffered, and Codex runs these hooks
synchronously. No HTTP store path uses the user id, so the hooks resolve local
fields only (Antigravity still probes for its local SDK path).
"""

from __future__ import annotations

import asyncio
import sys

import pytest

_IDENTITY_PATHS = ("/api/v1/agents/connections/me", "/api/v1/users/me")


@pytest.fixture
def store(suite, hook_module, monkeypatch, closed_port_url):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-1")
    module = hook_module(suite, "store-to-session.py")
    pc = sys.modules["_plugin_common"]
    requests: list[str] = []

    def record(path, *a, **k):
        requests.append(path.split("?", 1)[0])
        return {}

    monkeypatch.setattr(pc, "_json_http_request", record)
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "server_usable", lambda *a, **k: False)
    buffered: list[tuple] = []
    monkeypatch.setattr(module, "append_warmup_entry", lambda *a, **k: buffered.append(a))
    module.requests, module.buffered = requests, buffered
    return module


def _identity_requests(store) -> list[str]:
    return [p for p in store.requests if p in _IDENTITY_PATHS]


def test_load_session_resolves_locally(store):
    session_id, dataset, _ = store._load_session()
    assert session_id and dataset
    assert _identity_requests(store) == []


def test_tool_trace_is_buffered_without_identity_probes(store):
    asyncio.run(
        store._store_tool_call(
            {"tool_name": "Grep", "tool_input": {"pattern": "x"}, "tool_response": "ok"}
        )
    )
    assert _identity_requests(store) == []
    assert len(store.buffered) == 1


def test_antigravity_local_sdk_mode_still_resolves_identity(suite, hook_module, monkeypatch):
    """Only Antigravity kept the in-process SDK path, which needs the user id."""
    if suite.name != "antigravity":
        pytest.skip(f"{suite.name}: no local SDK store path")
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    module = hook_module(suite, "store-to-session.py")
    calls: list[dict] = []
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"mode": "local_sdk"})
    monkeypatch.setattr(module, "load_resolved", lambda **k: calls.append(k) or {})
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(module, "get_session_id", lambda config: "s")
    monkeypatch.setattr(module, "get_dataset", lambda config: "d")
    module._load_session()
    assert calls == [{"identity": True}]
