"""Integration tests for the session->graph improve submit
(_plugin_common.improve_session_via_http).

Driven against the mock server's real POST /api/v1/improve route, so the URL,
the JSON body, the generous submit timeout, and the real HTTP status taxonomy
are what get asserted.

Contract:
  * POST /api/v1/improve with {dataset_name, session_ids, run_in_background};
  * a 2xx submit counts as success (improve is idempotent server-side);
  * 404/405/422 is reported as ``unsupported`` and nothing else happens — the
    legacy full-document bridge that used to take over for 24h is gone;
  * an empty {} body means the per-session improve lock skipped the run — busy,
    never success;
  * a dataset_id in the response triggers best-effort cognify+memify polling
    (gated on ``has_improve_pipeline_polling``, now true for both suites — codex's
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
def test_unsupported_statuses_return_not_ok_without_fallback(pc, mock_server, code):
    mock_server.force_response("POST", IMPROVE, code, {"detail": "no such route"})
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert res["unsupported"] is True
    assert res["status"] == code
    # No marker, no memory of it: the next call asks the server again rather than
    # branding it for 24h, and nothing was re-posted through /remember.
    assert not (pc._SHARED_PLUGIN_ROOT / "improve-unsupported.json").exists()
    assert not hasattr(pc, "_IMPROVE_UNSUPPORTED_MARKER")
    mock_server.assert_not_called("POST", "/api/v1/remember")


@pytest.mark.parametrize("code", [404, 422])
def test_run_session_improve_reports_unsupported_and_does_not_bridge(
    pc, mock_server, monkeypatch, code
):
    """End to end through run_session_improve: the sync is reported as not done and
    the whole-transcript re-cognify that used to follow never happens."""
    mock_server.force_response("POST", IMPROVE, code, {"detail": "no such route"})
    monkeypatch.setattr(pc, "drain_warmup_entries", lambda *a, **k: (0, 0))
    events = []
    monkeypatch.setattr(pc, "hook_log", lambda ev, detail=None: events.append((ev, detail or {})))
    assert pc.run_session_improve("ds", "sid") is False
    assert any(ev == "improve_unsupported" and d.get("status") == code for ev, d in events)
    assert not any(ev == "improve_unsupported_fallback" for ev, _ in events)
    mock_server.assert_not_called("POST", "/api/v1/remember")
    assert pc.read_improve_state("sid") == {}


def test_server_error_is_not_unsupported(pc, mock_server):
    """A transient 500 must not be reported as a server lacking the endpoint."""
    mock_server.force_response("POST", IMPROVE, 500, {"detail": "boom"})
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is False
    assert "unsupported" not in res


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
    if not suite.has_improve_pipeline_polling:
        pytest.skip(f"{suite.name}: improve is submit-only (no cognify/memify polling)")

    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is True
    assert res["cognify_status"] == "completed"
    assert res["memify_status"] == "completed"

    pipelines = [c["query"].get("pipeline") for c in mock_server.calls if c["path"] == STATUS]
    assert pipelines == ["cognify_pipeline", "memify_pipeline"]


def test_no_polling_when_response_has_no_dataset_id(pc, mock_server):
    mock_server.set_improve_response({"status": "running"})  # no dataset_id
    res = pc.improve_session_via_http("ds", "sid")
    assert res["ok"] is True
    mock_server.assert_not_called("GET", STATUS)
