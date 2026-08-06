"""Unit tests for the session->graph bridge background+poll behavior
(_plugin_common._post_remember_document and persist_session_cache_to_graph_via_http).

Confirms the NGINX-safe contract:
  * the bridge POSTs run_in_background=true and parses the enqueue handle;
  * the SHA256 dedup digest is marked written ONLY when the graph is confirmed
    queryable (completed) or genuinely unpollable (unknown/no-id) — errored/timeout
    stay unmarked so the detached retry re-submits;
  * an already-synced document is not re-posted.

Run: python integrations/claude-code/tests/test_bridge_poll.py (or via pytest).
"""

import hashlib
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}", status=200):
        self._body = body
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def test_post_remember_document_background_and_parses_ids():
    captured = {}
    orig = urllib.request.urlopen

    def _fake(req, timeout=None, context=None):
        captured["req"] = req
        return _Resp(b'{"status":"running","dataset_id":"d1","pipeline_run_id":"p1"}')

    urllib.request.urlopen = _fake
    try:
        res = pc._post_remember_document("http://x", "k", "ds", "doc", "user_context", 30.0)
    finally:
        urllib.request.urlopen = orig
    assert b'name="run_in_background"\r\n\r\ntrue' in captured["req"].data
    assert res == {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}


def _run_bridge(
    outcome,
    *,
    post_result=None,
    post_results=None,
    preseed_state=None,
    docs=("qa text", ""),
    wait_sleep=0.0,
):
    """Drive persist_session_cache_to_graph_via_http with the HTTP seams mocked.

    `post_results` (a list) returns a different result per POST call, in order, to
    exercise one document failing while another succeeds. Returns
    (wrote, written_state, calls) where calls tracks post/wait invocations.
    """
    calls = {"post": 0, "wait": 0}
    written = {}
    saved = {
        k: getattr(pc, k)
        for k in (
            "_local_api_url",
            "_backend_reachable",
            "_api_key",
            "_format_cached_bridge_document",
            "_bridge_file",
            "_load_json_file",
            "_write_json_file",
            "_post_remember_document",
            "wait_for_cognify",
            "hook_log",
        )
    }

    def _post(*a, **k):
        calls["post"] += 1
        if post_results is not None:
            return post_results[min(calls["post"] - 1, len(post_results) - 1)]
        return post_result or {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}

    def _wait(*a, **k):
        calls["wait"] += 1
        if wait_sleep:
            time.sleep(wait_sleep)
        return outcome

    pc._local_api_url = lambda: "http://x"
    pc._backend_reachable = lambda url: True
    pc._api_key = lambda: "k"
    pc._format_cached_bridge_document = lambda dataset, sid: docs
    pc._bridge_file = lambda sid: pathlib.Path("/tmp/_bridge_test.json")
    pc._load_json_file = lambda p: {"_state": dict(preseed_state)} if preseed_state else {}
    pc._write_json_file = lambda p, data: written.update(data)
    pc._post_remember_document = _post
    pc.wait_for_cognify = _wait
    pc.hook_log = lambda *a, **k: None
    try:
        wrote = pc.persist_session_cache_to_graph_via_http("ds", "sid")
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)
    return wrote, written.get("_state", {}), calls


def test_dedup_marks_only_on_completed():
    wrote, state, calls = _run_bridge("completed")
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 1


def test_dedup_not_marked_on_errored():
    wrote, state, _ = _run_bridge("errored")
    assert wrote is False
    assert state == {}


def test_dedup_not_marked_on_timeout():
    wrote, state, _ = _run_bridge("timeout")
    assert wrote is False
    assert state == {}


def test_dedup_marked_on_unknown():
    wrote, state, _ = _run_bridge("unknown")
    assert wrote is True
    assert len(state) == 1


def test_no_dataset_id_marks_and_skips_poll():
    wrote, state, calls = _run_bridge(
        "completed", post_result={"ok": True, "dataset_id": "", "pipeline_run_id": ""}
    )
    assert wrote is True
    assert len(state) == 1
    assert calls["wait"] == 0  # nothing to poll without a dataset_id


def test_already_synced_skips_post():
    key = f"{pc._bridge_cache_key('ds', 'sid')}:qa"
    digest = hashlib.sha256("qa text".encode("utf-8")).hexdigest()
    wrote, state, calls = _run_bridge("completed", preseed_state={key: digest})
    assert calls["post"] == 0  # unchanged document is not re-posted
    assert wrote is False


def test_post_remember_document_http_error_returns_not_ok():
    # urlopen raises HTTPError on non-2xx; _post_remember_document must surface it as
    # {"ok": False} (graceful) rather than letting it crash the bridge.
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, context=None):
        raise urllib.error.HTTPError("http://x", 503, "Service Unavailable", {}, None)

    urllib.request.urlopen = _raise
    try:
        res = pc._post_remember_document("http://x", "k", "ds", "doc", "user_context", 30.0)
    finally:
        urllib.request.urlopen = orig
    assert res["ok"] is False
    assert res["status"] == 503
    assert "error" in res  # uniform: every failure carries a human-readable error


def test_post_remember_document_parse_error_flag():
    # 2xx with an unparseable body (e.g. a proxy error page) flags parse_error.
    orig = urllib.request.urlopen

    def _bad_json(req, timeout=None, context=None):
        return _Resp(b"<html>502 Bad Gateway</html>")

    urllib.request.urlopen = _bad_json
    try:
        res = pc._post_remember_document("http://x", "k", "ds", "doc", "user_context", 30.0)
    finally:
        urllib.request.urlopen = orig
    assert res["ok"] is True
    assert res.get("parse_error") is True
    assert res["dataset_id"] == ""
    assert res["status"] == 200  # uniform shape: parse_error carries status + error
    assert "error" in res


def test_post_remember_document_network_error_returns_not_ok():
    # A URLError/timeout during the POST must also be graceful, not propagate and
    # abort the whole bridge via the caller's outer handler.
    orig = urllib.request.urlopen

    def _raise(req, timeout=None, context=None):
        raise urllib.error.URLError("connection timed out")

    urllib.request.urlopen = _raise
    try:
        res = pc._post_remember_document("http://x", "k", "ds", "doc", "user_context", 30.0)
    finally:
        urllib.request.urlopen = orig
    assert res["ok"] is False
    assert "error" in res
    assert res["status"] == 0  # network-level failures use status 0


def test_parse_error_not_marked():
    # An unparseable 2xx must NOT be marked written (it's retried), unlike a valid
    # response with no dataset_id.
    wrote, state, calls = _run_bridge(
        "completed", post_result={"ok": True, "dataset_id": "", "parse_error": True}
    )
    assert wrote is False
    assert state == {}
    assert calls["wait"] == 0


def test_overall_deadline_across_documents():
    # poll_deadline is an overall budget: once the first document's wait exhausts it,
    # the second document is skipped (not given a fresh full deadline).
    os.environ["COGNEE_BRIDGE_POLL_DEADLINE"] = "0.01"
    try:
        wrote, state, calls = _run_bridge(
            "completed", docs=("qa text", "trace text"), wait_sleep=0.05
        )
    finally:
        os.environ.pop("COGNEE_BRIDGE_POLL_DEADLINE", None)
    assert calls["post"] == 1  # second document skipped once the budget is spent
    assert calls["wait"] == 1
    assert len(state) == 1  # only the first document synced
    assert wrote is True


def test_post_failure_skips_document():
    # A failing POST leaves the digest unmarked (retried later) and does not crash.
    wrote, state, calls = _run_bridge("completed", post_result={"ok": False, "status": 500})
    assert wrote is False
    assert state == {}
    assert calls["wait"] == 0  # never polled — the submit failed


def test_one_doc_fails_other_continues():
    # First document (qa) fails its POST; the second (trace) must still sync.
    wrote, state, calls = _run_bridge(
        "completed",
        docs=("qa text", "trace text"),
        post_results=[
            {"ok": False, "status": 503},
            {"ok": True, "dataset_id": "d2", "pipeline_run_id": "p2"},
        ],
    )
    assert calls["post"] == 2  # both attempted; the first failure didn't abort the loop
    assert calls["wait"] == 1  # only the successful submit was polled
    assert wrote is True
    assert len(state) == 1  # only the trace document was marked written


if __name__ == "__main__":
    failures = 0
    for _name, _fn in sorted(globals().items()):
        if _name.startswith("test_") and callable(_fn):
            try:
                _fn()
                print("PASS", _name)
            except AssertionError as exc:
                failures += 1
                print("FAIL", _name, exc)
    sys.exit(1 if failures else 0)
