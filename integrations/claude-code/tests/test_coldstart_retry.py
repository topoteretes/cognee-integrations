"""Unit tests for cold-start recall retry.

The first recall of a session often lands while the server is still warming, so
it times out even though a retry a moment later succeeds. These tests cover the
pure retry core (`retry_cold_start`) plus its integration into `_cognee_client.recall`:
first-recall gating, graceful degrade, no-retry on 4xx, budget-bounded backoff,
and breaker coherence (a retry burst is one failure).

State is file-based, so we point COGNEE_PLUGIN_STATE_DIR at a temp dir and patch
the transport (`do_recall`).

Run: `pytest integrations/claude-code/tests/test_coldstart_retry.py`
(or `python integrations/claude-code/tests/test_coldstart_retry.py` standalone).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import os  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="cognee-coldstart-test-")
os.environ["COGNEE_PLUGIN_STATE_DIR"] = _TMP
# Keep tests fast and deterministic: zero base backoff => no real sleeping.
os.environ["COGNEE_RECALL_COLDSTART_BACKOFF"] = "0"
os.environ["COGNEE_RECALL_COLDSTART_RETRIES"] = "2"

import _cognee_client as cc  # noqa: E402
from _recall_http import UNREACHABLE  # noqa: E402

_STATE = pathlib.Path(_TMP)


def _reset():
    for name in ("recall-breaker.json", "recall-coldstart.json"):
        p = _STATE / name
        if p.exists():
            p.unlink()


def _seq_transport(values):
    """Return a do_recall stub that yields `values` in order, plus a call counter."""
    calls = {"n": 0}

    def _stub(*a, **k):
        i = calls["n"]
        calls["n"] += 1
        return values[i] if i < len(values) else values[-1]

    return _stub, calls


# ── pure retry core ────────────────────────────────────────────────────────


def test_core_timeout_then_success():
    seq = iter([(False, "x"), (True, "hit")])
    ok, value = cc.retry_cold_start(lambda: next(seq), retries=2, backoff=0, sleep=lambda _d: None)
    assert ok is True and value == "hit"


def test_core_exhausts_and_returns_last():
    calls = {"n": 0}

    def _attempt():
        calls["n"] += 1
        return (False, "down")

    ok, value = cc.retry_cold_start(_attempt, retries=2, backoff=0, sleep=lambda _d: None)
    assert ok is False and value == "down"
    assert calls["n"] == 3  # 1 initial + 2 retries


def test_core_backoff_never_exceeds_budget():
    """With a fake clock advanced by sleep, total backoff stays within the deadline."""
    clock = {"t": 0.0}
    slept = []

    def _sleep(d):
        slept.append(d)
        clock["t"] += d

    ok, _ = cc.retry_cold_start(
        lambda: (False, None),
        retries=10,
        backoff=0.4,
        deadline=1.0,
        sleep=_sleep,
        rng=lambda: 0.5,  # jitter factor 1.0
        monotonic=lambda: clock["t"],
    )
    assert ok is False
    assert sum(slept) <= 1.0 + 1e-9  # never sleeps past the budget
    assert clock["t"] <= 1.0 + 1e-9


def test_first_recall_retries_then_returns_context():
    _reset()
    stub, calls = _seq_transport([UNREACHABLE, [{"text": "hit"}]])
    cc.do_recall = stub
    out = cc.recall("http://x", "", "q", "sess-A", '["graph"]', "5")
    assert out == [{"text": "hit"}]  # ultimately returns context
    assert calls["n"] == 2  # retried once
    assert cc.breaker_open()[0] is False  # success cleared the breaker


def test_exhausted_degrades_to_unreachable_no_error():
    _reset()
    stub, calls = _seq_transport([UNREACHABLE])
    cc.do_recall = stub
    out = cc.recall("http://x", "", "q", "sess-B", '["graph"]', "5")
    assert out == UNREACHABLE  # graceful degrade, no exception raised
    assert calls["n"] == 3  # 1 + 2 retries


def test_retry_burst_is_one_breaker_failure():
    _reset()
    stub, _ = _seq_transport([UNREACHABLE])
    cc.do_recall = stub
    cc.recall("http://x", "", "q", "sess-C", '["graph"]', "5")
    # Three transport calls, but the burst must count as a single failure.
    assert int(cc._read().get("failures") or 0) == 1


def test_4xx_envelope_is_not_retried():
    _reset()
    stub, calls = _seq_transport([{"error": "unauthorized", "status": 403, "authoritative": False}])
    cc.do_recall = stub
    out = cc.recall("http://x", "k", "q", "sess-D", '["graph"]', "5")
    assert isinstance(out, dict) and out["status"] == 403
    assert calls["n"] == 1  # reachable-but-rejected fails fast


def test_second_recall_same_session_does_not_retry():
    _reset()
    stub, calls = _seq_transport([UNREACHABLE, [{"text": "hit"}]])
    cc.do_recall = stub
    cc.recall("http://x", "", "q", "sess-E", '["graph"]', "5")  # first: retries
    first_calls = calls["n"]
    cc.recall("http://x", "", "q", "sess-E", '["graph"]', "5")  # second: single-shot
    assert calls["n"] == first_calls + 1  # exactly one more call, no retry


def test_no_session_id_is_single_shot():
    _reset()
    stub, calls = _seq_transport([UNREACHABLE])
    cc.do_recall = stub
    cc.recall("http://x", "", "q", "", '["graph"]', "5")
    assert calls["n"] == 1  # no session id => steady-state path, no retry


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
