"""Orchestration around the improve bridge (_plugin_common.run_session_improve /
run_session_improve_detailed on single-submit suites).

These stay at the seam: what is under test is the decision sequence — drain the
warmup buffer, improve, report failure when entries were left undelivered, and
record the session's improve state. There is deliberately no fallback any more: a
server without session-aware improve is reported as not synced, never bridged by
re-posting the whole transcript. The wire-level submit is covered in
integration/test_improve_http.py.

Busy handling differs by suite. Suites without ``has_single_submit_improve``
re-submit a busy answer until the server's per-session lock frees (the tests
at the bottom). Suites with it never do — that contract, plus the failure
backoff it records, lives in unit/test_improve_single_submit.py.

Migrated from {claude-code,codex}/tests/test_improve_sync.py.
"""

from __future__ import annotations

import time

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(
        common, "_suite_single_submit", suite.has_single_submit_improve, raising=False
    )
    return common


def _run_bool(pc, dataset, session_id, **kwargs) -> bool:
    """The boolean view of a sync across suite generations."""
    if hasattr(pc, "run_session_improve_detailed"):
        return bool(pc.run_session_improve_detailed(dataset, session_id, **kwargs)["ok"])
    return pc.run_session_improve(dataset, session_id, **kwargs)


@pytest.fixture
def run_improve(pc, monkeypatch):
    """Drive run_session_improve with every seam mocked; return (wrote, calls)."""

    def _run(improve_result, *, drain_results=None, improve_results=None, trigger=None):
        calls = {"drain": 0, "improve": 0}

        def _drain(dataset, session, **kwargs):
            calls["drain"] += 1
            if drain_results:
                return drain_results[min(calls["drain"] - 1, len(drain_results) - 1)]
            return (0, 0)

        def _improve(dataset, session, **kwargs):
            calls["improve"] += 1
            if improve_results:
                return improve_results[min(calls["improve"] - 1, len(improve_results) - 1)]
            return improve_result

        monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
        monkeypatch.setattr(pc, "_backend_reachable", lambda url: True)
        monkeypatch.setattr(pc, "drain_warmup_entries", _drain)
        monkeypatch.setattr(pc, "ensure_dataset_via_http", lambda d: None)
        monkeypatch.setattr(pc, "improve_session_via_http", _improve)
        monkeypatch.setattr(pc, "_DRAIN_RETRY_PAUSE_SECONDS", 0.0)

        kwargs = {} if trigger is None else {"trigger": trigger}
        return _run_bool(pc, "ds", "sid", **kwargs), calls

    return _run


def test_happy_path_drains_then_improves(run_improve):
    wrote, calls = run_improve({"ok": True})
    assert wrote is True
    assert calls == {"drain": 1, "improve": 1}


def test_unsupported_response_returns_false_without_sync(run_improve, pc):
    # A server without session-aware improve is reported, never worked around:
    # the old fallback re-posted the whole transcript for a full re-cognify.
    wrote, calls = run_improve({"ok": False, "unsupported": True, "status": 404})
    assert wrote is False
    assert calls["improve"] == 1
    assert not hasattr(pc, "persist_session_cache_to_graph_via_http")
    assert not hasattr(pc, "improve_unsupported")
    _assert_no_success_recorded(pc, suite_flag=pc._suite_single_submit)


def test_error_returns_false(run_improve, pc):
    wrote, _calls = run_improve({"ok": False, "status": 500, "error": "boom"})
    assert wrote is False
    _assert_no_success_recorded(pc, suite_flag=pc._suite_single_submit)


def _assert_no_success_recorded(pc, *, suite_flag: bool) -> None:
    """Nothing succeeded, so no success is on record. Single-submit suites do
    record the *failure* (that is what arms the backoff); the others record
    nothing at all."""
    state = pc.read_improve_state("sid")
    if suite_flag:
        assert "last_improved_at" not in state
        assert state.get("last_failed_at")
    else:
        assert state == {}


def test_success_records_improve_state(run_improve, pc):
    # The improve function itself stamps the session's improve state, so every
    # trigger (idle, auto, final, manual) feeds the cooldown the idle/auto
    # triggers read — recording in the watcher would stamp lock-refused runs.
    before = time.time()
    wrote, _calls = run_improve({"ok": True}, trigger="idle")
    assert wrote is True
    state = pc.read_improve_state("sid")
    assert state["session_id"] == "sid"
    assert state["dataset"] == "ds"
    assert state["trigger"] == "idle"
    assert state["last_improved_at"] >= before
    assert state["turn_count_at_improve"] == pc.read_turn_count("sid")


def test_default_trigger_is_final(run_improve, pc):
    run_improve({"ok": True})
    assert pc.read_improve_state("sid")["trigger"] == "final"


def test_final_trigger_ignores_cooldown(run_improve, pc):
    # run_session_improve never throttles itself: the cooldown is the caller's
    # decision (idle watcher, auto-every-N), and the final sync must always run.
    pc.record_improve_success("sid", "ds", "idle")
    assert pc.improve_throttle_reason("sid") == "cooldown"
    wrote, calls = run_improve({"ok": True}, trigger="final")
    assert wrote is True
    assert calls["improve"] == 1


def test_retries_busy_until_lock_frees(run_improve, suite, monkeypatch):
    # A lock-skipped improve may have snapshotted the cache before the latest
    # turns — run_session_improve must re-submit until a run actually lands.
    if suite.has_single_submit_improve:
        pytest.skip("single-submit suites never re-submit a busy answer")
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", "0.1")
    wrote, calls = run_improve(
        None,
        improve_results=[{"ok": False, "busy": True}, {"ok": False, "busy": True}, {"ok": True}],
    )
    assert wrote is True
    assert calls["improve"] == 3


def test_busy_deadline_gives_up(run_improve, suite, monkeypatch):
    if suite.has_single_submit_improve:
        pytest.skip("single-submit suites never re-submit a busy answer")
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", "0.1")
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_DEADLINE", "0.25")
    wrote, calls = run_improve({"ok": False, "busy": True})
    assert wrote is False  # still busy at deadline -> reported as not-synced
    assert calls["improve"] >= 2  # at least one retry happened


def test_incomplete_drain_returns_false_but_improve_still_runs(run_improve, pc):
    # Undelivered warmup entries mean the improve persisted an incomplete
    # session: the improve must still run (partial persist beats none), but the
    # sync must report failure so the caller's retry loop re-drives it.
    wrote, calls = run_improve({"ok": True}, drain_results=[(0, 3), (0, 3)])
    assert wrote is False
    assert calls["improve"] == 1  # improve ran despite the incomplete drain
    assert calls["drain"] == 2  # one in-place retry happened
    # The improve itself succeeded, so the cooldown still starts here.
    assert pc.read_improve_state("sid")["trigger"] == "final"


def test_drain_retry_recovers_and_sync_succeeds(run_improve):
    # First drain fails on a blip, the in-place retry delivers the tail ->
    # the sync is complete and reports success.
    wrote, calls = run_improve({"ok": True}, drain_results=[(0, 3), (3, 0)])
    assert wrote is True
    assert calls["drain"] == 2
    assert calls["improve"] == 1


def test_clean_drain_skips_retry(run_improve):
    wrote, calls = run_improve({"ok": True}, drain_results=[(2, 0)])
    assert wrote is True
    assert calls["drain"] == 1  # nothing remaining -> no retry
