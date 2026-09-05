"""Regression tests for the identity and dataset-access review findings."""

import pytest


def test_cached_identity_rejects_new_principal_on_same_server(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    pc.save_cached_agent_key("https://same.test", "account-a-agent-key", "agent-a")
    monkeypatch.setenv("COGNEE_API_KEY", "account-b-principal-key")
    with pytest.raises(RuntimeError, match="principal"):
        pc._api_key_with_source("https://same.test")


def test_dataset_listing_includes_shared_writable_dataset(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(
        pc,
        "_json_http_request",
        lambda *a, **k: [
            {
                "id": "shared-dataset-id",
                "name": "shared",
                "ownerId": "other-user",
                "permissions": ["read", "write"],
            }
        ],
    )
    listing = pc.list_writable_datasets("this-agent")
    assert listing["datasets"][0]["id"] == "shared-dataset-id"
    assert listing["hidden_readonly"] == 0


def test_false_identity_setting_disables_cached_identity(suite, hook_module):
    import asyncio

    start = hook_module(suite, "session-start.py")
    import _plugin_common as pc

    pc.save_cached_agent_key("https://same.test", "cached-agent-key", "agent-a")
    key = asyncio.run(
        start._ensure_plugin_identity(
            "https://same.test", {"plugin_identity": False}, "principal-key"
        )
    )
    assert key == ""


def test_federated_graph_recall_does_not_reuse_session_binding(
    suite, isolated_modules, monkeypatch
):
    import json
    from uuid import uuid4

    pc = isolated_modules(suite, "_plugin_common")
    ids = [str(uuid4()), str(uuid4())]
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", json.dumps(ids))
    calls = []
    monkeypatch.setattr(
        pc, "_json_http_request", lambda path, payload, **kw: calls.append(payload) or []
    )
    pc.recall_via_http("question", session_id="active", top_k=3, scope=["graph"], dataset="owned")
    assert calls[-1]["dataset_ids"] == ids
    assert "session_id" not in calls[-1] and "datasets" not in calls[-1]
    pc.recall_via_http("question", session_id="active", top_k=3, scope=["session"], dataset="owned")
    assert calls[-1]["session_id"] == "active" and calls[-1]["datasets"] == ["owned"]


def test_shared_uuid_is_used_by_write_and_registration(suite, isolated_modules, monkeypatch):
    from uuid import uuid4

    pc = isolated_modules(suite, "_plugin_common")
    ident = str(uuid4())
    calls = []
    monkeypatch.setattr(
        pc, "_json_http_request", lambda path, payload, **kw: calls.append(payload) or {}
    )
    pc.remember_entry_via_http(ident, "session", {"type": "qa", "question": "q", "answer": "a"})
    assert calls[-1]["dataset_id"] == ident and "dataset_name" not in calls[-1]
    pc.register_agent_via_http(agent_session_name="connection", dataset_names=[ident])
    assert calls[-1]["dataset_ids"] == [ident] and calls[-1]["dataset_names"] == []


def test_failed_switch_write_keeps_previous_record(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    pc.ensure_launch_record("test-host", "/tmp", dataset="old")
    before = pc._read_map_record("test-host")
    monkeypatch.setattr(pc, "_write_map_record", lambda *a: None)
    with pytest.raises(RuntimeError, match="not persisted"):
        pc.switch_launch_record("test-host", session_id="new", dataset="new", conn_uuid="new")
    assert pc._read_map_record("test-host") == before
