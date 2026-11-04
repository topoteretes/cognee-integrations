"""The session-end sync's dedupe marker: one sync per exit, not one per hour (SDK-1071).

One exit can start two final-sync workers (the SessionEnd hook and the exit
watcher); the marker lets only one submit. It used to be keyed on the session
alone and kept for an hour, so resuming a chat and leaving it again was skipped
as "already claimed" and the new turns never synced. The key is now the session
plus its running stored-item count:

  * the two workers of one exit share a key, so only one syncs;
  * an exit after new turns (stored or buffered) has a new key and syncs;
  * an exit with nothing new keeps the key and is skipped;
  * a worker whose every attempt failed releases its claim for the next exit;
  * the count lives in the session's own append-only file, so parallel stores
    are all counted and no session can reset another's.
"""

from __future__ import annotations

import sys

import pytest

SESSION_KEY = "17962c96-c888-400a-bc69-c212901d7e11"
SESSION_ID = f"claude_{SESSION_KEY}"


@pytest.fixture
def sync(suite, hook_module, monkeypatch):
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: no detached final sync")
    module = hook_module(suite, "sync-session-to-graph.py")
    monkeypatch.setenv("COGNEE_SESSION_KEY", SESSION_KEY)
    monkeypatch.setattr(
        module, "_load_resolved", lambda: (SESSION_ID, "", "", "", False, True, SESSION_KEY)
    )
    monkeypatch.setattr(module, "_CLAIMED_MARKER", None)
    return module


def _store(n: int = 1, *, buffered: bool = False) -> None:
    import _plugin_common

    for _ in range(n):
        _plugin_common.bump_save_counter(SESSION_ID, "answer", buffered=buffered)


def test_the_two_workers_of_one_exit_sync_once(sync):
    _store(3)
    assert sync._claim_final_sync_once() is True
    assert sync._claim_final_sync_once() is False


def test_leaving_again_after_new_turns_syncs_again(sync):
    _store(3)
    assert sync._claim_final_sync_once() is True
    _store(2)  # resumed, two more items stored
    assert sync._claim_final_sync_once() is True


def test_leaving_again_with_nothing_new_is_skipped(sync):
    _store(3)
    assert sync._claim_final_sync_once() is True
    assert sync._claim_final_sync_once() is False


def test_buffered_stores_count_as_new(sync):
    _store(3)
    assert sync._claim_final_sync_once() is True
    _store(1, buffered=True)  # the server was down: the entry went to the warmup buffer
    assert sync._claim_final_sync_once() is True


def test_the_stored_count_is_never_reset_by_the_recall_header(sync):
    import _plugin_common

    _store(4)
    _plugin_common.read_and_reset_save_counter(SESSION_ID)  # every prompt does this
    assert _plugin_common.read_stored_seq(SESSION_KEY) == 4


def test_a_failed_sync_releases_its_claim(sync, monkeypatch):
    _store(3)

    async def failing(**_kw):
        raise RuntimeError("final session sync incomplete")

    monkeypatch.setattr(sync, "_sync", failing)
    monkeypatch.setattr(sys, "argv", ["sync-session-to-graph.py", "--detached-final"])
    monkeypatch.setenv("COGNEE_SYNC_RETRIES", "1")
    monkeypatch.setenv("COGNEE_SYNC_START_DELAY", "0")
    sync.main()
    assert not sync._CLAIMED_MARKER.exists()
    assert sync._claim_final_sync_once() is True  # the next exit tries again


def test_a_successful_sync_keeps_its_claim(sync, monkeypatch):
    _store(3)

    async def ok(**_kw):
        return None

    monkeypatch.setattr(sync, "_sync", ok)
    monkeypatch.setattr(sys, "argv", ["sync-session-to-graph.py", "--detached-final"])
    monkeypatch.setenv("COGNEE_SYNC_START_DELAY", "0")
    sync.main()
    assert sync._CLAIMED_MARKER.exists()
    assert sync._claim_final_sync_once() is False


def test_distinct_sessions_never_share_a_marker(sync, monkeypatch):
    _store(3)
    assert sync._claim_final_sync_once() is True
    monkeypatch.setenv("COGNEE_SESSION_KEY", "another-session")
    monkeypatch.setattr(sync, "_load_resolved", lambda: ("claude_another-session",) + ("",) * 6)
    assert sync._claim_final_sync_once() is True


def test_parallel_stores_are_all_counted(sync):
    """Store hooks run in parallel (one per tool call): no store may be lost and
    the count may never go back, or a later exit could reuse a claimed key."""
    import threading

    import _plugin_common

    threads = [threading.Thread(target=_store, args=(25,)) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _plugin_common.read_stored_seq(SESSION_KEY) == 200


def test_one_session_cannot_reset_another(sync, monkeypatch):
    import _plugin_common

    _store(3)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "another-session")
    _plugin_common.bump_save_counter("claude_another-session", "answer")
    assert _plugin_common.read_stored_seq(SESSION_KEY) == 3
    assert _plugin_common.read_stored_seq("another-session") == 1
    stored = _plugin_common._PLUGIN_DIR / "sessions" / SESSION_KEY / "stored.log"
    assert stored.stat().st_size == 3
