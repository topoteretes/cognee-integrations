import urllib.error

import pytest


def prepare_env(suite, isolated_modules, monkeypatch, tmp_path):
    pm = isolated_modules(suite, "_project_memory")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "key"))
    monkeypatch.setattr(common, "_PLUGIN_DIR", tmp_path)
    return pm, common


def test_auto_tag_distinguishes_same_named_projects(suite, isolated_modules, tmp_path):
    pm = isolated_modules(suite, "_project_memory")
    assert pm.project_tag(str(tmp_path / "one" / "repo"), "auto") != pm.project_tag(
        str(tmp_path / "two" / "repo"), "auto"
    )
    assert pm.project_tag(str(tmp_path), "fixed") == "fixed"
    assert pm.project_tag(str(tmp_path), "off") == ""


def test_unverified_companion_falls_back(suite, isolated_modules, monkeypatch, tmp_path):
    pm, common = prepare_env(suite, isolated_modules, monkeypatch, tmp_path)
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    pm.begin("primary", "s", str(tmp_path))
    with pytest.raises(RuntimeError, match="pending"):
        pm.route("primary", "s")

    def request(path, *args, **kwargs):
        if path.endswith("/datasets"):
            return [{"name": "primary", "id": "id"}]
        raise urllib.error.HTTPError("https://tenant", 403, "forbidden", {}, None)

    monkeypatch.setattr(common, "_json_http_request", request)
    pm.prepare("primary", "s")
    assert pm.route("primary", "s")["write"] == "primary"


def test_verified_routes_capture_improve_and_dual_graph_recall(
    suite, isolated_modules, monkeypatch, tmp_path
):
    pm, common = prepare_env(suite, isolated_modules, monkeypatch, tmp_path)
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", "project-fixed")
    pm.begin("primary", "s", str(tmp_path))
    calls = []

    def request(path, payload=None, **kwargs):
        calls.append((path, payload))
        if path == "/openapi.json":
            return {
                "components": {
                    "schemas": {
                        name: {"properties": {"node_set": {}}} for name in ("QAEntry", "TraceEntry")
                    }
                }
            }
        if path.endswith("/datasets"):
            return [{"name": "primary", "id": "id"}]
        if path.endswith("/session-companion"):
            return {
                "primary_dataset_id": "id",
                "dataset_id": "companion-id",
                "dataset_name": "primary-agent_sessions",
                "permissions_verified": True,
            }
        if path.endswith("/recall"):
            return [{"source": "graph_context", "content": payload["datasets"][0]}]
        return {"status": "session_stored"}

    monkeypatch.setattr(common, "_json_http_request", request)
    pm.prepare("primary", "s")
    common.remember_entry_via_http("primary", "s", {"type": "qa", "question": "q", "answer": "a"})
    write = calls[-1][1]
    assert write["dataset_name"] == "primary-agent_sessions"
    assert write["entry"]["node_set"] == ["project-fixed"]
    result = common.recall_via_http(
        "q", dataset="primary", session_id="s", top_k=3, scope=["graph"]
    )
    assert len(result) == 2
    assert calls[-1][1]["datasets"] == ["primary"]
    assert "session_id" not in calls[-1][1]
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "other"))
    with pytest.raises(RuntimeError, match="principal changed"):
        pm.route("primary", "s")


def test_old_server_never_silently_drops_project_tags(
    suite, isolated_modules, monkeypatch, tmp_path
):
    pm, common = prepare_env(suite, isolated_modules, monkeypatch, tmp_path)
    monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", "auto")
    pm.begin("primary", "s", str(tmp_path))
    monkeypatch.setattr(common, "_json_http_request", lambda *args, **kwargs: {})
    pm.prepare("primary", "s")
    with pytest.raises(RuntimeError, match="does not support"):
        common.remember_entry_via_http("primary", "s", {"type": "qa"})


def test_default_session_dataset_needs_no_companion(suite, isolated_modules, monkeypatch, tmp_path):
    pm, common = prepare_env(suite, isolated_modules, monkeypatch, tmp_path)
    monkeypatch.setenv("COGNEE_SESSION_COMPANION_DATASET", "true")
    pm.begin("agent_sessions", "s", str(tmp_path))
    assert pm.route("agent_sessions", "s")["write"] == "agent_sessions"
    assert not pm._path("agent_sessions", "s").exists()
