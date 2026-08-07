"""Integration tests for the bridge's document submit
(_plugin_common._post_remember_document).

Driven against the mock server's real multipart POST /api/v1/remember route:
the NGINX-safe contract is that the submit runs in the background (so one
request is never held open for a full cognify) and that every failure mode
returns a uniform ``{"ok": ...}`` envelope instead of raising and aborting the
whole bridge.

Migrated from claude-code/tests/test_bridge_poll.py; the dedup/deadline state
machine around this call lives in unit/test_bridge_dedup.py.
"""

from __future__ import annotations

import pytest

REMEMBER = "/api/v1/remember"


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    if not suite.has_background_remember:
        pytest.skip(
            f"{suite.name}: _post_remember_document still posts synchronously and "
            "raises instead of returning an envelope"
        )
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def _post(pc, url, *, api_key="k", dataset="ds", document="doc", node_set="user_context"):
    return pc._post_remember_document(url, api_key, dataset, document, node_set, 30.0)


def test_background_submit_and_parses_ids(pc, mock_server):
    res = _post(pc, mock_server.url)
    assert res["ok"] is True
    assert res["dataset_id"] and res["pipeline_run_id"]

    call = mock_server.assert_called("POST", REMEMBER)
    # A real server parsed the multipart body the bridge built.
    assert call["form"]["run_in_background"] == "true"
    assert call["form"]["datasetName"] == "ds"
    assert call["form"]["node_set"] == "user_context"
    assert call["files"] == ["data"]


def test_http_error_returns_not_ok(pc, mock_server):
    # Non-2xx must be surfaced as {"ok": False} (graceful), never raised into
    # the bridge loop.
    mock_server.force_response("POST", REMEMBER, 503, {"detail": "Service Unavailable"})
    res = _post(pc, mock_server.url)
    assert res["ok"] is False
    assert res["status"] == 503
    assert "error" in res  # uniform: every failure carries a human-readable error


def test_parse_error_flag_on_unparseable_2xx(pc, mock_server):
    # 2xx with an unparseable body (e.g. a proxy error page) flags parse_error.
    mock_server.force_response("POST", REMEMBER, 200, b"<html>502 Bad Gateway</html>")
    res = _post(pc, mock_server.url)
    assert res["ok"] is True
    assert res.get("parse_error") is True
    assert res["dataset_id"] == ""
    assert res["status"] == 200  # uniform shape: parse_error carries status + error
    assert "error" in res


def test_network_error_returns_not_ok(pc, closed_port_url):
    # A connection failure during the POST must also be graceful, not propagate
    # and abort the whole bridge via the caller's outer handler.
    res = _post(pc, closed_port_url)
    assert res["ok"] is False
    assert "error" in res
    assert res["status"] == 0  # network-level failures use status 0
