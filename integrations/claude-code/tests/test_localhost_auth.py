"""Regression tests for issue #106: the X-Api-Key header must be sent to a
localhost server when a key is present.

The client used to strip ``X-Api-Key`` for loopback targets (``if api_key and
not _is_local(...)``) on the assumption that the local single-user server needs
no auth. The bundled local server *does* require the header and returns HTTP 401
without it, so every ``cognee-remember`` write failed. The fix attaches the key
whenever one is present; a server that ignores auth simply ignores the header.

These tests capture the outgoing ``urllib.request.Request`` via the injectable
``opener`` and assert the header is present for a ``localhost`` URL.

Run: python integrations/claude-code/tests/test_localhost_auth.py (or via pytest).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _recall_http as recall  # noqa: E402
import _remember_http as remember  # noqa: E402

# urllib capitalizes header keys, so "X-Api-Key" is stored as "X-api-key".
_HEADER = "X-api-key"
_LOCAL = "http://localhost:8011"
_KEY = "ck_secret_local"


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
        # Record every request issued (POST then any status GETs).
        captured.setdefault("reqs", []).append(req)
        captured["req"] = req
        return _Resp(body)

    return _open


# --- remember (write) path -------------------------------------------------


def test_remember_attaches_key_on_localhost():
    """The core bug: a write to localhost must carry X-Api-Key when a key is set."""
    cap = {}
    remember.do_remember(_LOCAL, _KEY, "content", "ds", "user_context", opener=_capturing_opener(cap))
    assert cap["req"].get_header(_HEADER) == _KEY


def test_remember_no_key_sends_no_header():
    """No key configured → no header (unchanged behavior, no accidental empty header)."""
    cap = {}
    remember.do_remember(_LOCAL, "", "content", "ds", "user_context", opener=_capturing_opener(cap))
    assert cap["req"].get_header(_HEADER) is None


def test_remember_attaches_key_on_remote():
    """Cloud/remote target keeps working — key still attached."""
    cap = {}
    remember.do_remember(
        "https://api.cognee.ai", _KEY, "content", "ds", "user_context", opener=_capturing_opener(cap)
    )
    assert cap["req"].get_header(_HEADER) == _KEY


def test_remember_status_poll_attaches_key_on_localhost():
    """The status-poll GET (cognify confirmation) must also be authenticated on localhost."""
    cap = {}
    # A response carrying dataset_id triggers the bounded status poll.
    body = b'{"status":"running","dataset_id":"d1","pipeline_run_id":"p1"}'
    import os

    os.environ["COGNEE_REMEMBER_WAIT_SECONDS"] = "0.05"
    os.environ["COGNEE_COGNIFY_POLL_INTERVAL"] = "0.01"
    try:
        remember.do_remember(_LOCAL, _KEY, "c", "ds", "uc", opener=_capturing_opener(cap, body))
    finally:
        os.environ.pop("COGNEE_REMEMBER_WAIT_SECONDS", None)
        os.environ.pop("COGNEE_COGNIFY_POLL_INTERVAL", None)
    methods = [getattr(r, "method", None) or r.get_method() for r in cap["reqs"]]
    assert "GET" in methods, "expected a status-poll GET to be issued"
    for r in cap["reqs"]:
        assert r.get_header(_HEADER) == _KEY


# --- recall (read) path ----------------------------------------------------


def test_recall_attaches_key_on_localhost():
    """Recall against an authenticated localhost server must carry the key too."""
    cap = {}
    recall.do_recall(_LOCAL, _KEY, "q", "", '["graph"]', "5", opener=_capturing_opener(cap, b"[]"))
    assert cap["req"].get_header(_HEADER) == _KEY


def test_recall_no_key_sends_no_header():
    cap = {}
    recall.do_recall(_LOCAL, "", "q", "", '["graph"]', "5", opener=_capturing_opener(cap, b"[]"))
    assert cap["req"].get_header(_HEADER) is None


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
