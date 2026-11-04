"""Regression tests for the identity and dataset-access review findings."""

import pytest


def test_cached_identity_rejects_new_principal_on_same_server(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    pc.save_cached_agent_key("https://same.test", "account-a-agent-key", "agent-a")
    monkeypatch.setenv("COGNEE_API_KEY", "account-b-principal-key")
    # ``auto``: the foreign identity is never used; the plugin runs as the
    # new principal instead.
    assert pc._api_key_with_source("https://same.test") == (
        "account-b-principal-key",
        "env_api_key",
    )
    # Explicit identity: an error, never a silent switch of accounts.
    monkeypatch.setenv("COGNEE_PLUGIN_IDENTITY", "true")
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


def test_malformed_read_dataset_ids_env_names_the_setting(suite, isolated_modules, monkeypatch):
    """Broken JSON in COGNEE_PLUGIN_READ_DATASET_IDS is a configuration error
    that says so, not a bare JSON traceback from inside the recall."""
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_PLUGIN_READ_DATASET_IDS", "[invalid")
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **kw: pytest.fail("request sent"))
    with pytest.raises(ValueError, match="COGNEE_PLUGIN_READ_DATASET_IDS is not valid JSON"):
        pc.recall_via_http("question", session_id="active", top_k=3, scope=["graph"], dataset="d")
    # Session-scoped recall never reads the federated setting.
    monkeypatch.setattr(pc, "_json_http_request", lambda *a, **kw: [])
    assert (
        pc.recall_via_http("question", session_id="active", top_k=3, scope=["session"], dataset="d")
        == []
    )


# What a server's API description says when POST /api/v1/remember/entry takes a
# dataset id: the field in the operation's request body, inline here and, as
# real servers write it, behind a $ref below.
_ENTRY_TAKES_DATASET_ID = {
    "paths": {
        "/api/v1/remember/entry": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"properties": {"dataset_id": {}, "dataset_name": {}}}
                        }
                    }
                }
            }
        }
    }
}
_ENTRY_TAKES_DATASET_ID_BY_REF = {
    "paths": {
        "/api/v1/remember/entry": {
            "post": {
                "requestBody": {
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/RememberEntryPayload"}
                        }
                    }
                }
            }
        }
    },
    "components": {
        "schemas": {"RememberEntryPayload": {"properties": {"dataset_id": {}, "entry": {}}}}
    },
}


def test_shared_uuid_is_used_by_write_and_registration(suite, isolated_modules, monkeypatch):
    from uuid import uuid4

    pc = isolated_modules(suite, "_plugin_common")
    ident = str(uuid4())
    calls = []
    monkeypatch.setattr(
        pc,
        "_json_http_request",
        lambda path, payload=None, **kw: (
            _ENTRY_TAKES_DATASET_ID if path == "/openapi.json" else calls.append(payload) or {}
        ),
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


def test_endpoint_resolution_preserves_explicit_parent(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    url = "https://explicit-account.test"
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    monkeypatch.setenv("COGNEE_API_KEY", "explicit-parent")
    monkeypatch.delenv("COGNEE_PRINCIPAL_API_KEY", raising=False)
    pc.save_cached_agent_key(url, "agent", "agent-id", principal_key="explicit-parent")
    assert pc.resolved_http_endpoint_auth() == (url, "agent")
    assert pc.resolved_http_endpoint_auth() == (url, "agent")
    assert pc._api_key() == "agent"


def test_rejected_old_key_does_not_block_new_reconnection(suite, isolated_modules):
    pc = isolated_modules(suite, "_plugin_common")
    pc.save_cached_agent_key("https://test", "new-key", "agent", principal_key="parent")
    pc.block_cached_agent_key("old-key")
    assert not pc._load_json_file(pc._AGENT_KEY_CACHE).get("blocked")
    pc.block_cached_agent_key("new-key")
    assert pc._load_json_file(pc._AGENT_KEY_CACHE)["blocked"] is True


def test_provision_uses_explicit_server_for_every_request(suite, isolated_modules, monkeypatch):
    pc = isolated_modules(suite, "_plugin_common")
    calls = []

    def request(path, *args, **kwargs):
        calls.append((path, kwargs))
        if path == "/openapi.json":
            return {
                "paths": {
                    "/api/v1/integrations/plugins/{plugin_key}/provision": {
                        "post": {"x-cognee-additive-plugin-keys": True}
                    }
                }
            }
        return {"apiKey": "agent", "agentId": "identity", "created": True}

    monkeypatch.setattr(pc, "_json_http_request", request)
    status, _ = pc.provision_plugin_agent_via_http(
        principal_key="parent", service_url="https://explicit-server.test"
    )
    assert status == "provisioned"
    assert [path for path, _ in calls] == [
        "/openapi.json",
        f"/api/v1/integrations/plugins/{pc.PLUGIN_KEY}/provision",
    ]
    assert all(call[1]["base_url"] == "https://explicit-server.test" for call in calls)


def test_old_sdk_cannot_accept_uuid_session_writes(suite, isolated_modules, monkeypatch):
    from uuid import uuid4

    pc = isolated_modules(suite, "_plugin_common")
    calls = []

    def request(path, *args, **kwargs):
        calls.append(path)
        # An old server: its entry endpoint lists no dataset_id in its request body.
        return {"paths": {"/api/v1/remember/entry": {"post": {}}}}

    monkeypatch.setattr(pc, "_json_http_request", request)
    with pytest.raises(RuntimeError, match="cannot safely store"):
        pc.remember_entry_via_http(
            str(uuid4()), "session", {"type": "qa", "question": "q", "answer": "a"}
        )
    assert calls == ["/openapi.json"]


def test_typed_dataset_support_is_read_from_the_entry_schema(suite, isolated_modules, monkeypatch):
    """The capability is what the server's own API description says: the
    request body of POST /api/v1/remember/entry lists ``dataset_id``, inline
    or behind a ``$ref`` as real servers write it. A custom marker no server
    emits used to be required, which refused every real server."""
    pc = isolated_modules(suite, "_plugin_common")
    for spec in (_ENTRY_TAKES_DATASET_ID, _ENTRY_TAKES_DATASET_ID_BY_REF):
        pc._TYPED_DATASET_CAPABILITIES.clear()
        monkeypatch.setattr(pc, "_json_http_request", lambda *a, **k: spec)
        pc.require_typed_dataset_id_support(service_url="https://tenant-x.stg-aws.cognee.ai")
    pc._TYPED_DATASET_CAPABILITIES.clear()
    monkeypatch.setattr(
        pc,
        "_json_http_request",
        lambda *a, **k: {
            "paths": {"/api/v1/remember/entry": {"post": {"x-cognee-session-dataset-ids": True}}}
        },
    )
    with pytest.raises(RuntimeError, match="takes no dataset_id"):
        pc.require_typed_dataset_id_support(service_url="https://tenant-x.stg-aws.cognee.ai")


def test_unsupported_shared_switch_aborts_before_sync(suite, hook_module, monkeypatch):
    from uuid import uuid4

    switch = hook_module(suite, "switch-dataset.py")
    import _plugin_common as pc

    ident = str(uuid4())
    monkeypatch.setattr(switch, "load_resolved", lambda **kw: {"user_id": "writer"})
    monkeypatch.setattr(
        switch,
        "list_writable_datasets",
        lambda *args: {
            "datasets": [{"id": ident, "name": "shared", "owner_id": "other", "writable": True}],
            "readonly": [],
        },
    )
    monkeypatch.setattr(pc, "_json_http_request", lambda *args, **kw: {"paths": {}})
    calls = []
    monkeypatch.setattr(switch, "_sync_current", lambda *args: calls.append("sync"))
    with pytest.raises(RuntimeError, match="cannot safely store"):
        switch._switch("host", {"session_id": "old", "dataset": "owned"}, ident, force=False)
    assert calls == []


def test_owned_uuid_selection_uses_compatible_owned_name(suite, hook_module, monkeypatch):
    from uuid import uuid4

    switch = hook_module(suite, "switch-dataset.py")
    ident = str(uuid4())
    monkeypatch.setattr(switch, "load_resolved", lambda **kw: {"user_id": "writer"})
    monkeypatch.setattr(
        switch,
        "list_writable_datasets",
        lambda *args: {
            "datasets": [{"id": ident, "name": "owned", "owner_id": "writer", "writable": True}],
            "readonly": [],
        },
    )
    monkeypatch.setattr(switch, "_sync_current", lambda *args: None)
    chosen = []

    def inspect(target):
        chosen.append(target)
        raise RuntimeError("inspection complete")

    monkeypatch.setattr(switch, "_ensure_dataset", inspect)
    with pytest.raises(RuntimeError, match="inspection complete"):
        switch._switch("host", {"session_id": "old", "dataset": "previous"}, ident, force=False)
    assert chosen == ["owned"]


def test_switch_takes_the_principal_from_the_lister_when_the_record_has_none(
    suite, hook_module, monkeypatch
):
    """A cloud tenant the session was not registered on leaves no user id in
    the launch record. The lister resolves it from the data plane; the switch
    must use it, or the user's own dataset counts as someone else's and is
    switched to by UUID through the typed-dataset gate."""
    from uuid import uuid4

    switch = hook_module(suite, "switch-dataset.py")
    import _plugin_common as pc

    ident = str(uuid4())
    monkeypatch.setattr(switch, "load_resolved", lambda **kw: {})
    monkeypatch.setattr(
        switch,
        "list_writable_datasets",
        lambda *args: {
            "user_id": "writer",
            "datasets": [{"id": ident, "name": "owned", "owner_id": "writer", "writable": True}],
            "readonly": [],
        },
    )
    monkeypatch.setattr(
        pc,
        "require_typed_dataset_id_support",
        lambda **kw: (_ for _ in ()).throw(AssertionError("gate ran")),
    )
    monkeypatch.setattr(switch, "_sync_current", lambda *args: None)
    chosen = []

    def inspect(target):
        chosen.append(target)
        raise RuntimeError("inspection complete")

    monkeypatch.setattr(switch, "_ensure_dataset", inspect)
    with pytest.raises(RuntimeError, match="inspection complete"):
        switch._switch("host", {"session_id": "old", "dataset": "previous"}, ident, force=False)
    assert chosen == ["owned"]


def test_windows_lock_contention_retries_without_writing_locked_byte(
    suite, isolated_modules, monkeypatch
):
    import os
    import sys
    from types import SimpleNamespace

    pc = isolated_modules(suite, "_plugin_common")
    modes = []

    def lock(fd, mode, size):
        modes.append(mode)
        if len(modes) == 1:
            raise OSError("Another process holds the byte lock")

    def write(fd, content):
        raise PermissionError("Cannot write a byte locked by another process")

    fake_os = SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})
    fake_os.name, fake_os.write = "nt", write
    monkeypatch.setattr(pc, "os", fake_os)
    monkeypatch.setitem(
        sys.modules, "msvcrt", SimpleNamespace(LK_NBLCK=1, LK_UNLCK=2, locking=lock)
    )
    with pc.plugin_identity_lock(timeout=1):
        assert modes == [1, 1]
    assert modes == [1, 1, 2]
