"""Unit tests for the server-first remember client (_remember_http.py).

Covers the two fixes:
  * remember posts with run_in_background=true by default (opt out via
    COGNEE_REMEMBER_BACKGROUND=false) so the agent turn isn't blocked on a
    synchronous cognify;
  * a write *timeout* is surfaced as a non-fatal note (NOT UNREACHABLE), so the
    caller does not fall back to the CLI and risk a duplicate write — while a real
    connection failure still returns UNREACHABLE.

Run: python integrations/claude-code/tests/test_remember_http.py (or via pytest).
"""

import os
import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _remember_http as rh  # noqa: E402


class _Resp:
    def __init__(self, body=b"{}"):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _capturing_opener(captured, body=b"{}"):
    def _open(req, timeout=None):
        captured["req"] = req
        return _Resp(body)

    return _open


def test_background_flag_default_true():
    os.environ.pop("COGNEE_REMEMBER_BACKGROUND", None)
    assert rh._background_flag() == "true"


def test_background_flag_opt_out():
    try:
        for v in ("false", "0", "no", "off", "FALSE"):
            os.environ["COGNEE_REMEMBER_BACKGROUND"] = v
            assert rh._background_flag() == "false"
    finally:
        os.environ.pop("COGNEE_REMEMBER_BACKGROUND", None)


def test_payload_sends_run_in_background_true():
    os.environ.pop("COGNEE_REMEMBER_BACKGROUND", None)
    cap = {}
    rh.do_remember("http://x", "", "content", "ds", "user_context", opener=_capturing_opener(cap))
    body = cap["req"].data
    assert b'name="run_in_background"\r\n\r\ntrue' in body


def test_timeout_does_not_fall_back():
    def _timeout_opener(req, timeout=None):
        raise TimeoutError("timed out")

    res = rh.do_remember("http://x", "", "c", "ds", "user_context", opener=_timeout_opener)
    assert res != rh.UNREACHABLE  # caller must NOT fall back to the CLI
    assert isinstance(res, dict) and "error" in res


def test_timeout_wrapped_in_urlerror_does_not_fall_back():
    def _wrapped(req, timeout=None):
        raise urllib.error.URLError(TimeoutError("timed out"))

    res = rh.do_remember("http://x", "", "c", "ds", "user_context", opener=_wrapped)
    assert res != rh.UNREACHABLE
    assert isinstance(res, dict) and "error" in res


def test_connection_failure_is_unreachable():
    def _refused(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    res = rh.do_remember("http://x", "", "c", "ds", "user_context", opener=_refused)
    assert res == rh.UNREACHABLE


def test_2xx_returns_ok():
    # body "{}" has no dataset_id, so the bounded wait is skipped.
    res = rh.do_remember("http://x", "", "c", "ds", "user_context", opener=_capturing_opener({}))
    assert res == {"ok": True}


def _seq_opener(post_body, status_bodies, counts=None):
    """Opener that returns post_body on the POST and successive status_bodies on GETs."""
    state = {"i": 0}

    def _open(req, timeout=None):
        method = getattr(req, "method", None) or req.get_method()
        if counts is not None:
            counts[method] = counts.get(method, 0) + 1
        if method == "POST":
            return _Resp(post_body)
        body = status_bodies[min(state["i"], len(status_bodies) - 1)]
        state["i"] += 1
        return _Resp(body)

    return _open


def test_response_body_parsed_into_result():
    os.environ["COGNEE_REMEMBER_WAIT_SECONDS"] = "0"  # isolate parsing from the poll
    try:
        body = b'{"status":"running","dataset_id":"d1","pipeline_run_id":"p1"}'
        res = rh.do_remember("http://x", "", "c", "ds", "uc", opener=_capturing_opener({}, body))
        assert res["ok"] is True
        assert res["dataset_id"] == "d1"
        assert res["pipeline_run_id"] == "p1"
        assert res["status"] == "running"
        assert "queryable" not in res  # wait disabled
    finally:
        os.environ.pop("COGNEE_REMEMBER_WAIT_SECONDS", None)


def test_unparseable_body_still_ok():
    res = rh.do_remember("http://x", "", "c", "ds", "uc", opener=_capturing_opener({}, b"not json"))
    assert res == {"ok": True}


def test_wait_zero_skips_poll():
    os.environ["COGNEE_REMEMBER_WAIT_SECONDS"] = "0"
    try:
        counts = {}
        body = b'{"status":"running","dataset_id":"d1"}'
        res = rh.do_remember(
            "http://x", "", "c", "ds", "uc", opener=_seq_opener(body, [b"{}"], counts)
        )
        assert counts.get("GET", 0) == 0  # no status poll issued
        assert "queryable" not in res
    finally:
        os.environ.pop("COGNEE_REMEMBER_WAIT_SECONDS", None)


def test_explicit_wait_completed():
    os.environ["COGNEE_REMEMBER_WAIT_SECONDS"] = "5"
    os.environ["COGNEE_COGNIFY_POLL_INTERVAL"] = "0.01"
    try:
        post = b'{"status":"running","dataset_id":"d1","pipeline_run_id":"p1"}'
        statuses = [
            b'{"d1":"DATASET_PROCESSING_STARTED"}',
            b'{"d1":"DATASET_PROCESSING_COMPLETED"}',
        ]
        res = rh.do_remember("http://x", "", "c", "ds", "uc", opener=_seq_opener(post, statuses))
        assert res["queryable"] is True
        assert res["wait_outcome"] == "completed"
    finally:
        os.environ.pop("COGNEE_REMEMBER_WAIT_SECONDS", None)
        os.environ.pop("COGNEE_COGNIFY_POLL_INTERVAL", None)


def test_explicit_wait_timeout():
    os.environ["COGNEE_REMEMBER_WAIT_SECONDS"] = "0.05"
    os.environ["COGNEE_COGNIFY_POLL_INTERVAL"] = "0.01"
    try:
        post = b'{"status":"running","dataset_id":"d1"}'
        statuses = [b'{"d1":"DATASET_PROCESSING_STARTED"}']  # never completes
        res = rh.do_remember("http://x", "", "c", "ds", "uc", opener=_seq_opener(post, statuses))
        assert res["queryable"] is False
        assert res["wait_outcome"] == "timeout"
    finally:
        os.environ.pop("COGNEE_REMEMBER_WAIT_SECONDS", None)
        os.environ.pop("COGNEE_COGNIFY_POLL_INTERVAL", None)


def test_remember_timeout_default_is_60():
    os.environ.pop("COGNEE_REMEMBER_TIMEOUT", None)
    assert rh._remember_timeout() == 60.0


def test_remember_timeout_reads_env():
    try:
        os.environ["COGNEE_REMEMBER_TIMEOUT"] = "12.5"
        assert rh._remember_timeout() == 12.5
    finally:
        os.environ.pop("COGNEE_REMEMBER_TIMEOUT", None)


def test_remember_timeout_bad_value_falls_back_to_default():
    try:
        os.environ["COGNEE_REMEMBER_TIMEOUT"] = "not-a-number"
        assert rh._remember_timeout() == 60.0
    finally:
        os.environ.pop("COGNEE_REMEMBER_TIMEOUT", None)


def test_main_passes_remember_timeout_to_do_remember():
    captured = {}
    original = rh.do_remember

    def _fake(*_args, **kwargs):
        captured["timeout"] = kwargs.get("timeout")
        return {"ok": True}

    rh.do_remember = _fake
    try:
        os.environ["COGNEE_REMEMBER_TIMEOUT"] = "7"
        rh.main(["http://x", "", "content", "ds", "ns"])
        assert captured["timeout"] == 7.0
    finally:
        rh.do_remember = original
        os.environ.pop("COGNEE_REMEMBER_TIMEOUT", None)


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
