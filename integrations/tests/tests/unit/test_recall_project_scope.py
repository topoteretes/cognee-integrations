"""Graph recall is scoped to the session's project node set plus the shared sets.

A project named either by the pinned project tag (COGNEE_PROJECT_NODE_SET) or by
COGNEE_RECALL_PROJECT_NODE_SET filters the graph lane with
``node_name=[project, *shared]`` (OR). Session and trace scopes stay unfiltered,
COGNEE_RECALL_PROJECT_SCOPE=false disables the filter, and a session that names
no project recalls exactly as before.
"""

import pytest

OPENAPI = {
    "components": {
        "schemas": {name: {"properties": {"node_set": {}}} for name in ("QAEntry", "TraceEntry")}
    }
}
# A backend without typed-entry tagging: capture cannot be tagged there, but
# recall filtering must still work, since it only needs node_name.
OPENAPI_UNTAGGED = {"components": {"schemas": {"QAEntry": {"properties": {}}}}}


def _env(suite, isolated_modules, monkeypatch, tmp_path, tag, *, schema=OPENAPI, **env):
    pm = isolated_modules(suite, "_project_memory")
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "key"))
    monkeypatch.setattr(common, "_PLUGIN_DIR", tmp_path)
    # Environment is set after the modules exist: the isolation fixture starts
    # every suite from a clean environment.
    if tag:
        monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", tag)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    pm.begin("primary", "s", str(tmp_path))
    calls = []

    def request(path, payload=None, **kwargs):
        calls.append((path, payload))
        if path == "/openapi.json":
            return schema
        return []

    monkeypatch.setattr(common, "_json_http_request", request)
    pm.prepare("primary", "s")
    return common, calls


def _recall(common, scope):
    return common.recall_via_http("q", dataset="primary", session_id="s", top_k=3, scope=scope)


def test_graph_scope_is_filtered_to_project_and_shared_sets(
    suite, isolated_modules, monkeypatch, tmp_path
):
    common, calls = _env(suite, isolated_modules, monkeypatch, tmp_path, "project-fixed")
    _recall(common, ["graph"])
    payload = calls[-1][1]
    assert payload["node_name"] == ["project-fixed", "global"]
    assert payload["node_name_filter_operator"] == "OR"


def test_session_scopes_stay_unfiltered(suite, isolated_modules, monkeypatch, tmp_path):
    common, calls = _env(suite, isolated_modules, monkeypatch, tmp_path, "project-fixed")
    _recall(common, ["session"])
    assert "node_name" not in calls[-1][1]


def test_shared_node_sets_are_configurable(suite, isolated_modules, monkeypatch, tmp_path):
    common, calls = _env(
        suite,
        isolated_modules,
        monkeypatch,
        tmp_path,
        "project-fixed",
        COGNEE_RECALL_SHARED_NODE_SETS="team, house-rules",
    )
    _recall(common, ["graph"])
    assert calls[-1][1]["node_name"] == ["project-fixed", "team", "house-rules"]


def test_scope_can_be_disabled_while_capture_stays_tagged(
    suite, isolated_modules, monkeypatch, tmp_path
):
    common, calls = _env(
        suite,
        isolated_modules,
        monkeypatch,
        tmp_path,
        "project-fixed",
        COGNEE_RECALL_PROJECT_SCOPE="false",
    )
    _recall(common, ["graph"])
    assert "node_name" not in calls[-1][1]


def test_no_project_means_no_filter(suite, isolated_modules, monkeypatch, tmp_path):
    common, calls = _env(suite, isolated_modules, monkeypatch, tmp_path, "")
    _recall(common, ["graph"])
    assert "node_name" not in calls[-1][1]


def test_recall_only_variable_scopes_without_a_pinned_tag(
    suite, isolated_modules, monkeypatch, tmp_path
):
    """The point of the recall-only name: no typed-entry tagging required."""
    common, calls = _env(
        suite,
        isolated_modules,
        monkeypatch,
        tmp_path,
        "",
        schema=OPENAPI_UNTAGGED,
        COGNEE_RECALL_PROJECT_NODE_SET="project-fixed",
    )
    _recall(common, ["graph"])
    payload = calls[-1][1]
    assert payload["node_name"] == ["project-fixed", "global"]
    assert payload["node_name_filter_operator"] == "OR"


def test_pinned_tag_wins_over_the_recall_only_variable(
    suite, isolated_modules, monkeypatch, tmp_path
):
    common, calls = _env(
        suite,
        isolated_modules,
        monkeypatch,
        tmp_path,
        "pinned",
        COGNEE_RECALL_PROJECT_NODE_SET="fallback",
    )
    _recall(common, ["graph"])
    assert calls[-1][1]["node_name"] == ["pinned", "global"]


@pytest.mark.parametrize("value", ["auto", "off", "0", "false", "no", "   "])
def test_recall_only_variable_ignores_non_names(
    suite, isolated_modules, monkeypatch, tmp_path, value
):
    common, calls = _env(
        suite,
        isolated_modules,
        monkeypatch,
        tmp_path,
        "",
        schema=OPENAPI_UNTAGGED,
        COGNEE_RECALL_PROJECT_NODE_SET=value,
    )
    _recall(common, ["graph"])
    assert "node_name" not in calls[-1][1]
