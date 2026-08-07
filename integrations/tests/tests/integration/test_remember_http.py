"""Integration tests for the server-first remember client (_remember_http.py).

Driven against the mock Cognee server, so "the multipart form field arrived"
is proven by a real server parsing the body rather than by grepping the raw
bytes for a boundary.

Covers the two fixes:
  * remember posts with run_in_background=true by default (opt out via
    COGNEE_REMEMBER_BACKGROUND=false) so the agent turn isn't blocked on a
    synchronous cognify;
  * a write *timeout* is surfaced as a non-fatal note (NOT UNREACHABLE), so the
    caller does not fall back to the CLI and risk a duplicate write — while a
    real connection failure still returns UNREACHABLE.

The bounded cognify wait is claude-code-only (codex's do_remember is
submit-only), gated on ``suite.has_background_remember``.

Migrated from claude-code/tests/test_remember_http.py; the transport-exception
half lives in unit/test_remember_http_transport.py.
"""

from __future__ import annotations

import pytest

REMEMBER = "/api/v1/remember"
STATUS = "/api/v1/datasets/status"


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_remember_http")


def _remember(rh, url, *, api_key="", content="content", dataset="ds", node_set="user_context"):
    """do_remember against a real server (default opener = urllib.urlopen)."""
    return rh.do_remember(url, api_key, content, dataset, node_set)


# ── the write itself ───────────────────────────────────────────────────────


def test_server_receives_multipart_fields(rh, mock_server):
    res = _remember(rh, mock_server.url, dataset="agent_sessions", node_set="user_context")
    assert res["ok"] is True

    call = mock_server.assert_called("POST", REMEMBER)
    # A real server parsed these out of the multipart body.
    assert call["form"]["datasetName"] == "agent_sessions"
    assert call["form"]["node_set"] == "user_context"
    assert call["form"]["run_in_background"] == "true"
    assert call["files"] == ["data"]


def test_background_opt_out_reaches_the_wire(rh, mock_server, monkeypatch):
    monkeypatch.setenv("COGNEE_REMEMBER_BACKGROUND", "false")
    _remember(rh, mock_server.url)
    call = mock_server.assert_called("POST", REMEMBER)
    assert call["form"]["run_in_background"] == "false"


def test_api_key_header_attached(rh, mock_server):
    _remember(rh, mock_server.url, api_key="cloud-key")
    call = mock_server.assert_called("POST", REMEMBER)
    assert call["headers"].get("X-Api-Key") == "cloud-key"


def test_2xx_returns_ok(rh, mock_server):
    # The default mock body carries a dataset_id; the wait is disabled below so
    # this isolates "the server accepted the write".
    mock_server.force_response("POST", REMEMBER, 200, {})
    assert _remember(rh, mock_server.url) == {"ok": True}


def test_unparseable_body_still_ok(rh, mock_server):
    mock_server.force_response("POST", REMEMBER, 200, b"not json")
    assert _remember(rh, mock_server.url) == {"ok": True}


def test_error_in_2xx_body_is_an_error_envelope(rh, mock_server):
    mock_server.force_response("POST", REMEMBER, 200, {"error": "dataset locked"})
    res = _remember(rh, mock_server.url)
    assert res != rh.UNREACHABLE
    assert isinstance(res, dict) and "error" in res


@pytest.mark.parametrize("code", [401, 403, 500])
def test_http_error_is_not_unreachable(rh, mock_server, code):
    """A reachable-but-failing server must never trigger the CLI fallback."""
    mock_server.force_response("POST", REMEMBER, code, {"detail": "nope"})
    res = _remember(rh, mock_server.url)
    assert res != rh.UNREACHABLE
    assert isinstance(res, dict) and "error" in res


def test_connection_failure_is_unreachable(rh, closed_port_url):
    assert _remember(rh, closed_port_url) == rh.UNREACHABLE


# ── the bounded cognify wait (claude-code only) ────────────────────────────


@pytest.fixture
def waits(suite):
    if not suite.has_background_remember:
        pytest.skip(f"{suite.name}: do_remember is submit-only (no bounded cognify wait)")


def test_response_body_parsed_into_result(rh, mock_server, waits, monkeypatch):
    monkeypatch.setenv("COGNEE_REMEMBER_WAIT_SECONDS", "0")  # isolate parsing from the poll
    mock_server.force_response(
        "POST", REMEMBER, 200, {"status": "running", "dataset_id": "d1", "pipeline_run_id": "p1"}
    )
    res = _remember(rh, mock_server.url)
    assert res["ok"] is True
    assert res["dataset_id"] == "d1"
    assert res["pipeline_run_id"] == "p1"
    assert res["status"] == "running"
    assert "queryable" not in res  # wait disabled


def test_wait_zero_skips_poll(rh, mock_server, waits, monkeypatch):
    monkeypatch.setenv("COGNEE_REMEMBER_WAIT_SECONDS", "0")
    res = _remember(rh, mock_server.url)
    assert "queryable" not in res
    mock_server.assert_not_called("GET", STATUS)  # no status poll issued


def test_explicit_wait_completed(rh, mock_server, waits, monkeypatch):
    monkeypatch.setenv("COGNEE_REMEMBER_WAIT_SECONDS", "5")
    monkeypatch.setenv("COGNEE_COGNIFY_POLL_INTERVAL", "0.01")
    res = _remember(rh, mock_server.url)  # mock reports COMPLETED by default
    assert res["queryable"] is True
    assert res["wait_outcome"] == "completed"
    mock_server.assert_called("GET", STATUS)


def test_explicit_wait_timeout(rh, mock_server, waits, monkeypatch):
    from utils.mock_cognee import STATUS_PROCESSING

    monkeypatch.setenv("COGNEE_REMEMBER_WAIT_SECONDS", "0.05")
    monkeypatch.setenv("COGNEE_COGNIFY_POLL_INTERVAL", "0.01")
    mock_server.set_dataset_status(STATUS_PROCESSING)  # never completes
    res = _remember(rh, mock_server.url)
    assert res["queryable"] is False
    assert res["wait_outcome"] == "timeout"
