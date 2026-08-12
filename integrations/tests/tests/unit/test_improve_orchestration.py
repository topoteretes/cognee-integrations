"""Orchestration around the improve bridge (_plugin_common.run_session_improve).

These stay at the seam: what is under test is the decision sequence — drain the
warmup buffer, improve, fall back to the legacy document bridge only when the
server genuinely lacks the endpoint, retry while the per-session improve lock is
busy, and report failure when entries were left undelivered. The wire-level
submit is covered in integration/test_improve_http.py.

Migrated from {claude-code,codex}/tests/test_improve_sync.py.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


@pytest.fixture
def run_improve(pc, monkeypatch):
    """Drive run_session_improve with every seam mocked; return (wrote, calls)."""

    def _run(improve_result, *, unsupported_marker=False, drain_results=None, improve_results=None):
        calls = {"drain": 0, "improve": 0, "legacy": 0}

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

        def _legacy(dataset, session):
            calls["legacy"] += 1
            return True

        monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
        monkeypatch.setattr(pc, "_backend_reachable", lambda url: True)
        monkeypatch.setattr(pc, "drain_warmup_entries", _drain)
        monkeypatch.setattr(pc, "ensure_dataset_via_http", lambda d: None)
        monkeypatch.setattr(pc, "improve_unsupported", lambda url: unsupported_marker)
        monkeypatch.setattr(pc, "improve_session_via_http", _improve)
        monkeypatch.setattr(pc, "persist_session_cache_to_graph_via_http", _legacy)
        monkeypatch.setattr(pc, "_DRAIN_RETRY_PAUSE_SECONDS", 0.0)

        return pc.run_session_improve("ds", "sid"), calls

    return _run


def test_happy_path_drains_then_improves(run_improve):
    wrote, calls = run_improve({"ok": True})
    assert wrote is True
    assert calls == {"drain": 1, "improve": 1, "legacy": 0}


def test_falls_back_when_unsupported_response(run_improve):
    wrote, calls = run_improve({"ok": False, "unsupported": True, "status": 404})
    assert wrote is True
    assert calls["improve"] == 1
    assert calls["legacy"] == 1


def test_skips_improve_when_marker_set(run_improve):
    wrote, calls = run_improve({"ok": True}, unsupported_marker=True)
    assert wrote is True
    assert calls["improve"] == 0  # marker short-circuits straight to legacy
    assert calls["legacy"] == 1


def test_error_returns_false_without_legacy(run_improve):
    wrote, calls = run_improve({"ok": False, "status": 500, "error": "boom"})
    assert wrote is False
    assert calls["legacy"] == 0  # a transient server error is not an unsupported server


def test_retries_busy_until_lock_frees(run_improve, monkeypatch):
    # A lock-skipped improve may have snapshotted the cache before the latest
    # turns — run_session_improve must re-submit until a run actually lands.
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", "0.1")
    wrote, calls = run_improve(
        None,
        improve_results=[{"ok": False, "busy": True}, {"ok": False, "busy": True}, {"ok": True}],
    )
    assert wrote is True
    assert calls["improve"] == 3


def test_busy_deadline_gives_up(run_improve, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_RETRY_INTERVAL", "0.1")
    monkeypatch.setenv("COGNEE_IMPROVE_BUSY_DEADLINE", "0.25")
    wrote, calls = run_improve({"ok": False, "busy": True})
    assert wrote is False  # still busy at deadline -> reported as not-synced
    assert calls["improve"] >= 2  # at least one retry happened


def test_incomplete_drain_returns_false_but_improve_still_runs(run_improve):
    # Undelivered warmup entries mean the improve persisted an incomplete
    # session: the improve must still run (partial persist beats none), but the
    # sync must report failure so the caller's retry loop re-drives it.
    wrote, calls = run_improve({"ok": True}, drain_results=[(0, 3), (0, 3)])
    assert wrote is False
    assert calls["improve"] == 1  # improve ran despite the incomplete drain
    assert calls["drain"] == 2  # one in-place retry happened
    assert calls["legacy"] == 0


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
