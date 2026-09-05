"""The idle watcher's main loop (``idle-watcher.py::_main_loop``) and its cooldown.

The watcher exits after one successful bridge (to release the local graph store)
and is respawned on the next prompt. Its cooldown therefore cannot live in the
process — it reads the per-session improve state through
``_plugin_common.improve_throttle_reason`` instead. What is pinned here:

  * throttled -> no improve, and the watcher keeps polling rather than exiting,
    so a quiet stretch that outlasts the cooldown still gets its one bridge;
  * not throttled -> exactly one improve, then exit with ``bridge_complete``;
  * the watcher never records improve state itself (the improve functions do,
    on a confirmed success — the watcher cannot tell a lock-refused run apart);
  * the shutdown bridge runs only when activity is newer than the last improve.

The loop is driven with a tiny poll interval and every I/O seam patched; the
watcher is loaded before ``_plugin_common`` so its function-local imports bind
to the module the patches land on (same pattern as test_llm_key_check.py).
"""

from __future__ import annotations

import asyncio
import time

import pytest


@pytest.fixture
def harness(suite, hook_module, isolated_modules, monkeypatch):
    watcher = hook_module(suite, "idle-watcher.py")
    pc = isolated_modules(suite, "_plugin_common")

    events: list[tuple[str, dict]] = []
    improves: list[tuple[str, str]] = []

    monkeypatch.setattr(watcher, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(watcher, "IDLE_SECONDS", 0.0)
    monkeypatch.setattr(watcher, "_log", lambda event, **detail: events.append((event, detail)))
    monkeypatch.setattr(watcher, "_read_activity_ts", lambda: time.time() - 100)
    monkeypatch.setattr(watcher, "_owns_pidfile", lambda: True)
    monkeypatch.setattr(watcher, "_run_update_check", lambda: None)
    monkeypatch.setattr(watcher, "_check_llm_key", lambda config: None)
    monkeypatch.setattr(pc, "resolve_cognee_session_id", lambda: "sid")
    monkeypatch.setattr(pc, "resolve_active_dataset", lambda: "ds")
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)

    async def _fake_improve(session_id, dataset, config):
        improves.append((session_id, dataset))
        return True

    monkeypatch.setattr(watcher, "_improve_once", _fake_improve)

    def _record_guard(*a, **k):
        raise AssertionError("the watcher must not record improve state itself")

    monkeypatch.setattr(pc, "record_improve_success", _record_guard)

    async def _run(*, stop_after: float | None = None):
        """Run the loop; optionally raise the stop flag after ``stop_after`` seconds."""
        watcher._should_stop = False
        task = asyncio.ensure_future(watcher._main_loop("sid", "ds", {}))
        if stop_after is not None:
            await asyncio.sleep(stop_after)
            watcher._should_stop = True
        await asyncio.wait_for(task, timeout=5)

    def _exit_reason():
        for event, detail in events:
            if event == "exiting":
                return detail.get("reason")
        return None

    return watcher, pc, events, improves, _run, _exit_reason


def test_not_throttled_bridges_once_and_exits(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness
    monkeypatch.setattr(pc, "improve_throttle_reason", lambda sid: "")

    asyncio.run(run())

    assert improves == [("sid", "ds")]
    assert exit_reason() == "bridge_complete"
    assert any(ev == "idle_trigger" for ev, _ in events)
    assert not any(ev == "improve_throttled" for ev, _ in events)


def test_throttled_keeps_polling_without_bridging(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness
    monkeypatch.setattr(pc, "improve_throttle_reason", lambda sid: "cooldown")
    # Recent improve on record: the shutdown bridge must stay quiet too.
    monkeypatch.setattr(pc, "read_improve_state", lambda sid: {"last_improved_at": time.time()})

    asyncio.run(run(stop_after=0.15))  # ~15 polls, all inside the cooldown

    assert improves == []
    assert exit_reason() == "signal", "the loop had to be stopped — it did not exit on its own"
    throttled = [d for ev, d in events if ev == "improve_throttled"]
    assert len(throttled) == 1, "logged once per reason, not once per poll"
    assert throttled[0]["reason"] == "cooldown"
    assert throttled[0]["session"] == "sid"
    assert not any(ev == "shutdown_trigger" for ev, _ in events)


def test_bridges_once_the_cooldown_expires(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness
    answers = iter(["cooldown", "cooldown", "cooldown", "no_new_entries", ""])
    monkeypatch.setattr(pc, "improve_throttle_reason", lambda sid: next(answers, ""))

    asyncio.run(run())

    assert improves == [("sid", "ds")]
    assert exit_reason() == "bridge_complete"
    reasons = [d["reason"] for ev, d in events if ev == "improve_throttled"]
    assert reasons == ["cooldown", "no_new_entries"], "one log line per reason change"


def test_failed_bridge_disables_further_attempts(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness
    monkeypatch.setattr(pc, "improve_throttle_reason", lambda sid: "")

    async def _failing(session_id, dataset, config):
        improves.append((session_id, dataset))
        return False

    monkeypatch.setattr(watcher, "_improve_once", _failing)
    monkeypatch.setattr(pc, "read_improve_state", lambda sid: {})

    asyncio.run(run(stop_after=0.1))

    assert improves == [("sid", "ds")]  # one attempt, then bridge_disabled
    assert any(ev == "bridge_disabled_after_failure" for ev, _ in events)
    assert not any(ev == "shutdown_trigger" for ev, _ in events)


def test_shutdown_bridge_only_when_activity_is_newer_than_last_improve(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness
    monkeypatch.setattr(pc, "improve_throttle_reason", lambda sid: "cooldown")

    # Last improve older than the activity -> the shutdown bridge fires once.
    monkeypatch.setattr(
        pc, "read_improve_state", lambda sid: {"last_improved_at": time.time() - 500}
    )
    asyncio.run(run(stop_after=0.05))
    assert improves == [("sid", "ds")]
    assert any(ev == "shutdown_trigger" for ev, _ in events)
    assert any(ev == "shutdown_bridge_done" for ev, _ in events)

    # Last improve newer than the activity -> nothing to bridge at shutdown.
    improves.clear()
    events.clear()
    monkeypatch.setattr(pc, "read_improve_state", lambda sid: {"last_improved_at": time.time() + 5})
    asyncio.run(run(stop_after=0.05))
    assert improves == []
    assert not any(ev == "shutdown_trigger" for ev, _ in events)


def test_cooldown_check_failure_fails_open(harness, monkeypatch):
    watcher, pc, events, improves, run, exit_reason = harness

    def _boom(sid):
        raise RuntimeError("state dir unreadable")

    monkeypatch.setattr(pc, "improve_throttle_reason", _boom)

    asyncio.run(run())

    assert improves == [("sid", "ds")]
    assert any(ev == "throttle_check_failed" for ev, _ in events)
