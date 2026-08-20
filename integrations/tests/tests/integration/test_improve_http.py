"""Integration tests for the session->graph improve submit
(_plugin_common.improve_session_via_http).

Driven against the mock server's real POST /api/v1/improve route, so the URL,
the JSON body, the generous submit timeout, and the real HTTP status taxonomy
are what get asserted — and the improve-unsupported marker is a real file under
the per-test HOME.

Contract:
  * POST /api/v1/improve with {dataset_name, session_ids, run_in_background};
  * a 2xx submit counts as success (improve is idempotent server-side);
  * 404/405/422 marks the server improve-unsupported (callers then fall back to
    the legacy full-document bridge);
  * an empty {} body means the per-session improve lock skipped the run — busy,
    never success;
  * a dataset_id in the response triggers best-effort cognify+memify polling
    (``has_improve_pipeline_polling`` is true for every shared suite — codex's
    improve path was the one piece the background-remember port missed, and it
    reported no cognify_status until that was fixed).

Migrated from {claude-code,codex}/tests/test_improve_sync.py; the
run_session_improve orchestration lives in unit/test_improve_orchestration.py.
"""

from __future__ import annotations

import pytest

IMPROVE = "/api/v1/improve"
STATUS = "/api/v1/datasets/status"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", "principal-key")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def test_improve_posts_expected_json_payload(pc, mock_server):
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is True

    call = mock_server.assert_called("POST", IMPROVE)
    assert call["json"] == {
        "dataset_name": "ds",
        "session_ids": ["sid"],
        "run_in_background": True,
    }
    assert call["headers"].get("X-Api-Key") == "principal-key"


def test_submit_timeout_is_generous(pc, monkeypatch):
    """Distillation/agent-context run inside the request even in background
    mode, so the submit timeout must stay generous (default 180s)."""
    timeouts = {}
    original = pc._json_http_request

    def _spy(path, payload=None, **kwargs):
        # Record per-path: the follow-on status polls use their own short timeout.
        timeouts.setdefault(path.split("?")[0], kwargs.get("timeout"))
        return original(path, payload, **kwargs)

    monkeypatch.setattr(pc, "_json_http_request", _spy)
    pc.improve_session_via_http("ds", "sid")
    assert timeouts[IMPROVE] >= 60


@pytest.mark.parametrize("code", [404, 405, 422])
def test_unsupported_statuses_mark_the_server(pc, mock_server, code):
    mock_server.force_response("POST", IMPROVE, code, {"detail": "no such route"})
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert res["unsupported"] is True
    # A real marker file landed, so later callers short-circuit to legacy.
    assert pc._IMPROVE_UNSUPPORTED_MARKER.exists()
    assert pc.improve_unsupported(mock_server.url) is True


def test_server_error_is_not_unsupported(pc, mock_server):
    """A transient 500 must not brand the server as lacking the endpoint."""
    mock_server.force_response("POST", IMPROVE, 500, {"detail": "boom"})
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert res.get("unsupported") is not True
    assert not pc._IMPROVE_UNSUPPORTED_MARKER.exists()


def test_improve_network_error_is_graceful(pc, closed_port_url, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert res["status"] == 0
    assert "error" in res


def test_improve_lock_skip_reports_busy(pc, mock_server):
    # improve() returns {} when the per-session lock skips the run — the helper
    # must surface that as busy, never as success.
    mock_server.set_improve_response({})
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert res["busy"] is True


def test_improve_polls_cognify_then_memify(pc, suite, mock_server):
    """A dataset_id in the response triggers both pipeline polls."""
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is True
    assert res["cognify_status"] == "completed"
    assert res["memify_status"] == "completed"

    pipelines = [c["query"].get("pipeline") for c in mock_server.calls if c["path"] == STATUS]
    assert pipelines == ["cognify_pipeline", "memify_pipeline"]
    assert suite.has_improve_pipeline_polling is True


def test_no_polling_when_response_has_no_dataset_id(pc, mock_server):
    mock_server.set_improve_response({"status": "running"})  # no dataset_id
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is True
    mock_server.assert_not_called("GET", STATUS)
