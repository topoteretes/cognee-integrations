"""Unit tests for the cohesive recall client + circuit breaker (_cognee_client.py).

The breaker is file-based (each plugin hook is a short-lived process), so these
tests point COGNEE_PLUGIN_STATE_DIR at a temp dir and patch the transport
(`do_recall`) to drive each branch.

Breaker semantics under test (SDK-356):
  * state is keyed by base_url — one server's failures never open another's
  * failures only count inside a sliding window (no accumulation across days)
  * tripping consumes the counted failures (half-open: one post-cooldown
    failure starts a fresh count instead of instantly re-opening)
  * transient results (timeouts) are no-verdict: neither failure nor success
  * the trip reason (unreachable vs server_error) is recorded

Run: `pytest integrations/claude-code/tests/test_cognee_client.py`
(or `python integrations/claude-code/tests/test_cognee_client.py` standalone).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import os  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cognee-breaker-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP

import _cognee_client as cc  # noqa: E402
from _recall_http import UNREACHABLE  # noqa: E402

URL = "http://x"


def _reset():
    # Resolve through _state_path(): other test modules (e.g. test_doctor) also
    # set COGNEE_PLUGIN_STATE_DIR at import, and under pytest the last import
    # wins — unlinking a hardcoded _TMP path would silently stop resetting.
    p = cc._state_path()
    if p.exists():
        p.unlink()


def _stub(value):
    """Make the transport return a fixed value."""
    cc.do_recall = lambda *a, **k: value


def test_closed_passes_results_through():
    _reset()
    _stub([{"text": "hit"}])
    assert cc.recall(URL, "", "q", "", '["graph"]', "5") == [{"text": "hit"}]
    assert cc.breaker_open(URL)[0] is False


def test_opens_after_threshold_unreachable_then_short_circuits():
    _reset()
    _stub(UNREACHABLE)
    for _ in range(cc._THRESHOLD):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    is_open, retry = cc.breaker_open(URL)
    assert is_open and retry > 0
    assert cc.breaker_reason(URL) == "unreachable"

    # While open, recall must NOT call the transport and must surface a 503 envelope.
    def _boom(*a, **k):
        raise AssertionError("transport must not be called while breaker is open")

    cc.do_recall = _boom
    out = cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert isinstance(out, dict) and out["status"] == 503 and out["authoritative"] is False


def test_5xx_trips_breaker_with_server_error_reason():
    _reset()
    _stub({"error": "boom", "status": 503, "authoritative": False})
    for _ in range(cc._THRESHOLD):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is True
    assert cc.breaker_reason(URL) == "server_error"


def test_auth_4xx_does_not_trip():
    _reset()
    _stub({"error": "unauthorized", "status": 403, "authoritative": False})
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "k", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is False  # config problem, not a backend outage


def test_transient_timeout_is_no_verdict():
    """A timeout envelope neither trips the breaker nor clears prior failures."""
    _reset()
    for _ in range(cc._THRESHOLD - 1):
        cc.record_failure("x", service_url=URL)
    _stub(
        {"error": "recall slow: timed out", "status": 0, "authoritative": False, "transient": True}
    )
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    # Still one failure short of the threshold: transients counted as nothing.
    assert cc.breaker_open(URL)[0] is False
    cc.record_failure("x", service_url=URL)
    assert cc.breaker_open(URL)[0] is True


def test_empty_list_is_success_not_failure():
    _reset()
    _stub([])
    for _ in range(cc._THRESHOLD + 2):
        cc.recall(URL, "", "q", "", '["graph"]', "5")
    assert cc.breaker_open(URL)[0] is False


def test_failures_are_keyed_by_server():
    """Cloud failures must not open the breaker for a local server (or Codex's)."""
    _reset()
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", service_url="https://cloud.example")
    assert cc.breaker_open("https://cloud.example")[0] is True
    assert cc.breaker_open("http://localhost:8011")[0] is False
    # No-URL callers (doctor) still see the worst open entry.
    assert cc.breaker_open()[0] is True


def test_failures_outside_window_do_not_count():
    """Five blips spread over days must not open the breaker."""
    _reset()
    now = 1_000_000.0
    step = cc._WINDOW + 1
    for i in range(cc._THRESHOLD):
        cc.record_failure("x", now=now + i * step, service_url=URL)
    assert cc.breaker_open(URL, now=now + cc._THRESHOLD * step)[0] is False


def test_half_open_after_cooldown():
    """Tripping consumes the failures: one post-cooldown failure must not re-open."""
    _reset()
    now = 1000.0
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", now=now, service_url=URL)
    assert cc.breaker_open(URL, now=now)[0] is True
    after = now + cc._COOLDOWN + 1
    assert cc.breaker_open(URL, now=after)[0] is False
    cc.record_failure("x", now=after, service_url=URL)
    assert cc.breaker_open(URL, now=after)[0] is False  # fresh count, not instant re-open


def test_record_success_clears():
    _reset()
    for _ in range(cc._THRESHOLD):
        cc.record_failure("x", service_url=URL)
    assert cc.breaker_open(URL)[0] is True
    cc.record_success(URL)
    assert cc.breaker_open(URL)[0] is False


def test_legacy_flat_schema_is_discarded():
    """A pre-upgrade flat breaker file (machine-wide) must read as closed."""
    _reset()
    import json as _json
    import time as _time

    path = pathlib.Path(_TMP) / "recall-breaker.json"
    path.write_text(
        _json.dumps({"failures": 99, "cooldown_until": _time.time() + 3600}), encoding="utf-8"
    )
    assert cc.breaker_open(URL)[0] is False
    assert cc.breaker_open()[0] is False


def test_dataset_forwarded_to_transport():
    _reset()
    captured = {}

    def _capture(*a, **k):
        captured["dataset"] = a[6] if len(a) > 6 else k.get("dataset", "")
        return []

    cc.do_recall = _capture
    cc.recall(URL, "", "q", "", '["graph"]', "5", "my_dataset")
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
