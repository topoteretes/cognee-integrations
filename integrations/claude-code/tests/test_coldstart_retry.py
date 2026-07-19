"""Unit tests for cold-start recall retry.

The first recall of a session often lands while the server is still warming, so
it times out even though a retry a moment later succeeds. These tests cover the
pure retry core (`retry_cold_start`) plus its integration into `_cognee_client.recall`:
first-recall gating, graceful degrade, no-retry on 4xx, budget-bounded backoff,
and breaker coherence (a retry burst is one failure). They also cover the
auto-recall hook's error contract (`coldstart_recall_attempt`) and the explicit
path's deadline bound.

State is file-based, so we point COGNEE_PLUGIN_STATE_DIR at a temp dir and patch
the transport (`do_recall`).

Run: `pytest integrations/claude-code/tests/test_coldstart_retry.py`
(or `python integrations/claude-code/tests/test_coldstart_retry.py` standalone).
"""

import pathlib
import sys
import tempfile
import time
import urllib.error

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


# ── auto-recall hook contract (coldstart_recall_attempt) ─────────────────────


def _raising_transport(values):
    """Return a do_call stub that raises each Exception in `values` and returns the rest."""
    seq = iter(values)
    calls = {"n": 0}

    def _call():
        calls["n"] += 1
        v = next(seq)
        if isinstance(v, BaseException):
            raise v
        return v

    return _call, calls


def test_hook_attempt_retries_on_connection_error_then_returns_context():
    """The hook's attempt: a timeout/connection error retries, then returns context."""
    call, calls = _raising_transport([urllib.error.URLError("refused"), [{"text": "hit"}]])
    attempt = cc.coldstart_recall_attempt(call)
    ok, value = cc.retry_cold_start(attempt, retries=2, backoff=0, sleep=lambda _d: None)
    assert ok is True and value == [{"text": "hit"}]
    assert calls["n"] == 2  # retried once


def test_hook_attempt_does_not_retry_httperror():
    """A reachable-but-rejected 4xx (HTTPError) fails fast: re-raised, never retried."""
    err = urllib.error.HTTPError("http://x", 403, "forbidden", {}, None)
    call, calls = _raising_transport([err, [{"text": "unreached"}]])
    attempt = cc.coldstart_recall_attempt(call)
    raised = False
    try:
        cc.retry_cold_start(attempt, retries=2, backoff=0, sleep=lambda _d: None)
    except urllib.error.HTTPError:
        raised = True
    assert raised and calls["n"] == 1  # failed fast, no retry


def test_hook_attempt_on_retry_receives_exception():
    """The on_retry hook fires with the caught exception before each retry."""
    seen = []
    call, _ = _raising_transport([TimeoutError("slow"), []])
    attempt = cc.coldstart_recall_attempt(call, on_retry=seen.append)
    cc.retry_cold_start(attempt, retries=1, backoff=0, sleep=lambda _d: None)
    assert len(seen) == 1 and isinstance(seen[0], TimeoutError)


def test_explicit_deadline_bounds_slow_retries():
    """A slow (full-timeout) first recall must not stack retries past the deadline."""
    _reset()
    calls = {"n": 0}

    def _slow(*a, **k):
        calls["n"] += 1
        time.sleep(0.05)  # each attempt consumes the whole (tiny) recall timeout
        return UNREACHABLE

    cc.do_recall = _slow
    out = cc.recall("http://x", "", "q", "sess-slow", '["graph"]', "5", timeout=0.05)
    assert out == UNREACHABLE  # graceful degrade
    assert calls["n"] == 1  # deadline stops it after one slow attempt (no ~3x block)


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
