"""The hook-side recall client always names its dataset
(_plugin_common.recall_via_http).

``/api/v1/recall`` was the only data-plane call omitting a dataset, which left
the graph scope dependent on the session's dataset binding — an unbound session
with several readable datasets is rejected as ambiguous rather than searched.
Naming the dataset makes the scope explicit.

Asserted on the wire against the mock server, so the body the server actually
receives is what the test reads.

Migrated from {claude-code,codex}/tests/test_improve_session_lock.py (whose
lock half is now unit/test_improve_session_lock.py).
"""

from __future__ import annotations

import pytest

RECALL = "/api/v1/recall"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setattr(common, "hook_log", lambda *a, **kw: None)
    return common


def _recall(pc, **kwargs):
    return pc.recall_via_http("q", session_id="s", top_k=5, scope=["graph"], **kwargs)


def test_recall_sends_the_dataset(pc, mock_server):
    assert _recall(pc, dataset="agent_sessions") == []
    call = mock_server.assert_called("POST", RECALL)
    assert call["json"]["datasets"] == ["agent_sessions"]


def test_recall_omits_datasets_key_when_no_dataset_known(pc, mock_server):
    """No dataset => no key, rather than a null/empty list the server must parse."""
    _recall(pc)
    call = mock_server.assert_called("POST", RECALL)
    assert "datasets" not in call["json"]


def test_recall_still_sends_search_type_and_scope(pc, mock_server):
    _recall(pc, dataset="ds", search_type="HYBRID_COMPLETION")
    body = mock_server.assert_called("POST", RECALL)["json"]
    assert body["datasets"] == ["ds"] and body["search_type"] == "HYBRID_COMPLETION"
    assert body["scope"] == ["graph"] and body["only_context"] is True
    assert body["query"] == "q" and body["session_id"] == "s" and body["top_k"] == 5


def test_recall_returns_the_servers_results(pc, mock_server):
    mock_server.set_recall_results([{"text": "remembered"}])
    assert _recall(pc, dataset="ds") == [{"text": "remembered"}]


def test_non_list_response_is_coerced_to_empty(pc, mock_server):
    """The client contract is a list; anything else must not leak upward."""
    mock_server.force_response("POST", RECALL, 200, {"unexpected": "shape"})
    assert _recall(pc, dataset="ds") == []


OPENAPI = "/openapi.json"


def _spec(*recall_fields):
    """An /openapi.json whose recall request body lists ``recall_fields``."""
    ref = "#/components/schemas/RecallPayloadDTO"
    return {
        "paths": {
            RECALL: {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": {"$ref": ref}}}}
                }
            }
        },
        "components": {
            "schemas": {"RecallPayloadDTO": {"properties": {f: {} for f in recall_fields}}}
        },
    }


@pytest.fixture
def marker(pc, tmp_path, monkeypatch):
    path = tmp_path / "recall-session-history.json"
    monkeypatch.setattr(pc, "_RECALL_SESSION_HISTORY_MARKER", path)
    return path


def _openapi_calls(mock_server):
    return [c for c in mock_server.calls if c["method"] == "GET" and c["path"] == OPENAPI]


def _health(mock_server, version):
    mock_server.force_response("GET", "/health", 200, {"status": "healthy", "version": version})


def test_recall_without_session_history_attributes_the_session(pc, mock_server, marker):
    """A server that knows the field gets the session for attribution only."""
    _health(mock_server, "1.6.7")
    mock_server.force_response("GET", OPENAPI, 200, _spec("sessionId", "sessionHistory"))
    pc.refresh_recall_session_history_support()

    _recall(pc, dataset="ds", session_history=False)

    body = mock_server.assert_called("POST", RECALL)["json"]
    assert body["session_id"] == "s"
    assert body["session_history"] is False


def test_recall_drops_the_session_on_a_server_without_session_history(pc, mock_server, marker):
    """An older server would ignore the field and put the history in the prompt."""
    _health(mock_server, "1.6.6")
    mock_server.force_response("GET", OPENAPI, 200, _spec("sessionId"))
    pc.refresh_recall_session_history_support()

    _recall(pc, dataset="ds", session_history=False)

    body = mock_server.assert_called("POST", RECALL)["json"]
    assert "session_id" not in body
    assert "session_history" not in body


def test_recall_never_probes_the_server_itself(pc, mock_server, marker):
    """The prompt path only reads the recorded verdict: a probe there would add a
    request to every prompt's deadline. No verdict reads as unsupported."""
    _recall(pc, dataset="ds", session_history=False)

    assert "session_id" not in mock_server.assert_called("POST", RECALL)["json"]
    assert _openapi_calls(mock_server) == []


def test_recall_with_session_history_sends_the_session_without_the_flag(pc, mock_server, marker):
    _recall(pc, dataset="ds")

    body = mock_server.assert_called("POST", RECALL)["json"]
    assert body["session_id"] == "s"
    assert "session_history" not in body


def test_refresh_reads_the_spec_once_per_cognee_version(pc, mock_server, marker):
    _health(mock_server, "1.6.7")
    mock_server.force_response("GET", OPENAPI, 200, _spec("sessionHistory"))

    pc.refresh_recall_session_history_support()
    pc.refresh_recall_session_history_support()
    assert len(_openapi_calls(mock_server)) == 1

    _health(mock_server, "1.6.8")
    pc.refresh_recall_session_history_support()
    assert len(_openapi_calls(mock_server)) == 2


def test_a_failed_refresh_records_nothing(pc, mock_server, marker):
    _health(mock_server, "1.6.7")
    mock_server.force_response("GET", OPENAPI, 503, {"detail": "down"})

    with pytest.raises(Exception):
        pc.refresh_recall_session_history_support()

    assert not marker.exists()
