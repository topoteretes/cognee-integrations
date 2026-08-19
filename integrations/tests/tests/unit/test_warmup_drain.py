"""The warmup entry buffer
(_plugin_common.append_warmup_entry / drain_warmup_entries).

Entries captured while the local server is still warming must be buffered as
structured /remember/entry payloads and replayed IN ORDER once the server is
ready, so the server-side session cache (which improve() bridges from) holds
the complete session. drain_warmup_entries returns (drained, remaining); a
replay failure keeps the unreplayed tail buffered; the buffer trim is computed
against a fresh re-read so entries appended during the replay survive; and a
single-drainer lock prevents concurrent double-replays.

Kept at the seam: locks, budgets, the concurrent-append mutex and the
backoff bookkeeping are all local state that no server can observe. The
in-order replay and the 503-arms-backoff path are additionally proven over real
HTTP in integration/test_warmup_replay.py.

Migrated from {claude-code,codex}/tests/test_warmup_drain.py.
"""

from __future__ import annotations

import concurrent.futures
import time
import urllib.error

import pytest


@pytest.fixture
def pc(suite, isolated_modules, tmp_path, monkeypatch):
    """_plugin_common with the bridge file and both locks in a temp dir."""
    common = isolated_modules(suite, "_plugin_common")
    bridge = tmp_path / "bridge_test.json"
    monkeypatch.setattr(common, "_bridge_file", lambda sid="": bridge)
    monkeypatch.setattr(common, "_DRAIN_LOCK", tmp_path / "drain.lock")
    monkeypatch.setattr(common, "_BUFFER_LOCK", tmp_path / "buffer.lock")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def events(pc, monkeypatch):
    recorded: list[tuple[str, dict]] = []
    monkeypatch.setattr(pc, "hook_log", lambda ev, detail=None: recorded.append((ev, detail or {})))
    return recorded


def _session_state(pc) -> dict:
    cache = pc._load_json_file(pc._bridge_file("sid"))
    return cache.get(pc._bridge_cache_key("ds", "sid"), {})


def _pending(pc) -> list:
    return _session_state(pc).get("pending_entries") or []


def _replay_into(pc, monkeypatch, sink: list):
    monkeypatch.setattr(
        pc, "remember_entry_via_http", lambda d, s, entry, **k: sink.append(entry) or {}
    )


def test_append_and_drain_in_order(pc, monkeypatch):
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
    pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "q", "answer": "a"})

    replayed: list = []
    _replay_into(pc, monkeypatch, replayed)
    assert pc.drain_warmup_entries("ds", "sid") == (2, 0)
    assert [e["type"] for e in replayed] == ["trace", "qa"]


def test_drain_empty_buffer_is_noop(pc, monkeypatch):
    calls: list = []
    monkeypatch.setattr(pc, "remember_entry_via_http", lambda *a, **k: calls.append(a) or {})
    assert pc.drain_warmup_entries("ds", "sid") == (0, 0)
    assert calls == []


def test_partial_failure_keeps_tail_buffered(pc, monkeypatch):
    replayed: list = []

    def _flaky(dataset, session_id, entry, **k):
        if len(replayed) >= 1:
            raise OSError("server went away")
        replayed.append(entry)
        return {}

    for entry in (
        {"type": "trace", "origin_function": "Read"},
        {"type": "trace", "origin_function": "Edit"},
        {"type": "qa", "question": "q", "answer": "a"},
    ):
        pc.append_warmup_entry("ds", "sid", entry)

    monkeypatch.setattr(pc, "remember_entry_via_http", _flaky)
    assert pc.drain_warmup_entries("ds", "sid") == (1, 2)

    # Second drain replays the surviving tail, in order.
    _replay_into(pc, monkeypatch, replayed)
    assert pc.drain_warmup_entries("ds", "sid") == (2, 0)
    assert [e.get("origin_function") or e["type"] for e in replayed] == ["Read", "Edit", "qa"]


def test_concurrent_append_during_drain_survives(pc, monkeypatch):
    # An entry appended by a concurrent hook WHILE the replay is in flight must
    # survive the buffer write-back (trim by fresh re-read, not stale snapshot).
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "B"})
    replayed: list = []

    def _replay(dataset, session_id, entry, **k):
        replayed.append(entry)
        if len(replayed) == 1:
            pc.append_warmup_entry("ds", "sid", {"type": "qa", "question": "new", "answer": "x"})
        return {}

    monkeypatch.setattr(pc, "remember_entry_via_http", _replay)
    # Both originals replayed; the mid-drain arrival remains.
    assert pc.drain_warmup_entries("ds", "sid") == (2, 1)
    assert _pending(pc) == [{"type": "qa", "question": "new", "answer": "x"}]


def test_drain_skipped_when_lock_busy(pc, monkeypatch):
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
    calls: list = []
    monkeypatch.setattr(pc, "remember_entry_via_http", lambda *a, **k: calls.append(a) or {})
    monkeypatch.setattr(pc, "_try_acquire_drain_lock", lambda: False)
    # Skipped, nothing replayed, entry still pending.
    assert pc.drain_warmup_entries("ds", "sid") == (0, 1)
    assert calls == []


def test_concurrent_appends_do_not_lose_entries(pc):
    # Two async hooks appending at the same moment must both land: the buffer
    # mutex serializes the read-modify-write, so the last writer no longer
    # clobbers the other's entry.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        list(
            ex.map(
                lambda i: pc.append_warmup_entry(
                    "ds", "sid", {"type": "trace", "origin_function": f"tool{i}"}
                ),
                range(8),
            )
        )
    assert sorted(e["origin_function"] for e in _pending(pc)) == [f"tool{i}" for i in range(8)]


def test_append_fails_open_when_lock_held(pc, monkeypatch):
    # A wedged lock must never make a hook hang or drop the entry: after the
    # short wait the append proceeds without the lock.
    monkeypatch.setattr(pc, "_BUFFER_LOCK_TIMEOUT_SECONDS", 0.05)
    pc._BUFFER_LOCK.parent.mkdir(parents=True, exist_ok=True)
    pc._BUFFER_LOCK.write_text("held")  # fresh lock held by someone else
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "X"})
    entries = _pending(pc)
    assert entries and entries[0]["origin_function"] == "X"


def test_drain_leaves_legacy_shadow_untouched(pc, monkeypatch):
    # The qa/trace text mirrors (legacy document-bridge data) must survive a drain.
    pc.append_http_bridge_entry("ds", "sid", trace="Bash [success]")
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "Bash"})
    monkeypatch.setattr(pc, "remember_entry_via_http", lambda *a, **k: {})
    pc.drain_warmup_entries("ds", "sid")

    state = _session_state(pc)
    assert state.get("trace") == ["Bash [success]"]
    assert state.get("pending_entries") == []


def test_drain_budget_exceeded_preserves_tail(pc, events, monkeypatch):
    """#298: the replay stops once its time budget is spent — the unreplayed
    tail stays buffered and a warmup_drain_budget_exceeded event is logged."""
    for name in ("A", "B", "C"):
        pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": name})

    def _slow(dataset, session_id, entry, **k):
        time.sleep(0.06)
        return {}

    monkeypatch.setattr(pc, "remember_entry_via_http", _slow)
    drained, remaining = pc.drain_warmup_entries("ds", "sid", budget_seconds=0.1)
    assert drained >= 1 and remaining >= 1, f"expected a partial drain, got {(drained, remaining)}"
    assert drained + remaining == 3
    assert any(ev == "warmup_drain_budget_exceeded" for ev, _ in events)


def test_http_failure_arms_backoff_and_skips_next_drain(pc, events, monkeypatch):
    """#298: an HTTP-status replay failure arms a backoff window during which
    further drains are skipped without touching the network — a poisoned session
    (e.g. server 503-ing every write) cannot be ground against forever."""
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})

    def _boom(dataset, session_id, entry, **k):
        raise urllib.error.HTTPError("http://x", 503, "busy", None, None)

    monkeypatch.setattr(pc, "remember_entry_via_http", _boom)
    assert pc.drain_warmup_entries("ds", "sid") == (0, 1)

    state = _session_state(pc)
    assert int(state.get("drain_fail_count") or 0) == 1
    assert float(state.get("drain_fail_at") or 0) > 0

    # Second drain, inside the backoff window: skipped before any network.
    calls: list = []
    monkeypatch.setattr(pc, "remember_entry_via_http", lambda *a, **k: calls.append(a) or {})
    assert pc.drain_warmup_entries("ds", "sid") == (0, 1)
    assert calls == [], "backoff must skip the replay entirely"
    assert any(ev == "warmup_drain_backoff" for ev, _ in events)


def test_backoff_expires_and_success_clears_it(pc, monkeypatch):
    """After the backoff window passes, the drain runs again; progress clears the
    failure bookkeeping so the session is back to normal."""
    pc.append_warmup_entry("ds", "sid", {"type": "trace", "origin_function": "A"})

    def _boom(dataset, session_id, entry, **k):
        raise urllib.error.HTTPError("http://x", 503, "busy", None, None)

    monkeypatch.setattr(pc, "remember_entry_via_http", _boom)
    pc.drain_warmup_entries("ds", "sid")

    # Age the failure past the first backoff window (60s).
    path = pc._bridge_file("sid")
    key = pc._bridge_cache_key("ds", "sid")
    cache = pc._load_json_file(path)
    cache[key]["drain_fail_at"] = cache[key]["drain_fail_at"] - 61
    pc._write_json_file(path, cache)

    monkeypatch.setattr(pc, "remember_entry_via_http", lambda d, s, entry, **k: {})
    assert pc.drain_warmup_entries("ds", "sid") == (1, 0)

    state = _session_state(pc)
    assert "drain_fail_count" not in state, f"backoff not cleared: {state}"
    assert "drain_fail_at" not in state
