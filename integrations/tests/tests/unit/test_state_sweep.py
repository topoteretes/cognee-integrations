"""SessionStart's sweep of dead per-session state (``sweep_stale_state``).

Every launch leaves a file in half a dozen directories and nothing removed them
(1,200+ files after two months of daily use). The sweep deletes what belongs to
sessions that are provably over, and only that: age alone for the status
markers and per-session caches a live session keeps rewriting, pid death plus a
grace period for launch records the exit-watcher still reads after the host
exits, dead-pid for improve locks, TTL for the shared improve-unsupported
marker. It also rotates oversized logs that predate the cap or are only ever
written by a child process, and removes directories older versions left behind.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

DAY = 24 * 3600


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    module = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return module


def _write(path, payload, age_seconds):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    old = time.time() - age_seconds
    os.utime(path, (old, old))
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


# ── status markers and per-session caches: age only ────────────────────────


@pytest.mark.parametrize("subdir", ["conn-state", "llm-state", "recall", "bridge", "pending"])
def test_week_old_session_files_go_and_recent_ones_stay(pc, subdir):
    directory = pc._PLUGIN_DIR / subdir
    old = _write(directory / "old.json", {"x": 1}, 8 * DAY)
    fresh = _write(directory / "fresh.json", {"x": 1}, 6 * DAY)
    counts = pc.sweep_stale_state()
    assert not old.exists() and fresh.exists()
    assert counts[subdir.replace("-", "_")] == 1


def test_nothing_to_do_is_silent(pc):
    assert pc.sweep_stale_state() == {}


# ── launch records: pid death + grace, or 30 days ─────────────────────────


def test_launch_record_of_dead_host_is_removed_after_the_grace_period(pc):
    dead = _dead_pid()
    gone = _write(
        pc._SESSIONS_MAP_DIR / "gone.json", {"session_id": "s", "host_pid": dead}, 2 * DAY
    )
    just_died = _write(
        pc._SESSIONS_MAP_DIR / "just.json", {"session_id": "s", "host_pid": dead}, 3600
    )
    pc.sweep_stale_state()
    assert not gone.exists(), "dead for two days: the exit-watcher had its chance"
    assert just_died.exists(), "within the grace period the final sync may still read it"


def test_launch_record_of_live_host_survives_any_age_under_thirty_days(pc):
    live = _write(
        pc._SESSIONS_MAP_DIR / "live.json", {"session_id": "s", "host_pid": os.getpid()}, 20 * DAY
    )
    pc.sweep_stale_state()
    assert live.exists()


def test_launch_record_without_pid_is_kept_until_thirty_days(pc):
    young = _write(pc._SESSIONS_MAP_DIR / "young.json", {"session_id": "s"}, 20 * DAY)
    ancient = _write(pc._SESSIONS_MAP_DIR / "ancient.json", {"session_id": "s"}, 31 * DAY)
    pc.sweep_stale_state()
    assert young.exists() and not ancient.exists()


# ── improve locks ──────────────────────────────────────────────────────────


def test_dead_pid_and_overaged_improve_locks_are_cleared_live_ones_kept(pc):
    now = time.time()
    dead = _write(
        pc._IMPROVE_LOCK_DIR / "dead.lock",
        {"owner": "run_session_improve", "pid": _dead_pid(), "created_at": now - 60},
        60,
    )
    old = _write(
        pc._IMPROVE_LOCK_DIR / "old.lock",
        {
            "owner": "run_session_improve",
            "pid": os.getpid(),
            "created_at": now - 2 * pc.SYNC_LOCK_STALE_SECONDS,
        },
        60,
    )
    live = _write(
        pc._IMPROVE_LOCK_DIR / "live.lock",
        {"owner": "run_session_improve", "pid": os.getpid(), "created_at": now - 60},
        60,
    )
    garbage = pc._IMPROVE_LOCK_DIR / "garbage.lock"
    garbage.write_text("{not json", encoding="utf-8")
    counts = pc.sweep_stale_state()
    assert not dead.exists() and not old.exists() and not garbage.exists()
    assert live.exists()
    assert counts["improve_locks"] == 3


# ── shared improve-unsupported marker: TTL ─────────────────────────────────


def test_expired_improve_marker_is_removed_but_a_fresh_one_kept(pc):
    marker = pc._IMPROVE_UNSUPPORTED_MARKER
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(
        json.dumps({"base_url": "x", "marked_at": time.time() - 3600}), encoding="utf-8"
    )
    pc.sweep_stale_state()
    assert marker.exists(), "still inside its 24h TTL"
    marker.write_text(
        json.dumps(
            {"base_url": "x", "marked_at": time.time() - pc._IMPROVE_UNSUPPORTED_TTL_SECONDS - 1}
        ),
        encoding="utf-8",
    )
    assert pc.sweep_stale_state()["expired_markers"] == 1
    assert not marker.exists()


# ── legacy dirs and oversized logs ─────────────────────────────────────────


def test_legacy_statusline_dir_is_removed(pc):
    legacy = pc._PLUGIN_DIR / "statusline"
    legacy.mkdir(parents=True)
    (legacy / "cognee-statusline.py").write_text("print()", encoding="utf-8")
    assert pc.sweep_stale_state()["legacy_dirs"] == 1
    assert not legacy.exists()


def test_legacy_per_plugin_breaker_file_is_removed(pc):
    """cognee-search.sh used to keep its own recall-breaker.json here."""
    stray = pc._PLUGIN_DIR / "recall-breaker.json"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("{}", encoding="utf-8")
    assert pc.sweep_stale_state()["legacy_files"] == 1
    assert not stray.exists()


def test_oversized_logs_are_rotated_even_when_no_writer_touches_them(pc, monkeypatch):
    """A bootstrap.log from before the cap, or one only a child writes, still
    gets bounded at the next SessionStart."""
    monkeypatch.setenv("COGNEE_PLUGIN_LOG_MAX_BYTES", "100")
    big = pc._PLUGIN_DIR / "bootstrap.log"
    big.parent.mkdir(parents=True, exist_ok=True)
    big.write_bytes(b"x" * 500)
    small = pc._PLUGIN_DIR / "watcher.log"
    small.write_bytes(b"x" * 10)
    counts = pc.sweep_stale_state()
    assert counts["logs_rotated"] == 1
    assert not big.exists() and (pc._PLUGIN_DIR / "bootstrap.log.1").exists()
    assert small.exists()


def test_sweep_never_touches_other_plugins_state(pc, suite, temp_home):
    """Shared root: another plugin's subdir is not this sweep's to clean."""
    from utils.suites import plugin_root

    other = plugin_root(temp_home) / ("codex" if suite.name == "claude-code" else "claude-code")
    victim = _write(
        other / "sessions" / "x.json", {"session_id": "s", "host_pid": _dead_pid()}, 40 * DAY
    )
    pc.sweep_stale_state()
    assert victim.exists()


# ── it is wired into SessionStart ──────────────────────────────────────────


def test_session_start_runs_the_sweep(suite):
    source = (suite.scripts_dir / "session-start.py").read_text(encoding="utf-8")
    assert "sweep_stale_state()" in source
