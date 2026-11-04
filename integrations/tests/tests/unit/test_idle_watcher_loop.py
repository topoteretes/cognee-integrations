"""The background watcher's main loop (``idle-watcher.py::_main_loop``).

What is pinned here:

  * the stop sentinel ends the loop;
  * the periodic shared-memory refresh fires when due.

The loop is driven with a tiny poll interval and every I/O seam patched; the
watcher is loaded before ``_plugin_common`` so its function-local imports bind
to the module the patches land on (same pattern as test_llm_key_check.py).
"""

from __future__ import annotations

import asyncio

import pytest


@pytest.fixture
def harness(suite, hook_module, isolated_modules, monkeypatch):
    watcher = hook_module(suite, "idle-watcher.py")
    pc = isolated_modules(suite, "_plugin_common")

    events: list[tuple[str, dict]] = []

    monkeypatch.setattr(watcher, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(watcher, "_log", lambda event, **detail: events.append((event, detail)))
    monkeypatch.setattr(watcher, "_owns_pidfile", lambda: True)
    monkeypatch.setattr(watcher, "_run_update_check", lambda: None)
    monkeypatch.setattr(watcher, "_check_llm_key", lambda config: None)
    monkeypatch.setattr(pc, "hook_log", lambda *a, **k: None)

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

    return watcher, pc, events, _run, _exit_reason


def test_stop_sentinel_ends_the_loop(harness):
    watcher, pc, events, run, exit_reason = harness
    watcher._STOPFILE.parent.mkdir(parents=True, exist_ok=True)
    watcher._STOPFILE.touch()

    asyncio.run(run())

    assert exit_reason() == "stop_sentinel"


def test_shared_memory_refresh_fires_when_due(harness, monkeypatch):
    watcher, pc, events, run, exit_reason = harness
    refreshes: list[int] = []
    monkeypatch.setattr(watcher, "SHARED_REFRESH_SECONDS", 0.02)
    monkeypatch.setattr(pc, "refresh_shared_memory", lambda: refreshes.append(1) or True)

    asyncio.run(run(stop_after=0.1))

    assert refreshes, "no refresh in five refresh intervals"
    assert any(ev == "shared_memory_refreshed" for ev, _ in events)
    assert exit_reason() == "signal"
