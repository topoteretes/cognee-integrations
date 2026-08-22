"""One improve per session, machine-wide (_plugin_common.improve_session_lock).

The regression this locks down was root-caused from a real hook.log: the idle
watcher, ``store-to-session`` and the SessionEnd sync all bridge sessions, and
the outer ``sync_lock`` is bypassed in API mode
(``nullcontext(True) if api_mode``). 67% of sessions were submitted by two
processes at once; the server's own per-session lock answered the loser with
``{}`` (busy), driving a 15s retry loop for up to ten minutes, while concurrent
writers collided on the single-writer graph store ("Could not set lock on file")
and left pipeline runs stuck with the graph unwritten.

Pure filesystem/pid logic, so it stays a unit test. The recall-payload half of
the original file is now covered on the wire in integration/test_recall_via_http.py.

Migrated from {claude-code,codex}/tests/test_improve_session_lock.py.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import threading

import pytest


@pytest.fixture
def pc(suite, isolated_modules, tmp_path, monkeypatch):
    """_plugin_common with its improve-lock dir pointed at a temp path."""
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "_IMPROVE_LOCK_DIR", tmp_path / "improve-locks")
    monkeypatch.setattr(common, "hook_log", lambda *a, **kw: None)
    return common


def _lock_file(pc, session_id: str):
    digest = hashlib.sha1(session_id.encode()).hexdigest()
    path = pc._IMPROVE_LOCK_DIR / f"{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_second_holder_is_refused_for_the_same_session(pc):
    with pc.improve_session_lock("sess-A", "first") as a:
        assert a is True, "first claimer must win"
        with pc.improve_session_lock("sess-A", "second") as b:
            assert b is False, "concurrent claim on the SAME session must be refused"


def test_different_sessions_do_not_block_each_other(pc):
    with pc.improve_session_lock("sess-A", "first") as a:
        with pc.improve_session_lock("sess-B", "second") as b:
            assert a is True and b is True, "distinct sessions must run concurrently"


def test_lock_is_released_after_the_block(pc):
    with pc.improve_session_lock("sess-A", "first") as a:
        assert a is True
    with pc.improve_session_lock("sess-A", "second") as b:
        assert b is True, "lock must not leak once the holder finishes"


def test_lock_released_even_when_body_raises(pc):
    with pytest.raises(RuntimeError):
        with pc.improve_session_lock("sess-A", "boom"):
            raise RuntimeError("improve blew up")
    with pc.improve_session_lock("sess-A", "after") as b:
        assert b is True, "a crashing improve must not wedge the session"


def test_dead_holder_lock_is_reclaimed(pc, monkeypatch):
    """A crashed worker's lock must not strand the session forever."""
    # created_at in the far future so age alone cannot explain the reclaim — the
    # dead-pid branch must be what clears it. pid_alive is stubbed rather than
    # guessing an unused pid, so the test can't flake on a recycled pid.
    _lock_file(pc, "sess-A").write_text(
        json.dumps({"owner": "dead", "pid": 4242, "created_at": 9e9})
    )
    monkeypatch.setattr(pc._proc, "pid_alive", lambda pid: False)
    with pc.improve_session_lock("sess-A", "reclaimer") as claimed:
        assert claimed is True, "stale lock from a dead pid must be reclaimed"


def test_live_holder_lock_is_not_stolen(pc, monkeypatch):
    """The reclaim path must not evict a lock whose owner is still running."""
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    _lock_file(pc, "sess-A").write_text(
        json.dumps({"owner": "live", "pid": 4242, "created_at": now})
    )
    monkeypatch.setattr(pc._proc, "pid_alive", lambda pid: True)
    with pc.improve_session_lock("sess-A", "intruder") as claimed:
        assert claimed is False, "a live holder's lock must be respected"


def test_fresh_partial_lock_metadata_is_not_unlinked_or_bypassed(pc, monkeypatch):
    """A contender must respect the inode while its owner is still publishing metadata."""
    publication_started = threading.Event()
    release_publication = threading.Event()
    real_dump = pc.json.dump

    def delayed_first_dump(data, file_handle, *args, **kwargs):
        if isinstance(data, dict) and data.get("owner") == "first":
            publication_started.set()
            assert release_publication.wait(timeout=2), "timed out releasing lock publication"
        return real_dump(data, file_handle, *args, **kwargs)

    monkeypatch.setattr(pc.json, "dump", delayed_first_dump)

    def claim(owner):
        with pc.improve_session_lock("sess-partial", owner) as acquired:
            return acquired

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(claim, "first")
        assert publication_started.wait(timeout=2), "first lock path never became visible"
        second = executor.submit(claim, "second")
        second_acquired = second.result(timeout=2)
        release_publication.set()
        first_acquired = first.result(timeout=2)

    assert first_acquired is True
    assert second_acquired is False, "contender bypassed a live lock with partial metadata"


def test_old_unreadable_lock_is_reclaimed_after_the_existing_timeout(pc):
    lock_path = _lock_file(pc, "sess-unreadable-stale")
    lock_path.write_text("{", encoding="utf-8")
    old = dt.datetime.now(dt.timezone.utc).timestamp() - pc.SYNC_LOCK_STALE_SECONDS - 1
    os.utime(lock_path, (old, old))

    with pc.improve_session_lock("sess-unreadable-stale", "reclaimer") as claimed:
        assert claimed is True, "expired unreadable lock must remain recoverable"


def test_missing_session_id_never_blocks(pc):
    with pc.improve_session_lock("", "no-id") as a:
        with pc.improve_session_lock("", "also-no-id") as b:
            assert a is True and b is True, "no session id => no claim, must fail open"


def test_run_session_improve_skips_when_claim_is_held(pc, monkeypatch):
    """The guard lives in run_session_improve, so every caller inherits it."""
    called = []
    monkeypatch.setattr(
        pc,
        "_run_session_improve_locked",
        lambda ds, sid: called.append((ds, sid)) or True,
    )

    assert pc.run_session_improve("ds", "sess-A") is True
    assert called == [("ds", "sess-A")], "uncontended improve must run"

    with pc.improve_session_lock("sess-A", "holder"):
        assert pc.run_session_improve("ds", "sess-A") is False, "must report not-synced"
    assert len(called) == 1, "contended improve must NOT reach the submit"
