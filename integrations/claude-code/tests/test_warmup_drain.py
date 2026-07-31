"""Unit tests for the warmup entry buffer
(_plugin_common.append_warmup_entry and drain_warmup_entries).

Entries captured while the local server is still warming must be buffered as
structured /remember/entry payloads and replayed IN ORDER once the server is
ready, so the server-side session cache (which improve() bridges from) holds
the complete session. drain_warmup_entries returns (drained, remaining); a
replay failure keeps the unreplayed tail buffered; the buffer trim is computed
against a fresh re-read so entries appended during the replay survive; and a
single-drainer lock prevents concurrent double-replays.

Run: python integrations/claude-code/tests/test_warmup_drain.py (or via pytest).
"""

import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _plugin_common as pc  # noqa: E402


def _with_tmp_bridge(fn):
    """Run fn() with the bridge file and both locks pointed at temp paths."""
    saved = {k: getattr(pc, k) for k in ("_bridge_file", "hook_log", "_DRAIN_LOCK", "_BUFFER_LOCK")}
    with tempfile.TemporaryDirectory() as tmp:
        bridge = pathlib.Path(tmp) / "bridge_test.json"
        pc._bridge_file = lambda sid="": bridge
        pc.hook_log = lambda *a, **k: None
        pc._DRAIN_LOCK = pathlib.Path(tmp) / "drain.lock"
        pc._BUFFER_LOCK = pathlib.Path(tmp) / "buffer.lock"
        try:
            return fn()
        finally:
            for k, v in saved.items():
                setattr(pc, k, v)


def test_append_and_drain_in_order():
    def _run():
        replayed = []
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
        pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "q", "answer": "a"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda d, s, entry, **k: replayed.append(entry) or {}
        try:
            result = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return result, replayed

    result, replayed = _with_tmp_bridge(_run)
    assert result == (2, 0)
    assert [e["type"] for e in replayed] == ["trace", "qa"]


def test_drain_empty_buffer_is_noop():
    def _run():
        calls = []
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda *a, **k: calls.append(a) or {}
        try:
            result = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return result, calls

    result, calls = _with_tmp_bridge(_run)
    assert result == (0, 0)
    assert calls == []


def test_partial_failure_keeps_tail_buffered():
    def _run():
        replayed = []

        def _flaky(dataset, session_id, entry, **k):
            if len(replayed) >= 1:
                raise OSError("server went away")
            replayed.append(entry)
            return {}

        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Read"})
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Edit"})
        pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "q", "answer": "a"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _flaky
        try:
            first = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved

        # Second drain replays the surviving tail, in order.
        pc.remember_entry_via_http = lambda d, s, entry, **k: replayed.append(entry) or {}
        try:
            second = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return first, second, replayed

    first, second, replayed = _with_tmp_bridge(_run)
    assert first == (1, 2)
    assert second == (2, 0)
    assert [e.get("origin_function") or e["type"] for e in replayed] == ["Read", "Edit", "qa"]


def test_concurrent_append_during_drain_survives():
    # An entry appended by a concurrent hook WHILE the replay is in flight must
    # survive the buffer write-back (trim by fresh re-read, not stale snapshot).
    def _run():
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "B"})
        replayed = []

        def _replay(dataset, session_id, entry, **k):
            replayed.append(entry)
            if len(replayed) == 1:
                pc.append_warmup_entry(
                    "ds", "sid", {"type": "qa", "question": "new", "answer": "x"}
                )
            return {}

        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _replay
        try:
            result = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        cache = pc._load_json_file(pc._bridge_file("sid"))
        left = (cache.get(pc._bridge_cache_key("ds", "sid")) or {}).get("pending_entries")
        return result, left

    result, left = _with_tmp_bridge(_run)
    assert result == (2, 1)  # both originals replayed; the mid-drain arrival remains
    assert left == [{"type": "qa", "question": "new", "answer": "x"}]


def test_drain_skipped_when_lock_busy():
    def _run():
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
        calls = []
        saved_http = pc.remember_entry_via_http
        saved_lock = pc._try_acquire_drain_lock
        pc.remember_entry_via_http = lambda *a, **k: calls.append(a) or {}
        pc._try_acquire_drain_lock = lambda: False
        try:
            result = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved_http
            pc._try_acquire_drain_lock = saved_lock
        return result, calls

    result, calls = _with_tmp_bridge(_run)
    assert result == (0, 1)  # skipped, nothing replayed, entry still pending
    assert calls == []


def test_concurrent_appends_do_not_lose_entries():
    # Two async hooks appending at the same moment must both land: the buffer
    # mutex serializes the read-modify-write, so the last writer no longer
    # clobbers the other's entry.
    import concurrent.futures

    def _run():
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
            list(
                ex.map(
                    lambda i: pc.append_warmup_entry(
                        "ds", "sid", {"type": "trace", "origin_function": f"tool{i}"}
                    ),
                    range(8),
                )
            )
        cache = pc._load_json_file(pc._bridge_file("sid"))
        return (cache.get(pc._bridge_cache_key("ds", "sid")) or {}).get("pending_entries") or []

    entries = _with_tmp_bridge(_run)
    assert sorted(e["origin_function"] for e in entries) == [f"tool{i}" for i in range(8)]


def test_append_fails_open_when_lock_held():
    # A wedged lock must never make a hook hang or drop the entry: after the
    # short wait the append proceeds without the lock.
    def _run():
        saved_timeout = pc._BUFFER_LOCK_TIMEOUT_SECONDS
        pc._BUFFER_LOCK_TIMEOUT_SECONDS = 0.05
        pc._BUFFER_LOCK.parent.mkdir(parents=True, exist_ok=True)
        pc._BUFFER_LOCK.write_text("held")  # fresh lock held by someone else
        try:
            pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "X"})
        finally:
            pc._BUFFER_LOCK_TIMEOUT_SECONDS = saved_timeout
            pc._BUFFER_LOCK.unlink()
        cache = pc._load_json_file(pc._bridge_file("sid"))
        return (cache.get(pc._bridge_cache_key("ds", "sid")) or {}).get("pending_entries")

    entries = _with_tmp_bridge(_run)
    assert entries and entries[0]["origin_function"] == "X"


def test_drain_leaves_legacy_shadow_untouched():
    # The qa/trace text mirrors (legacy document-bridge data) must survive a drain.
    def _run():
        pc.append_http_bridge_entry("ds", "sid", trace="Bash [success]")
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = lambda *a, **k: {}
        try:
            pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        cache = pc._load_json_file(pc._bridge_file("sid"))
        return cache.get(pc._bridge_cache_key("ds", "sid"), {})

    session_cache = _with_tmp_bridge(_run)
    assert session_cache.get("trace") == ["Bash [success]"]
    assert session_cache.get("pending_entries") == []


def test_drain_budget_exceeded_preserves_tail():
    """#298: the replay stops once its time budget is spent — the unreplayed
    tail stays buffered and a warmup_drain_budget_exceeded event is logged."""

    def _run():
        import time as _time

        events = []
        pc.hook_log = lambda ev, detail=None: events.append((ev, detail or {}))
        for name in ("A", "B", "C"):
            pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": name})

        def _slow(dataset, session_id, entry, **k):
            _time.sleep(0.06)
            return {}

        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _slow
        try:
            result = pc.drain_warmup_entries("ds", "sid", budget_seconds=0.1)
        finally:
            pc.remember_entry_via_http = saved
        return result, events

    result, events = _with_tmp_bridge(_run)
    drained, remaining = result
    assert drained >= 1 and remaining >= 1, f"expected a partial drain, got {result}"
    assert drained + remaining == 3
    assert any(ev == "warmup_drain_budget_exceeded" for ev, _ in events)


def test_http_failure_arms_backoff_and_skips_next_drain():
    """#298: an HTTP-status replay failure arms a backoff window during which
    further drains are skipped without touching the network — a poisoned
    session (e.g. server 503-ing every write) cannot be ground against
    forever."""
    import urllib.error

    def _run():
        events = []
        pc.hook_log = lambda ev, detail=None: events.append((ev, detail or {}))
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})

        def _boom(dataset, session_id, entry, **k):
            raise urllib.error.HTTPError("http://x", 503, "busy", None, None)

        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _boom
        try:
            first = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved

        cache = pc._load_json_file(pc._bridge_file("sid"))
        state = cache.get(pc._bridge_cache_key("ds", "sid"), {})

        # Second drain, inside the backoff window: skipped before any network.
        calls = []
        pc.remember_entry_via_http = lambda *a, **k: calls.append(a) or {}
        try:
            second = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        return first, second, state, calls, events

    first, second, state, calls, events = _with_tmp_bridge(_run)
    assert first == (0, 1)
    assert int(state.get("drain_fail_count") or 0) == 1
    assert float(state.get("drain_fail_at") or 0) > 0
    assert second == (0, 1)
    assert calls == [], "backoff must skip the replay entirely"
    assert any(ev == "warmup_drain_backoff" for ev, _ in events)


def test_backoff_expires_and_success_clears_it():
    """After the backoff window passes, the drain runs again; progress clears
    the failure bookkeeping so the session is back to normal."""
    import urllib.error

    def _run():
        pc.hook_log = lambda *a, **k: None
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})

        def _boom(dataset, session_id, entry, **k):
            raise urllib.error.HTTPError("http://x", 503, "busy", None, None)

        saved = pc.remember_entry_via_http
        pc.remember_entry_via_http = _boom
        try:
            pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved

        # Age the failure past the first backoff window (60s).
        path = pc._bridge_file("sid")
        key = pc._bridge_cache_key("ds", "sid")
        cache = pc._load_json_file(path)
        cache[key]["drain_fail_at"] = cache[key]["drain_fail_at"] - 61
        pc._write_json_file(path, cache)

        pc.remember_entry_via_http = lambda d, s, entry, **k: {}
        try:
            result = pc.drain_warmup_entries("ds", "sid")
        finally:
            pc.remember_entry_via_http = saved
        state = pc._load_json_file(path).get(key, {})
        return result, state

    result, state = _with_tmp_bridge(_run)
    assert result == (1, 0)
    assert "drain_fail_count" not in state, f"backoff not cleared: {state}"
    assert "drain_fail_at" not in state


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
