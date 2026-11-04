"""The live-activity marker tells the truth about finished work.

``phase_end`` carries ``ok``; the status row ticks a phase only when it is True.
The session sync and the compaction anchor used to end their phases from a
``finally`` with the default, so a sync that raised still read ``✓ session
saved`` and a failed anchor ``✓ context kept``.
"""

from __future__ import annotations

import asyncio
import io
import json
import sys

import pytest

SESSION_KEY = "claude_f9d3c6c0-1a2b-4c3d-8e9f-0a1b2c3d4e5f"


def _capture(module, monkeypatch) -> list:
    ends: list = []
    monkeypatch.setattr(module, "phase_begin", lambda *a, **k: None)
    monkeypatch.setattr(module, "phase_end", lambda phase, **kw: ends.append((phase, kw)))
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return ends


@pytest.fixture
def sync(suite, hook_module, monkeypatch):
    module = hook_module(suite, "sync-session-to-graph.py")
    if not hasattr(module, "phase_end"):
        pytest.skip(f"{suite.name}: no live-activity marker")
    monkeypatch.setattr(
        module, "_load_resolved", lambda: ("sid", "ds", "uid", "", False, True, SESSION_KEY)
    )
    monkeypatch.setattr(module, "_sync_targets", lambda *a, **k: [])
    return module


def test_a_sync_that_raises_is_reported_as_failed(sync, monkeypatch):
    ends = _capture(sync, monkeypatch)

    def boom():
        raise RuntimeError("watcher gone")

    monkeypatch.setattr(sync, "_stop_idle_watcher", boom)
    with pytest.raises(RuntimeError):
        asyncio.run(sync._sync(stop_watcher=True))
    assert ends == [("syncing", {"session_key": SESSION_KEY, "ok": False})]


def test_a_sync_with_nothing_left_over_is_reported_as_done(sync, monkeypatch):
    ends = _capture(sync, monkeypatch)
    monkeypatch.setattr(sync, "_stop_idle_watcher", lambda: None)
    asyncio.run(sync._sync(stop_watcher=True))
    assert ends == [("syncing", {"session_key": SESSION_KEY, "ok": True})]


@pytest.fixture
def compact(suite, hook_module, monkeypatch):
    module = hook_module(suite, "pre-compact.py")
    if not hasattr(module, "phase_end"):
        pytest.skip(f"{suite.name}: no live-activity marker")
    monkeypatch.setattr(module, "is_observer_child", lambda: False)
    monkeypatch.setattr(module, "resolve_session_key_from_payload", lambda payload: (None, ""))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "host"})))
    return module


def test_a_failed_anchor_is_not_reported_as_kept(compact, monkeypatch):
    ends = _capture(compact, monkeypatch)

    async def boom():
        raise RuntimeError("server down")

    monkeypatch.setattr(compact, "_run", boom)
    compact.main()
    assert ends == [("anchoring", {"ok": False})]


def test_a_landed_anchor_is_reported_as_kept(compact, monkeypatch):
    ends = _capture(compact, monkeypatch)

    async def fine():
        return None

    monkeypatch.setattr(compact, "_run", fine)
    compact.main()
    assert ends == [("anchoring", {"ok": True})]
