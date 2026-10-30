"""Who says "memory ready" when a detached worker boots the server (SDK-1244).

The SessionStart hook hands a cold boot to a detached bootstrap worker and
returns at once. It used to record ``phase_end("connecting", ok=False)`` on its
way out, because the server was (naturally) not up yet; once the ``setting_up``
phase the worker owns went stale, the row fell back to that verdict and read
"memory unavailable" next to a live server.

Contract:
  * the hook records no ``ready`` verdict when a worker owns the boot;
  * ``phase_touch`` keeps a running phase fresh without changing anything else;
  * the worker says ready once the session is registered, before the
    code-graph pass, and never while nothing is registered.

claude-code only: the other hosts have no lazy bootstrap worker.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def _supported(suite):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: no bootstrap worker")


@pytest.fixture
def pc(suite, _supported, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def session_start(suite, _supported, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "false")
    monkeypatch.setattr(module, "_LAZY_BOOTSTRAP", True)
    monkeypatch.setattr(module, "_spawn_bootstrap", lambda *a, **k: None)
    monkeypatch.setattr(module, "_spawn_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(module, "_spawn_exit_watcher", lambda *a, **k: None)
    return module


def _phases(module, monkeypatch) -> dict:
    seen: dict = {"begin": [], "end": []}
    monkeypatch.setattr(module, "phase_begin", lambda phase, *a, **kw: seen["begin"].append(phase))
    monkeypatch.setattr(module, "phase_end", lambda phase, **kw: seen["end"].append((phase, kw)))
    return seen


def _run_start(module, payloads, url, monkeypatch, *, verdict, heavy_ok=True):
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    monkeypatch.setattr(module, "server_presence", lambda *a, **k: (verdict, {}))

    async def fake_heavy(*a, **k):
        return "", "", heavy_ok

    monkeypatch.setattr(module, "_run_heavy", fake_heavy)
    return asyncio.run(module._start(payloads.session_start()))


# ── the hook ────────────────────────────────────────────────────────────────


def test_a_handed_off_boot_leaves_the_verdict_to_the_worker(
    session_start, payloads, closed_port_url, monkeypatch
):
    seen = _phases(session_start, monkeypatch)
    _run_start(
        session_start, payloads, closed_port_url, monkeypatch, verdict=session_start.PRESENCE_ABSENT
    )
    assert "setting_up" in seen["begin"]
    assert all(phase != "connecting" for phase, _ in seen["end"])


def test_a_failed_inline_connect_retried_by_a_worker_leaves_the_verdict_too(
    session_start, payloads, closed_port_url, monkeypatch
):
    seen = _phases(session_start, monkeypatch)
    _run_start(
        session_start,
        payloads,
        closed_port_url,
        monkeypatch,
        verdict=session_start.PRESENCE_READY,
        heavy_ok=False,
    )
    assert "setting_up" in seen["begin"]
    assert all(phase != "connecting" for phase, _ in seen["end"])


def test_a_live_server_still_gets_the_hooks_own_verdict(
    session_start, payloads, closed_port_url, monkeypatch
):
    seen = _phases(session_start, monkeypatch)
    _run_start(
        session_start, payloads, closed_port_url, monkeypatch, verdict=session_start.PRESENCE_READY
    )
    assert ("connecting", {"session_key": None, "ok": True}) in seen["end"] or any(
        phase == "connecting" and kw.get("ok") is True for phase, kw in seen["end"]
    )


# ── the marker ──────────────────────────────────────────────────────────────


def test_phase_touch_refreshes_a_running_phase(pc, monkeypatch):
    key = "claude_touch"
    pc.phase_begin("setting_up", "starting memory server", session_key=key)
    path = pc._phase_path(key)
    before = json.loads(path.read_text(encoding="utf-8"))["active"]
    monkeypatch.setattr(pc.time, "time", lambda: before["updated_at"] + 200)
    pc.phase_touch("setting_up", session_key=key)
    after = json.loads(path.read_text(encoding="utf-8"))["active"]
    assert after["updated_at"] == before["updated_at"] + 200
    assert after["started_at"] == before["started_at"]
    assert after["detail"] == "starting memory server"
    # Fresh again for the surfaces that would have called it dead.
    assert pc.phase_snapshot(key)["active"]["phase"] == "setting_up"


def test_phase_touch_leaves_another_phase_alone(pc):
    key = "claude_touch_other"
    pc.phase_begin("recalling", session_key=key)
    before = json.loads(pc._phase_path(key).read_text(encoding="utf-8"))
    pc.phase_touch("setting_up", session_key=key)
    assert json.loads(pc._phase_path(key).read_text(encoding="utf-8")) == before
    # And writes nothing when no phase runs.
    pc.phase_end("recalling", count=0, session_key=key)
    before = json.loads(pc._phase_path(key).read_text(encoding="utf-8"))
    pc.phase_touch("setting_up", session_key=key)
    assert json.loads(pc._phase_path(key).read_text(encoding="utf-8")) == before


# ── the worker ──────────────────────────────────────────────────────────────


def _worker(module, monkeypatch, *, healthy: bool):
    seen = _phases(module, monkeypatch)
    monkeypatch.setattr(module, "_health_ok", lambda *a, **k: healthy)
    monkeypatch.setattr(module, "_wait_for_health", lambda *a, **k: healthy)
    monkeypatch.setattr(module, "_SETTING_UP_HEARTBEAT_SECONDS", 0.01)
    touches: list = []
    monkeypatch.setattr(module, "phase_touch", lambda phase, **kw: touches.append(phase))
    return seen, touches


def test_the_worker_says_ready_once_registered_before_the_index(session_start, monkeypatch):
    seen, touches = _worker(session_start, monkeypatch, healthy=True)
    order: list = []

    async def fake_heavy(*a, on_registered=None, **k):
        order.append("registering")
        on_registered()
        order.append("indexing")
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", fake_heavy)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert seen["end"] == [("setting_up", {"session_key": "k"})]
    assert order == ["registering", "indexing"]


def test_the_worker_ends_the_phase_once_when_the_heavy_path_never_calls_back(
    session_start, monkeypatch
):
    seen, _ = _worker(session_start, monkeypatch, healthy=True)

    async def fake_heavy(*a, **k):
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", fake_heavy)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert seen["end"] == [("setting_up", {"session_key": "k"})]


def test_a_server_that_never_answers_is_reported_as_not_ready(session_start, monkeypatch):
    seen, _ = _worker(session_start, monkeypatch, healthy=False)
    monkeypatch.setattr(session_start, "_bootstrap_singleflight", lambda: _NoLock())
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert seen["end"] == [("setting_up", {"session_key": "k", "ok": False})]


def test_a_registration_that_raises_is_reported_as_not_ready(session_start, monkeypatch):
    seen, _ = _worker(session_start, monkeypatch, healthy=True)

    async def boom(*a, **k):
        raise RuntimeError("registration refused")

    monkeypatch.setattr(session_start, "_run_heavy", boom)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert seen["end"] == [("setting_up", {"session_key": "k", "ok": False})]


def test_a_registration_the_heavy_path_reports_refused_is_not_ready(session_start, monkeypatch):
    seen, _ = _worker(session_start, monkeypatch, healthy=True)

    async def refused(*a, on_registered=None, **k):
        # _run_heavy reports a refused registration (a 403, a bad key) in its
        # return, not by raising; it never calls back.
        return "", "", False

    monkeypatch.setattr(session_start, "_run_heavy", refused)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert seen["end"] == [("setting_up", {"session_key": "k", "ok": False})]


def test_the_heartbeat_has_stopped_before_the_verdict_is_recorded(session_start, monkeypatch):
    import threading
    import time

    _worker(session_start, monkeypatch, healthy=True)
    # A touch that takes longer than the heartbeat's wait, so the thread is
    # mid-touch most of the time: a stop that does not wait for it would let
    # the touch write a stale "setting_up" back over the ready result.
    monkeypatch.setattr(session_start, "phase_touch", lambda phase, **kw: time.sleep(0.02))
    heartbeat_alive_at_end: list = []

    def end(phase, **kw):
        heartbeat_alive_at_end.append(
            any(t.name == "cognee-setting-up" and t.is_alive() for t in threading.enumerate())
        )

    monkeypatch.setattr(session_start, "phase_end", end)

    async def slow_heavy(*a, on_registered=None, **k):
        await asyncio.sleep(0.1)
        on_registered()
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", slow_heavy)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert heartbeat_alive_at_end == [False]


def test_the_heartbeat_touches_the_phase_while_the_boot_runs(session_start, monkeypatch):
    seen, touches = _worker(session_start, monkeypatch, healthy=True)

    async def slow_heavy(*a, on_registered=None, **k):
        await asyncio.sleep(0.1)
        on_registered()
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", slow_heavy)
    asyncio.run(session_start._run_bootstrap({"session_id": "sid", "session_key": "k"}))
    assert touches and set(touches) == {"setting_up"}
    assert seen["end"] == [("setting_up", {"session_key": "k"})]


class _NoLock:
    """A single-flight that is always won, with nothing to release."""

    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False
