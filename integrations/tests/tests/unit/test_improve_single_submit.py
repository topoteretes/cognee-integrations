"""One improve submit per trigger (SDK-594, ``has_single_submit_improve`` suites).

Root-caused from a real hook.log: the plugin re-submitted a busy answer from
``POST /api/v1/improve`` every 15s for up to ten minutes, and the final sync
retried that whole loop three times — 13,915 busy re-submits against 1,819 real
improves, one session at ~640 submits over three days with no bridge landing.
The server records every submit as an improve operation whether or not it did
anything, and repeated/parallel improves are safe server-side (feedback weights
carry per-QA applied markers, QA/trace persistence and agent-context extraction
advance per-session watermarks, ingestion dedups by content hash), so waiting on
the lock bought nothing: the in-flight run persists everything above the
watermark and the next trigger covers the rest.

Contract pinned here:
  * no plugin-side per-session lock (``improve_session_lock`` is gone, the
    ``improve-locks/`` dir is swept as a legacy leftover) and no boolean
    ``run_session_improve`` wrapper;
  * ``run_session_improve_detailed`` returns ``{"ok", "reason", "error"}``;
    a busy answer is ONE submit with ``reason == "busy"``, never re-submitted;
  * every attempt that does not land records a failure, which
    ``improve_throttle_reason`` reports as ``backoff`` for the cooldown window;
    a later success clears it;
  * exactly one ``improve_fired`` line per attempt, carrying ``reason``;
  * no post-submit pipeline-status poll and no ``wait_for_cognify``.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    if not suite.has_single_submit_improve:
        pytest.skip("suite still carries the plugin-side improve lock and busy loop")
    common = isolated_modules(suite, "_plugin_common")
    common._events = []
    monkeypatch.setattr(
        common, "hook_log", lambda ev, detail=None: common._events.append((ev, detail or {}))
    )
    return common


@pytest.fixture
def run_detailed(pc, monkeypatch):
    """Drive run_session_improve_detailed with every seam mocked; return (outcome, calls)."""

    def _run(improve_result, *, drain_results=None, reachable=True, trigger="idle"):
        calls = {"drain": 0, "improve": 0, "sleep": 0}

        def _drain(dataset, session, **kwargs):
            calls["drain"] += 1
            if drain_results:
                return drain_results[min(calls["drain"] - 1, len(drain_results) - 1)]
            return (0, 0)

        def _improve(dataset, session, **kwargs):
            calls["improve"] += 1
            return improve_result

        monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
        monkeypatch.setattr(pc, "_backend_reachable", lambda url, **kw: reachable)
        monkeypatch.setattr(pc, "drain_warmup_entries", _drain)
        monkeypatch.setattr(pc, "ensure_dataset_via_http", lambda d: None)
        monkeypatch.setattr(pc, "improve_session_via_http", _improve)
        monkeypatch.setattr(pc, "refresh_credits", lambda *a, **k: {})
        monkeypatch.setattr(pc, "_DRAIN_RETRY_PAUSE_SECONDS", 0.0)
        monkeypatch.setattr(
            pc.time, "sleep", lambda s: calls.__setitem__("sleep", calls["sleep"] + 1)
        )
        pc._events.clear()
        return pc.run_session_improve_detailed("ds", "sid", trigger=trigger), calls

    return _run


def _fired(pc) -> list[dict]:
    return [d for ev, d in pc._events if ev == "improve_fired"]


# ── surface ───────────────────────────────────────────────────────────────────


def test_lock_wrapper_and_poller_are_gone(pc):
    for name in (
        "improve_session_lock",
        "_IMPROVE_LOCK_DIR",
        "_run_session_improve_locked",
        "SYNC_LOCK_STALE_SECONDS",
        "run_session_improve",
        "wait_for_cognify",
    ):
        assert not hasattr(pc, name), name


def test_outcome_shape(run_detailed):
    outcome, _ = run_detailed({"ok": True})
    assert outcome == {"ok": True, "reason": "", "error": ""}


# ── busy ──────────────────────────────────────────────────────────────────────


def test_busy_is_one_submit_and_never_waited_on(run_detailed, pc):
    outcome, calls = run_detailed({"ok": False, "busy": True})
    assert outcome == {"ok": False, "reason": "busy", "error": ""}
    assert calls["improve"] == 1, "a busy answer must not be re-submitted"
    assert calls["sleep"] == 0, "no busy-wait"
    assert [f["reason"] for f in _fired(pc)] == ["busy"], "exactly one improve_fired"


def test_busy_records_a_failure_that_arms_the_backoff(run_detailed, pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    run_detailed({"ok": False, "busy": True})
    state = pc.read_improve_state("sid")
    assert state["last_failure_reason"] == "busy"
    assert "last_improved_at" not in state
    assert pc.improve_throttle_reason("sid") == "backoff"


# ── other failures ────────────────────────────────────────────────────────────


def test_timeout_and_http_errors_record_the_error_text(run_detailed, pc):
    outcome, _ = run_detailed({"ok": False, "status": 0, "error": "timed out"}, trigger="auto")
    assert outcome == {"ok": False, "reason": "failed", "error": "timed out"}
    state = pc.read_improve_state("sid")
    assert state["last_failure_reason"] == "timed out"
    assert state["last_failure_trigger"] == "auto"
    run_detailed({"ok": False, "status": 500, "error": "HTTP 500: boom"})
    assert pc.read_improve_state("sid")["failure_count"] == 2


def test_unreachable_backend_submits_nothing_but_still_backs_off(run_detailed, pc):
    outcome, calls = run_detailed({"ok": True}, reachable=False)
    assert outcome == {"ok": False, "reason": "unreachable", "error": "backend unreachable"}
    assert calls == {"drain": 0, "improve": 0, "sleep": 0}
    assert [f["reason"] for f in _fired(pc)] == ["unreachable"]
    assert pc.read_improve_state("sid")["last_failure_reason"] == "unreachable"
    assert pc.improve_throttle_reason("sid") == "backoff"


def test_incomplete_drain_is_reported_separately(run_detailed, pc):
    outcome, calls = run_detailed({"ok": True}, drain_results=[(0, 3), (0, 3)])
    assert outcome == {"ok": False, "reason": "incomplete_drain", "error": ""}
    assert calls["improve"] == 1  # the improve still ran: partial persist beats none
    assert calls["drain"] == 2  # one in-place retry
    # The submit itself landed, so the cooldown (not a backoff) starts here.
    assert "last_improved_at" in pc.read_improve_state("sid")
    assert any(ev == "improve_incomplete_drain" for ev, _ in pc._events)


# ── success ───────────────────────────────────────────────────────────────────


def test_success_clears_the_backoff_and_arms_the_cooldown(run_detailed, pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    run_detailed({"ok": False, "busy": True})
    assert pc.improve_throttle_reason("sid") == "backoff"
    outcome, _ = run_detailed({"ok": True}, trigger="final")
    assert outcome["ok"] is True
    state = pc.read_improve_state("sid")
    assert "last_failed_at" not in state and "failure_count" not in state
    assert state["trigger"] == "final"
    assert pc.improve_throttle_reason("sid") == "cooldown"
    assert [f["ok"] for f in _fired(pc)] == [True]


# ── backoff semantics in improve_throttle_reason ─────────────────────────────


def test_backoff_expires_with_the_cooldown_window(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    pc.record_improve_failure("sid", "ds", "idle", "timed out")
    assert pc.improve_throttle_reason("sid") == "backoff"
    path = pc._improve_state_path("sid")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["last_failed_at"] -= 601
    path.write_text(json.dumps(state), encoding="utf-8")
    assert pc.improve_throttle_reason("sid") == ""


def test_failure_after_success_keeps_the_success_on_record(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "0")
    pc.record_improve_success("sid", "ds", "idle")
    pc.bump_turn_counter("sid")
    pc.record_improve_failure("sid", "ds", "idle", "busy")
    state = pc.read_improve_state("sid")
    assert state["last_improved_at"] and state["last_failed_at"]
    # Cooldown 0: neither window applies, and there IS a new entry -> allowed.
    assert pc.improve_throttle_reason("sid") == ""


def test_empty_session_id_failure_is_a_no_op(pc):
    pc.record_improve_failure("", "ds", "idle", "busy")
    assert pc.read_improve_state("") == {}
    assert not pc._IMPROVE_STATE_DIR.exists() or not list(pc._IMPROVE_STATE_DIR.glob("*.json"))


# ── the idle watcher honours the backoff like any other reason ───────────────


def test_idle_watcher_treats_backoff_as_throttled(
    suite, hook_module, isolated_modules, monkeypatch
):
    import asyncio
    import time

    if not suite.has_single_submit_improve:
        pytest.skip("suite has no failure backoff")
    watcher = hook_module(suite, "idle-watcher.py")
    common = isolated_modules(suite, "_plugin_common")
    events, improves = [], []
    monkeypatch.setattr(watcher, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(watcher, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(watcher, "_log", lambda event, **detail: events.append((event, detail)))
    monkeypatch.setattr(watcher, "_read_activity_ts", lambda: time.time() - 100)
    monkeypatch.setattr(watcher, "_owns_pidfile", lambda: True)
    monkeypatch.setattr(watcher, "_run_update_check", lambda: None)
    monkeypatch.setattr(watcher, "_check_llm_key", lambda config: None)
    monkeypatch.setattr(common, "resolve_cognee_session_id", lambda: "sid")
    monkeypatch.setattr(common, "resolve_active_dataset", lambda: "ds")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(common, "improve_throttle_reason", lambda sid: "backoff")

    async def _fake_improve(session_id, dataset, config):
        improves.append((session_id, dataset))
        return True

    monkeypatch.setattr(watcher, "_improve_once", _fake_improve)

    async def _run():
        watcher._should_stop = False
        task = asyncio.ensure_future(watcher._main_loop("sid", "ds", {}))
        await asyncio.sleep(0.1)
        watcher._should_stop = True
        await asyncio.wait_for(task, timeout=5)

    asyncio.run(_run())
    assert improves == []
    throttled = [d for ev, d in events if ev == "improve_throttled"]
    assert throttled and throttled[0]["reason"] == "backoff"
