"""Unit tests for observability timing (#3676): elapsed_ms on recall / bridge / improve.

Covers:
  * elapsed_ms(): monotonic-based, integer milliseconds, non-negative.
  * persist_session_cache_to_graph_via_http: http_bridge_poll carries elapsed_ms.
  * the failure path (http_bridge_post_failed) also carries elapsed_ms, so a
    slow-failing submit is still visible in latency logs.

Run: python integrations/claude-code/tests/test_hook_timing.py (or via pytest).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def test_elapsed_ms_is_non_negative_int():
    start = pc.time.monotonic()
    value = pc.elapsed_ms(start)
    assert isinstance(value, int)
    assert value >= 0


def test_elapsed_ms_measures_delta():
    # Drive time.monotonic() deterministically: start at 100.0, read at 100.25 -> 250 ms.
    seq = iter([100.0, 100.25])
    orig = pc.time.monotonic
    pc.time.monotonic = lambda: next(seq)
    try:
        start = pc.time.monotonic()
        assert pc.elapsed_ms(start) == 250
    finally:
        pc.time.monotonic = orig


def _run_bridge(outcome, *, post_result=None):
    """Drive persist_session_cache_to_graph_via_http with HTTP seams mocked,
    capturing every hook_log(event, detail) call for assertion."""
    events = []
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
    pc._local_api_url = lambda: "http://x"
    pc._backend_reachable = lambda url: True
    pc._api_key = lambda: "k"
    pc._format_cached_bridge_document = lambda dataset, sid: ("qa text", "")
    pc._bridge_file = lambda sid: pathlib.Path("/tmp/_bridge_timing_test.json")
    pc._load_json_file = lambda p: {}
    pc._write_json_file = lambda p, data: None
    pc._post_remember_document = lambda *a, **k: (
        post_result or {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"}
    )
    pc.wait_for_cognify = lambda *a, **k: outcome
    pc.hook_log = lambda event, detail=None: events.append((event, detail or {}))
    try:
        pc.persist_session_cache_to_graph_via_http("ds", "sid")
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)
    return events


def _detail_for(events, event_name):
    for name, detail in events:
        if name == event_name:
            return detail
    return None


def test_http_bridge_poll_carries_elapsed_ms():
    detail = _detail_for(_run_bridge("completed"), "http_bridge_poll")
    assert detail is not None, "expected an http_bridge_poll event"
    assert "elapsed_ms" in detail
    assert isinstance(detail["elapsed_ms"], int)
    assert detail["elapsed_ms"] >= 0
    # No behavior change: the existing fields are still present.
    assert detail["outcome"] == "completed"
    assert detail["dataset_id"] == "d1"


def test_failed_post_still_carries_elapsed_ms():
    detail = _detail_for(
        _run_bridge("completed", post_result={"ok": False, "status": 503}),
        "http_bridge_post_failed",
    )
    assert detail is not None, "expected an http_bridge_post_failed event"
    assert isinstance(detail.get("elapsed_ms"), int)
    assert detail["elapsed_ms"] >= 0


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
