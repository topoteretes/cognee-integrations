"""Pinned project datasets reach registration and every recall transport."""

from __future__ import annotations

import asyncio
import importlib
import sys
import types


def _track_project_derivation(monkeypatch):
    config = sys.modules.get("config") or importlib.import_module("config")
    calls = []
    monkeypatch.setenv("COGNEE_DATASET_SCOPE", "project")
    monkeypatch.setattr(
        config,
        "derive_project_dataset",
        lambda workspace: calls.append(workspace) or "project_wrong_999999999999",
    )
    return calls


def test_session_start_winner_reaches_ready_and_agent_registration(suite, hook_module, monkeypatch):
    session_start = hook_module(suite, "session-start.py")
    common = sys.modules["_plugin_common"]
    common.ensure_launch_record(
        "shared-host",
        "/winner",
        dataset="project_winner_111111111111",
        dataset_source="project",
    )
    session_id, conn_uuid = common.ensure_launch_record(
        "shared-host",
        "/loser",
        dataset="project_loser_222222222222",
        dataset_source="project",
    )
    winner, _source = common.get_launch_dataset("shared-host")
    observed = {}

    async def ready(config):
        observed["ready"] = config["dataset"]

    async def principal_key(_service_url, _config):
        return "api-key"

    async def user_id(_service_url, _api_key):
        return "user-id"

    def register_agent_via_http(**kwargs):
        observed["registration"] = kwargs["dataset_names"]
        return True, {"id": "connection-id"}

    async def ensure_dataset(_service_url, _api_key, dataset):
        observed["dataset_ready"] = dataset

    monkeypatch.setattr(session_start, "_reexec_into_venv", lambda: None)
    monkeypatch.setattr(session_start, "ensure_cognee_ready", ready)
    monkeypatch.setattr(session_start, "is_cloud_mode", lambda _config: True)
    monkeypatch.setattr(session_start, "_resolve_single_principal_key", principal_key)
    monkeypatch.setattr(session_start, "_user_id_via_api", user_id)
    monkeypatch.setattr(session_start, "ensure_dataset_ready_via_api", ensure_dataset)
    monkeypatch.setattr(session_start, "probe_health", lambda *_a, **_k: "ready")
    monkeypatch.setattr(session_start, "write_connection_state", lambda *_a, **_k: None)
    monkeypatch.setattr(common, "register_agent_via_http", register_agent_via_http)

    config = {
        "base_url": "https://cloud.example",
        "dataset": "project_loser_222222222222",
        "agent_name": "test-agent",
    }
    result = asyncio.run(
        session_start._run_heavy(
            config,
            "/loser",
            session_id,
            conn_uuid,
            "shared-host",
            winner,
            managed_endpoint=True,
            boot_timeout=1,
        )
    )

    assert result[2] is True
    assert observed == {
        "ready": winner,
        "registration": [winner],
        "dataset_ready": winner,
    }


def test_local_graph_recall_receives_pinned_dataset(suite, hook_module, monkeypatch):
    lookup = hook_module(suite, "session-context-lookup.py")
    calls = []
    derivations = _track_project_derivation(monkeypatch)

    async def recall(_prompt, **kwargs):
        calls.append(kwargs)
        return []

    fake_cognee = types.ModuleType("cognee")
    fake_cognee.recall = recall
    fake_types = types.ModuleType("cognee.modules.search.types")
    fake_types.SearchType = types.SimpleNamespace(HYBRID_COMPLETION="HYBRID_COMPLETION")
    monkeypatch.setitem(sys.modules, "cognee", fake_cognee)
    monkeypatch.setitem(sys.modules, "cognee.modules", types.ModuleType("cognee.modules"))
    monkeypatch.setitem(
        sys.modules, "cognee.modules.search", types.ModuleType("cognee.modules.search")
    )
    monkeypatch.setitem(sys.modules, "cognee.modules.search.types", fake_types)
    monkeypatch.setattr(lookup, "resolve_runtime_mode", lambda: {"mode": "local", "base_url": ""})
    monkeypatch.setattr(lookup, "read_connection_state", lambda: {})
    monkeypatch.setattr(lookup, "ensure_cognee_ready", lambda _config: asyncio.sleep(0))
    monkeypatch.setattr(lookup, "_load_session", lambda _workspace="": ("session-id", "pinned"))
    monkeypatch.setattr(lookup, "_load_user_id", lambda: "user-id")
    monkeypatch.setattr(lookup, "resolve_user", lambda _user_id: asyncio.sleep(0, result="user"))
    monkeypatch.setattr(lookup, "_recent_trace_fallback", lambda *_a: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(
        lookup,
        "read_and_reset_save_counter",
        lambda _session_id: {"prompt": 0, "trace": 0, "answer": 0},
    )
    monkeypatch.setattr(lookup, "hook_log", lambda *_a, **_k: None)
    if hasattr(lookup, "render_status_for_host"):
        monkeypatch.setattr(lookup, "render_status_for_host", lambda _session_id: "Cognee")

    asyncio.run(lookup._run("recall the pinned project", "/later"))

    graph = next(call for call in calls if call["scope"] == ["graph"])
    assert graph["datasets"] == ["pinned"]
    assert derivations == []


def test_prompt_hook_does_not_derive_after_loading_pinned_state(suite, hook_module, monkeypatch):
    prompt_hook = hook_module(suite, "store-user-prompt.py")
    derivations = _track_project_derivation(monkeypatch)
    monkeypatch.setattr(
        prompt_hook,
        "load_resolved",
        lambda: {
            "session_id": "session-id",
            "dataset": "project_pinned_111111111111",
            "user_id": "user-id",
            "tenant_id": "tenant-id",
        },
    )
    monkeypatch.setattr(
        prompt_hook,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "https://memory.example"},
    )
    monkeypatch.setattr(prompt_hook, "_ensure_idle_watcher", lambda *_a, **_k: None)
    monkeypatch.setattr(prompt_hook, "server_usable", lambda _url: False)

    asyncio.run(prompt_hook._store("remember the pinned decision", {}))

    assert derivations == []


def test_tool_and_answer_hooks_do_not_derive_after_loading_pinned_state(
    suite, hook_module, monkeypatch
):
    store_hook = hook_module(suite, "store-to-session.py")
    derivations = _track_project_derivation(monkeypatch)
    monkeypatch.setattr(
        store_hook,
        "_load_session",
        lambda: ("session-id", "project_pinned_111111111111", "user-id"),
    )
    monkeypatch.setattr(
        store_hook,
        "resolve_runtime_mode",
        lambda: {"mode": "http", "base_url": "https://memory.example"},
    )
    monkeypatch.setattr(store_hook, "server_usable", lambda _url: False)
    monkeypatch.setattr(store_hook, "append_warmup_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(store_hook, "append_http_bridge_entry", lambda *_a, **_k: None)
    monkeypatch.setattr(store_hook, "bump_save_counter", lambda *_a, **_k: None)
    monkeypatch.setattr(store_hook, "hook_log", lambda *_a, **_k: None)

    asyncio.run(
        store_hook._store_tool_call(
            {"tool_name": "Read", "tool_input": {"path": "README.md"}, "tool_output": "ok"}
        )
    )
    asyncio.run(
        store_hook._store_assistant_stop(
            {"assistant_message": "The pinned answer is stable", "turn_id": "turn-one"}
        )
    )

    assert derivations == []


def test_precompact_does_not_derive_after_loading_pinned_state(suite, hook_module, monkeypatch):
    precompact = hook_module(suite, "pre-compact.py")
    derivations = _track_project_derivation(monkeypatch)
    if suite.name == "claude-code":
        monkeypatch.setattr(
            precompact,
            "_load_resolved_fields",
            lambda: ("session-id", "project_pinned_111111111111"),
        )
    else:
        monkeypatch.setattr(
            precompact,
            "_load_resolved_fields",
            lambda: ("session-id", "project_pinned_111111111111", "user-id"),
        )
        monkeypatch.setattr(precompact, "_spawn_background_sync", lambda *_a, **_k: None)
    monkeypatch.setattr(precompact, "ensure_cognee_ready", lambda _config: asyncio.sleep(0))
    monkeypatch.setattr(precompact, "is_cloud_mode", lambda _config: True)
    monkeypatch.setattr(precompact, "_recall", lambda *_a, **_k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(precompact, "load_resolved", lambda: {})

    asyncio.run(precompact._run())

    assert derivations == []


def test_final_sync_does_not_derive_after_loading_pinned_state(suite, hook_module, monkeypatch):
    sync = hook_module(suite, "sync-session-to-graph.py")
    derivations = _track_project_derivation(monkeypatch)
    monkeypatch.setattr(
        sync,
        "_load_resolved",
        lambda: (
            "session-id",
            "project_pinned_111111111111",
            "user-id",
            "connection-id",
            True,
            True,
            "host-id",
        ),
    )
    monkeypatch.setattr(sync, "http_api_ready", lambda: True)
    monkeypatch.setattr(sync, "run_session_improve", lambda *_a, **_k: True)
    monkeypatch.setattr(sync, "hook_log", lambda *_a, **_k: None)

    asyncio.run(sync._sync(stop_watcher=False))

    assert derivations == []


def test_common_runtime_config_reads_do_not_derive_project_dataset(
    suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")
    derivations = _track_project_derivation(monkeypatch)

    assert common._resolve_agent_name()
    if suite.name == "codex":
        common._local_api_url_with_source()

    assert derivations == []


def test_precompact_http_recall_uses_resolved_dataset(suite, hook_module, monkeypatch):
    precompact = hook_module(suite, "pre-compact.py")
    observed = {}

    def recall_via_http(_query, **kwargs):
        observed.update(kwargs)
        return []

    monkeypatch.setattr(precompact, "is_cloud_mode", lambda _config: True)
    monkeypatch.setattr(precompact, "recall_via_http", recall_via_http)

    results = asyncio.run(
        precompact._recall(
            "session-id",
            "project_pinned_111111111111",
            query="project decision",
            scope=["graph"],
            top_k=3,
            config={"dataset": "project_later_222222222222"},
        )
    )

    assert results == []
    assert observed["dataset"] == "project_pinned_111111111111"
