"""Unit tests for the cognify status poll helper (_plugin_common.wait_for_cognify).

Confirms a background remember can be confirmed/abandoned correctly:
  * STARTED -> COMPLETED is reported as "completed"
  * ERRORED / deadline are distinguished (so the bridge can retry, not mark)
  * a 404 (older server without the status route) returns "unknown" immediately
  * a transient poll failure does not abort the whole deadline

Run: python integrations/claude-code/tests/test_cognify_poll.py (or via pytest).
"""

import pathlib
import sys
import urllib.error

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402

_ORIG = pc._json_http_request


def _fake(seq):
    """Replace _json_http_request with one returning/raising successive `seq` items."""
    state = {"i": 0}

    def _f(path, payload=None, *, method="GET", timeout=30.0):
        item = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    state["fn"] = _f
    pc._json_http_request = _f
    return state


def _restore():
    pc._json_http_request = _ORIG


def test_poll_started_then_completed():
    _fake(
        [
            {"d1": "DATASET_PROCESSING_STARTED"},
            {"d1": "DATASET_PROCESSING_STARTED"},
            {"d1": "DATASET_PROCESSING_COMPLETED"},
        ]
    )
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "completed"
    finally:
        _restore()


def test_poll_errored():
    _fake([{"d1": "DATASET_PROCESSING_ERRORED"}])
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "errored"
    finally:
        _restore()


def test_poll_timeout():
    _fake([{"d1": "DATASET_PROCESSING_STARTED"}])  # never completes
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=0.05, interval_seconds=0.01) == "timeout"
    finally:
        _restore()


def test_poll_404_unknown():
    err = urllib.error.HTTPError("http://x", 404, "Not Found", {}, None)
    _fake([err])
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "unknown"
    finally:
        _restore()


def test_poll_missing_dataset_id_unknown():
    called = {"n": 0}

    def _boom(*a, **k):
        called["n"] += 1
        raise AssertionError("should not poll without a dataset_id")

    pc._json_http_request = _boom
    try:
        assert pc.wait_for_cognify("", deadline_seconds=5.0) == "unknown"
        assert called["n"] == 0
    finally:
        _restore()


def test_poll_transient_then_completed():
    _fake([urllib.error.URLError("boom"), {"d1": "DATASET_PROCESSING_COMPLETED"}])
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "completed"
    finally:
        _restore()


def test_poll_nested_pipeline_shape():
    # Multi-pipeline responses nest {pipeline: status}; the helper must unwrap.
    _fake([{"d1": {"cognify_pipeline": "DATASET_PROCESSING_COMPLETED"}}])
    try:
        assert pc.wait_for_cognify("d1", deadline_seconds=5.0, interval_seconds=0.01) == "completed"
    finally:
        _restore()


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
