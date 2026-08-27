"""Bounded append-only logs (_logfiles.py): the cap that keeps hook.log & co. openable.

Every plugin log used to be an uncapped ``open("a")``; bootstrap.log reached
gigabytes. ``_logfiles`` gives the two writing patterns one ceiling: line writers
go through ``append_line``, and logs handed to a child as its stdout/stderr are
rotated by ``rotate_if_oversized`` at the moment they are opened for it. One
generation is kept, so a log is bounded at twice the cap and the stretch just
before the cap hit survives.

The two suites ship a byte-identical ``_logfiles.py``; parametrizing over both
keeps them that way.
"""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def lf(suite, isolated_modules):
    return isolated_modules(suite, "_logfiles")


@pytest.fixture
def log(tmp_path):
    return tmp_path / "logs" / "thing.log"


# ── the cap ────────────────────────────────────────────────────────────────


def test_default_cap_is_twenty_mib(lf, monkeypatch):
    monkeypatch.delenv(lf.MAX_BYTES_ENV, raising=False)
    assert lf.max_bytes() == 20 * 1024 * 1024 == lf.DEFAULT_MAX_BYTES


def test_cap_from_env_including_zero_and_garbage(lf, monkeypatch):
    monkeypatch.setenv(lf.MAX_BYTES_ENV, "1000")
    assert lf.max_bytes() == 1000
    monkeypatch.setenv(lf.MAX_BYTES_ENV, "0")
    assert lf.max_bytes() == 0
    monkeypatch.setenv(lf.MAX_BYTES_ENV, "lots")
    assert lf.max_bytes() == lf.DEFAULT_MAX_BYTES
    assert lf.max_bytes(default=7) == 7


# ── append_line ────────────────────────────────────────────────────────────


def test_append_creates_parents_and_adds_newline(lf, log):
    assert lf.append_line(log, "one")
    assert lf.append_line(log, "two\n")
    assert log.read_text(encoding="utf-8") == "one\ntwo\n"


def test_append_rotates_once_over_the_cap_and_keeps_one_generation(lf, log):
    first = "a" * 600
    assert lf.append_line(log, first, cap=1000)  # 601 bytes: under
    assert lf.append_line(log, "b" * 600, cap=1000)  # now 1202: over, but already written
    assert not lf.rotated_path(log).exists()  # rotation happens before the *next* write
    assert lf.append_line(log, "c", cap=1000)
    assert log.read_text(encoding="utf-8") == "c\n"
    kept = lf.rotated_path(log).read_text(encoding="utf-8")
    assert kept.startswith(first) and kept.endswith("b" * 600 + "\n")
    # A later rotation replaces .1 rather than growing a chain.
    lf.append_line(log, "d" * 1200, cap=1000)
    lf.append_line(log, "e", cap=1000)
    assert lf.rotated_path(log).read_text(encoding="utf-8") == "c\n" + "d" * 1200 + "\n"
    assert not (log.parent / "thing.log.2").exists()


def test_zero_cap_disables_rotation(lf, log):
    lf.append_line(log, "x" * 5000, cap=0)
    lf.append_line(log, "y", cap=0)
    assert log.stat().st_size > 5000
    assert not lf.rotated_path(log).exists()


def test_append_never_raises(lf, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("file", encoding="utf-8")
    assert lf.append_line(blocker / "child" / "x.log", "line") is False


# ── rotate_if_oversized (the child-fd path) ────────────────────────────────


def test_rotate_is_a_noop_for_missing_or_small_files(lf, log):
    assert lf.rotate_if_oversized(log, cap=10) is False
    log.parent.mkdir(parents=True)
    log.write_text("tiny", encoding="utf-8")
    assert lf.rotate_if_oversized(log, cap=10) is False
    assert log.exists()


def test_rotate_moves_an_oversized_file_aside(lf, log):
    log.parent.mkdir(parents=True)
    log.write_bytes(b"z" * 50)
    assert lf.rotate_if_oversized(log, cap=10) is True
    assert not log.exists()
    assert lf.rotated_path(log).read_bytes() == b"z" * 50


def test_rotate_reads_the_cap_from_env_when_not_given(lf, log, monkeypatch):
    log.parent.mkdir(parents=True)
    log.write_bytes(b"z" * 50)
    monkeypatch.setenv(lf.MAX_BYTES_ENV, "10")
    assert lf.rotate_if_oversized(log) is True


# ── the writers that matter actually use it ────────────────────────────────


def test_hook_log_is_capped(suite, isolated_modules, monkeypatch):
    """hook.log is the busiest log; a hook that loops must not grow it forever."""
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_LOG_MAX_BYTES", "2000")
    for i in range(40):
        pc.hook_log("noise", {"i": i, "pad": "x" * 100})
    assert pc._HOOK_LOG.stat().st_size <= 2000 + 300  # at most one line over the cap
    rotated = pc._HOOK_LOG.with_name("hook.log.1")
    assert rotated.exists()
    # Both halves are still valid JSONL.
    for path in (pc._HOOK_LOG, rotated):
        for line in path.read_text(encoding="utf-8").splitlines():
            assert json.loads(line)["event"] == "noise"
