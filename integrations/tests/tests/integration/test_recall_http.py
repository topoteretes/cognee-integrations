"""Integration tests for the server-first recall helper (_recall_http.py).

These drive the real urllib stack against the mock Cognee server, so the
request that goes on the wire — URL, X-api-key header, JSON body — and the
handling of real HTTP status codes are what gets asserted, not a hand-rolled
fake response object.

Contract under test (from the PR reviews):
- a 2xx empty list is AUTHORITATIVE (not a fallback trigger);
- only a genuine connection failure -> UNREACHABLE (the *only* thing that lets
  cognee-search.sh fall back to the local CLI);
- any HTTP error (5xx/4xx, and especially 401/403 auth) -> an error envelope
  (dict, authoritative=False), NOT UNREACHABLE, so the wrapper reports it and
  does NOT fall back to a possibly-different local backend.

Migrated from claude-code/tests/test_recall_http.py (the module is identical in
all registered suites, so Codex and Antigravity gain this coverage).
Transport-exception classification lives in unit/test_recall_http_transport.py
— no server can raise those.
"""

from __future__ import annotations

import pytest

RECALL = "/api/v1/recall"


@pytest.fixture
def rh(suite, isolated_modules):
    return isolated_modules(suite, "_recall_http")


def _recall(rh, url, *, api_key="", query="q", dataset=""):
    """do_recall against a real server (no opener -> the module's HTTPS opener)."""
    return rh.do_recall(url, api_key, query, "", '["graph"]', "5", dataset)


# ── 2xx bodies ─────────────────────────────────────────────────────────────


def test_empty_list_is_authoritative(rh, mock_server):
    # The whole point of the fix: the server's empty list is a real answer.
    assert _recall(rh, mock_server.url) == []


def test_list_results_passthrough(rh, mock_server):
    mock_server.set_recall_results([{"text": "hit"}])
    assert _recall(rh, mock_server.url) == [{"text": "hit"}]


def test_non_error_dict_is_wrapped(rh, mock_server):
    mock_server.force_response("POST", RECALL, 200, {"answer": "x"})
    assert _recall(rh, mock_server.url) == [{"answer": "x"}]


def test_error_dict_is_error_envelope_not_fallback(rh, mock_server):
    mock_server.force_response("POST", RECALL, 200, {"error": "bad request"})
    out = _recall(rh, mock_server.url)
    assert isinstance(out, dict) and out.get("authoritative") is False
    assert out != rh.UNREACHABLE  # must NOT trigger CLI fallback


def test_malformed_json_is_error_not_unreachable(rh, mock_server):
    # A reachable server returning garbage is a SERVER bug -> error envelope,
    # NOT UNREACHABLE (which would wrongly trigger the CLI fallback).
    mock_server.force_response("POST", RECALL, 200, b"not json{")
    out = _recall(rh, mock_server.url)
    assert isinstance(out, dict) and out["authoritative"] is False
    assert out != rh.UNREACHABLE


# ── HTTP error statuses ────────────────────────────────────────────────────


def test_http_500_is_error_envelope(rh, mock_server):
    mock_server.force_response("POST", RECALL, 500, {"detail": "boom"})
    out = _recall(rh, mock_server.url)
    assert isinstance(out, dict) and out["status"] == 500 and out["authoritative"] is False
    # reachable-but-erroring must NOT fall back to the local CLI
    assert out != rh.UNREACHABLE


@pytest.mark.parametrize("code", [401, 403])
def test_auth_failure_is_error_envelope_not_fallback(rh, mock_server, code):
    mock_server.force_response("POST", RECALL, code, {"detail": "denied"})
    out = _recall(rh, mock_server.url, api_key="k")
    assert isinstance(out, dict) and out["status"] == code
    # auth failure must NOT fall back to local CLI (would bypass authz)
    assert out != rh.UNREACHABLE


def test_connection_refused_is_unreachable(rh, closed_port_url):
    """A genuinely absent server (nothing listening) is the one fallback trigger."""
    assert _recall(rh, closed_port_url) == rh.UNREACHABLE


# ── what actually goes on the wire ─────────────────────────────────────────


def test_api_key_sent_to_any_target(rh, mock_server):
    # cognee >=1.2.2 enforces auth even on loopback, so the key is always
    # attached (a server with auth disabled just ignores the header).
    _recall(rh, mock_server.url, api_key="local-key")
    call = mock_server.assert_called("POST", RECALL)
    assert call["headers"].get("X-Api-Key") == "local-key"


def test_api_key_header_omitted_when_unset(rh, mock_server):
    _recall(rh, mock_server.url, api_key="")
    call = mock_server.assert_called("POST", RECALL)
    assert "X-Api-Key" not in call["headers"]


def test_dataset_scoped_in_body(rh, mock_server):
    _recall(rh, mock_server.url, dataset="my_dataset")
    call = mock_server.assert_called("POST", RECALL)
    assert call["json"]["datasets"] == ["my_dataset"]


def test_no_dataset_key_when_empty(rh, mock_server):
    _recall(rh, mock_server.url, dataset="")
    call = mock_server.assert_called("POST", RECALL)
    assert "datasets" not in call["json"]


def test_request_body_carries_the_recall_contract(rh, mock_server):
    """top_k / scope / only_context are coerced before they hit the wire."""
    rh.do_recall(mock_server.url, "", "what did we decide?", "sess-1", '["graph"]', "7")
    call = mock_server.assert_called("POST", RECALL)
    assert call["json"]["query"] == "what did we decide?"
    assert call["json"]["session_id"] == "sess-1"
    assert call["json"]["top_k"] == 7
    assert call["json"]["scope"] == ["graph"]
    assert call["json"]["only_context"] is True
