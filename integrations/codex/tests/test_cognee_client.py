"""Unit tests for the cohesive recall client + circuit breaker (_cognee_client.py).

The breaker is file-based (each plugin hook is a short-lived process), so these
tests point COGNEE_PLUGIN_STATE_DIR at a temp dir and patch the transport
(`do_recall`) to drive each branch.

Run: `pytest integrations/codex/tests/test_cognee_client.py`
(or `python integrations/codex/tests/test_cognee_client.py` standalone).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts"))

import os  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cognee-breaker-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP

import _cognee_client as cc  # noqa: E402
from _recall_http import UNREACHABLE  # noqa: E402


def _reset():
    p = pathlib.Path(_TMP) / "recall-breaker.json"
    if p.exists():
        p.unlink()


def _stub(value):
    """Make the transport return a fixed value."""
    cc.do_recall = lambda *a, **k: value


def test_closed_passes_results_through():
    _reset()
    _stub([{"text": "hit"}])
    assert cc.recall("http://x", "", "q", "", '["graph"]', "5") == [{"text": "hit"}]
    assert cc.breaker_open()[0] is False


def test_opens_after_threshold_unreachable_then_short_circuits():
    _reset()
    _stub(UNREACHABLE)
    for _ in range(cc._THRESHOLD):
        cc.recall("http://x", "", "q", "", '["graph"]', "5")
    is_open, retry = cc.breaker_open()
    assert is_open and retry > 0

    # While open, recall must NOT call the transport and must surface a 503 envelope.
    def _boom(*a, **k):
        raise AssertionError("transport must not be called while breaker is open")

    cc.do_recall = _boom
    out = cc.recall("http://x", "", "q", "", '["graph"]', "5")
    assert isinstance(out, dict) and out["status"] == 503 and out["authoritative"] is False


def test_5xx_trips_breaker():
    _reset()
    _stub({"error": "boom", "status": 503, "authoritative": False})
    for _ in range(cc._THRESHOLD):
        cc.recall("http://x", "", "q", "", '["graph"]', "5")
    assert cc.breaker_open()[0] is True


def test_auth_4xx_does_not_trip():
    _reset()
    _stub({"error": "unauthorized", "status": 403, "authoritative": False})
    for _ in range(cc._THRESHOLD + 2):
        cc.recall("http://x", "k", "q", "", '["graph"]', "5")
    assert cc.breaker_open()[0] is False  # config problem, not a backend outage


def test_empty_list_is_success_not_failure():
    _reset()
    _stub([])
    for _ in range(cc._THRESHOLD + 2):
        cc.recall("http://x", "", "q", "", '["graph"]', "5")
    assert cc.breaker_open()[0] is False


def test_resets_after_cooldown():
    _reset()
    now = 1000.0
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", now=now)
    assert cc.breaker_open(now=now)[0] is True
    assert cc.breaker_open(now=now + cc._COOLDOWN + 1)[0] is False


def test_record_success_clears():
    _reset()
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x")
    assert cc.breaker_open()[0] is True
    cc.record_success()
    assert cc.breaker_open()[0] is False


def test_dataset_forwarded_to_transport():
    _reset()
    captured = {}

    def _capture(*a, **k):
        captured["dataset"] = a[6] if len(a) > 6 else k.get("dataset", "")
        return []

    cc.do_recall = _capture
    cc.recall("http://x", "", "q", "", '["graph"]', "5", "my_dataset")
    assert captured.get("dataset") == "my_dataset"


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
