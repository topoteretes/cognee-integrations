"""Tests for sync-session-to-graph.py strict mode.

The detached final-sync worker retries only on exceptions. Strict mode (used by
that worker) makes an incomplete session sync raise so the retry loop re-drives
the whole drain+improve, instead of silently reporting success on the session's
LAST sync. Non-strict (manual /cognee-sync, mid-session) keeps the old
log-and-return behavior.

The unregister half runs against the mock server's real
POST /api/v1/agents/unregister route, because the claim that matters — "a
strict raise still tears the registration down" — is about a request actually
leaving the process on the way out of a `finally` block. The bare `return` that
used to swallow that exception in codex was a real bug (fixed in #331); the last
test here is its guard.

Migrated from {claude-code,codex}/tests/test_sync_strict.py.
"""

from __future__ import annotations

import asyncio

import pytest

UNREGISTER = "/api/v1/agents/unregister"


@pytest.fixture
def sync_mod(suite, hook_module, mock_server, monkeypatch):
    """sync-session-to-graph.py with its resolution/improve seams stubbed.

    Only the *decision* inputs are faked (resolved identity, config, improve
    outcome); unregister keeps its real HTTP implementation so the teardown
    lands on the mock server.
    """
    module = hook_module(suite, "sync-session-to-graph.py")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", "principal-key")
    monkeypatch.setattr(module, "load_config", lambda: {})
    monkeypatch.setattr(module, "http_api_ready", lambda: True)
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return module


def _resolve(module, monkeypatch, *, agent_session_name="agent1", session_key="key1"):
    # (session_id, dataset, user_id, agent_session_name, was_registered,
    #  has_api_key, session_key)
    monkeypatch.setattr(
        module,
        "_load_resolved",
        lambda: ("sess1", "ds", "u1", agent_session_name, True, True, session_key),
    )


def _improve(module, monkeypatch, wrote, *, busy=False):
    """Stub the improve seam across suite generations: single-submit suites call
    ``run_session_improve_detailed`` (a dict outcome), the others the boolean
    ``run_session_improve``."""
    if hasattr(module, "run_session_improve_detailed"):
        outcome = {
            "ok": wrote,
            "reason": "busy" if busy else ("" if wrote else "failed"),
            "error": "" if wrote or busy else "timed out",
        }
        monkeypatch.setattr(module, "run_session_improve_detailed", lambda d, s, **kw: outcome)
    else:
        monkeypatch.setattr(module, "run_session_improve", lambda d, s, **kw: wrote)


def test_strict_raises_on_incomplete_sync(sync_mod, monkeypatch):
    _resolve(sync_mod, monkeypatch)
    _improve(sync_mod, monkeypatch, False)
    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(sync_mod._sync(stop_watcher=False, strict=True))


def test_non_strict_does_not_raise(sync_mod, monkeypatch):
    _resolve(sync_mod, monkeypatch)
    _improve(sync_mod, monkeypatch, False)
    asyncio.run(sync_mod._sync(stop_watcher=False, strict=False))  # must not raise


def test_strict_complete_sync_does_not_raise(sync_mod, monkeypatch):
    _resolve(sync_mod, monkeypatch)
    _improve(sync_mod, monkeypatch, True)
    asyncio.run(sync_mod._sync(stop_watcher=False, strict=True))  # must not raise


def test_strict_defers_a_busy_answer_instead_of_raising(sync_mod, suite, monkeypatch):
    """Another improve of this session is in flight server-side; it persists
    everything above the watermark, so re-driving would only be recorded as one
    more improve operation. Busy is logged as deferred, never as incomplete —
    until 1.5.3 one collision here became three ten-minute busy loops."""
    if not suite.has_single_submit_improve:
        pytest.skip("suite still treats busy as incomplete")
    events = []
    monkeypatch.setattr(
        sync_mod, "hook_log", lambda ev, detail=None: events.append((ev, detail or {}))
    )
    _resolve(sync_mod, monkeypatch)
    _improve(sync_mod, monkeypatch, False, busy=True)
    asyncio.run(sync_mod._sync(stop_watcher=False, strict=True))  # must not raise
    assert any(ev == "sync_bridge_deferred_busy" and d["session"] == "sess1" for ev, d in events)
    done = [d for ev, d in events if ev == "sync_bridge_done"]
    assert done and done[0]["wrote"] is False and done[0]["reason"] == "busy"


def test_unregister_still_runs_when_strict_raises(sync_mod, mock_server, monkeypatch):
    """The finally-block unregister must run even when strict mode raises, so a
    retried worker never leaves a dangling registration."""
    _resolve(sync_mod, monkeypatch)
    _improve(sync_mod, monkeypatch, False)
    with pytest.raises(RuntimeError):
        asyncio.run(sync_mod._sync(stop_watcher=False, unregister_on_finish=True, strict=True))

    call = mock_server.assert_called("POST", UNREGISTER)
    assert call["json"]["agent_session_name"] == "agent1"
    assert len([c for c in mock_server.calls if c["path"] == UNREGISTER]) == 1


def test_missing_unregister_name_does_not_swallow_strict_error(sync_mod, mock_server, monkeypatch):
    """Guard for the return-in-finally bug: with no name to unregister, the
    skip must not consume the in-flight strict RuntimeError."""
    _resolve(sync_mod, monkeypatch, agent_session_name="", session_key="")
    _improve(sync_mod, monkeypatch, False)
    with pytest.raises(RuntimeError, match="incomplete"):
        asyncio.run(sync_mod._sync(stop_watcher=False, unregister_on_finish=True, strict=True))
    mock_server.assert_not_called("POST", UNREGISTER)
