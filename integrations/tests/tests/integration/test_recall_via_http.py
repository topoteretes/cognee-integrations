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
