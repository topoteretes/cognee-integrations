"""Unit tests for elapsed_ms timing on hook events (_plugin_common._elapsed_ms).

Covers the helper itself (deterministic via a patched monotonic clock) and the
bridge poll path, asserting `http_bridge_poll` carries an integer `elapsed_ms`
with no behavior change to the dedup/poll contract.

Run: python integrations/claude-code/tests/test_elapsed_ms.py (or via pytest).
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def _patch_monotonic(values):
    """Replace pc.time.monotonic with a generator over `values`; returns a restore()."""
    seq = iter(values)
    orig = pc.time.monotonic
    pc.time.monotonic = lambda: next(seq)
    return lambda: setattr(pc.time, "monotonic", orig)


# --- _elapsed_ms helper -----------------------------------------------------
def test_elapsed_ms_basic():
    restore = _patch_monotonic([100.0, 100.25])  # start, then measured end (+250ms)
    try:
        start = pc.time.monotonic()
        assert pc._elapsed_ms(start) == 250
    finally:
        restore()


def test_elapsed_ms_floors_at_zero():
    # A non-monotonic reading (clock goes backwards) must never produce a negative.
    restore = _patch_monotonic([100.0, 99.0])
    try:
        start = pc.time.monotonic()
        assert pc._elapsed_ms(start) == 0
    finally:
        restore()


def test_elapsed_ms_returns_int():
    start = pc.time.monotonic()
    out = pc._elapsed_ms(start)
    assert isinstance(out, int)
    assert out >= 0


# --- bridge poll carries elapsed_ms -----------------------------------------
def test_http_bridge_poll_logs_elapsed_ms():
    logs = []
    seams = (
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
    saved = {k: getattr(pc, k) for k in seams}

    pc._local_api_url = lambda: "http://x"
    pc._backend_reachable = lambda url: True
    pc._api_key = lambda: "k"
    pc._format_cached_bridge_document = lambda dataset, sid: ("qa text", "")
    pc._bridge_file = lambda sid: pathlib.Path("/tmp/_elapsed_bridge_test.json")
    pc._load_json_file = lambda p: {}
    pc._write_json_file = lambda p, data: None
    pc._post_remember_document = lambda *a, **k: {"ok": True, "dataset_id": "d1"}
    pc.wait_for_cognify = lambda *a, **k: "completed"
    pc.hook_log = lambda event, detail=None: logs.append((event, detail))
    try:
        pc.persist_session_cache_to_graph_via_http("ds", "sid")
    finally:
        for k, v in saved.items():
            setattr(pc, k, v)

    polls = [detail for event, detail in logs if event == "http_bridge_poll"]
    assert polls, "expected an http_bridge_poll event"
    assert "elapsed_ms" in polls[0]
    assert isinstance(polls[0]["elapsed_ms"], int)
    assert polls[0]["elapsed_ms"] >= 0


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
