"""Unit tests for the server-first recall helper (_recall_http.py).

Covers the contract from the PR reviews:
- a 2xx empty list is AUTHORITATIVE (not a fallback trigger);
- only a genuine connection failure -> UNREACHABLE (the *only* thing that lets
  cognee-search.sh fall back to the local CLI);
- any HTTP error (5xx/4xx, and especially 401/403 auth) -> an error envelope
  (dict, authoritative=False), NOT UNREACHABLE, so the wrapper reports it and
  does NOT fall back to a possibly-different local backend;
- top_k / scope coercion never raises.

Run: `pytest integrations/claude-code/tests/test_recall_http.py`
(or `python integrations/claude-code/tests/test_recall_http.py` standalone).
"""

import json
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
import _recall_http as rh  # noqa: E402


class _Resp:
    def __init__(self, payload):
        self._payload = payload.encode("utf-8") if isinstance(payload, str) else payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _returns(payload):
    def _opener(req, timeout=None):
        return _Resp(payload)

    return _opener


def _raises(exc):
    def _opener(req, timeout=None):
        raise exc

    return _opener


def test_empty_list_is_authoritative():
    # The whole point of the fix: server's empty list is a real answer, not fallback.
    assert rh.do_recall("http://x", "", "q", "", '["graph"]', "5", opener=_returns("[]")) == []


def test_list_results_passthrough():
    assert rh.do_recall(
        "http://x", "", "q", "", '["graph"]', "5", opener=_returns('[{"text": "hit"}]')
    ) == [{"text": "hit"}]


def test_non_error_dict_is_wrapped():
    assert rh.do_recall(
        "http://x", "", "q", "", '["graph"]', "5", opener=_returns('{"answer": "x"}')
    ) == [{"answer": "x"}]


def test_error_dict_is_error_envelope_not_fallback():
    out = rh.do_recall(
        "http://x", "", "q", "", '["graph"]', "5", opener=_returns('{"error": "bad request"}')
    )
    assert isinstance(out, dict) and out.get("authoritative") is False
    assert out != rh.UNREACHABLE  # must NOT trigger CLI fallback


def test_http_500_is_error_envelope():
    err = urllib.error.HTTPError("http://x", 500, "boom", {}, None)
    out = rh.do_recall("http://x", "", "q", "", '["graph"]', "5", opener=_raises(err))
    assert isinstance(out, dict) and out["status"] == 500 and out["authoritative"] is False
    assert out != rh.UNREACHABLE  # reachable-but-erroring must NOT fall back to local CLI


def test_http_401_403_auth_is_error_envelope_not_fallback():
    for code in (401, 403):
        err = urllib.error.HTTPError("http://x", code, "denied", {}, None)
        out = rh.do_recall("http://x", "k", "q", "", '["graph"]', "5", opener=_raises(err))
        assert isinstance(out, dict) and out["status"] == code
        # auth failure must NOT fall back to local CLI (would bypass authz / return wrong data)
        assert out != rh.UNREACHABLE


def test_connection_error_is_unreachable():
    # Only a genuine connection failure may fall back to the local CLI.
    assert (
        rh.do_recall(
            "http://x",
            "",
            "q",
            "",
            '["graph"]',
            "5",
            opener=_raises(urllib.error.URLError("refused")),
        )
        == rh.UNREACHABLE
    )


def test_malformed_json_is_error_not_unreachable():
    # A reachable server returning garbage is a SERVER bug → error envelope,
    # NOT UNREACHABLE (which would wrongly trigger the CLI fallback).
    out = rh.do_recall("http://x", "", "q", "", '["graph"]', "5", opener=_returns("not json{"))
    assert isinstance(out, dict) and out["authoritative"] is False
    assert out != rh.UNREACHABLE


def test_api_key_sent_to_any_target():
    captured = {}

    def _capture(req, timeout=None):
        captured["xapikey"] = req.get_header("X-api-key")  # urllib title-cases header names
        return _Resp("[]")

    # localhost target → key IS attached (cognee >=1.2.2 enforces auth even on
    # loopback; a server with auth disabled ignores the header)
    rh.do_recall("http://localhost:8011", "local-key", "q", "", '["graph"]', "5", opener=_capture)
    assert captured["xapikey"] == "local-key"
    # remote target → key IS attached
    rh.do_recall(
        "https://tenant.cognee.ai", "cloud-key", "q", "", '["graph"]', "5", opener=_capture
    )
    assert captured["xapikey"] == "cloud-key"
    # no key configured → header omitted entirely
    rh.do_recall("http://localhost:8011", "", "q", "", '["graph"]', "5", opener=_capture)
    assert captured["xapikey"] is None


def test_dataset_scoped_in_body():
    captured = {}

    def _capture(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp("[]")

    rh.do_recall("http://x", "", "q", "", '["graph"]', "5", "my_dataset", opener=_capture)
    assert captured["body"].get("datasets") == ["my_dataset"]


def test_no_dataset_key_when_empty():
    captured = {}

    def _capture(req, timeout=None):
        captured["body"] = json.loads(req.data)
        return _Resp("[]")

    rh.do_recall("http://x", "", "q", "", '["graph"]', "5", "", opener=_capture)
    assert "datasets" not in captured["body"]


def test_coerce_top_k():
    assert rh.coerce_top_k("abc") == 5
    assert rh.coerce_top_k("0") == 5
    assert rh.coerce_top_k("") == 5
    assert rh.coerce_top_k(None) == 5
    assert rh.coerce_top_k("10") == 10


def test_coerce_scope():
    assert rh.coerce_scope('["graph"]') == ["graph"]
    assert rh.coerce_scope("not json") == "auto"
    assert rh.coerce_scope("") == "auto"


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                failures += 1
                print("FAIL", name, e)
    sys.exit(1 if failures else 0)
