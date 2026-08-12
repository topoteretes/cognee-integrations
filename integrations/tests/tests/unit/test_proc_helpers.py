"""The cross-platform process helpers (_proc.py): liveness and host ancestry.

Every hook that owns a background worker (idle watcher, exit watcher, the
improve lock) decides whether to respawn by asking ``pid_alive``. A false
positive strands a session with no watcher; a false negative spawns duplicates.
Neither is visible to the user until memory silently stops syncing, so the
probe's edge cases are worth pinning directly.

Contract:
  * ``pid_alive`` is True for a live process, False for reserved pids (0, 1 and
    negatives — a corrupt pidfile must never be read as "init is my worker"),
    False for a reaped child, and False rather than raising for an int too large
    for the OS;
  * ``_matches_host_exe`` matches the host binary case-insensitively, ignores
    the extension, accepts ``<stem>-*`` channel variants, and rejects mere
    prefixes/lookalikes;
  * ``_walk_ancestors`` returns the nearest matching ancestor, falls back to the
    starting pid when the host is absent, and terminates on a cyclic table.

The two suites ship a byte-identical ``_proc.py``, so parametrizing over both is
a drift guard rather than duplicated coverage: the day one integration edits it
alone, one side goes red.

The Windows liveness and process-table paths use Win32 (Toolhelp) and are
genuinely skipped off-Windows — the pure ancestry tests above cover the walk
logic on every platform.

Migrated from claude-code/tests/test_proc.py and codex/tests/test_proc.py, which
ran only in the Windows CI job and so never executed on Linux or macOS.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest


@pytest.fixture
def proc(suite, isolated_modules):
    return isolated_modules(suite, "_proc")


def test_pid_alive_is_true_for_this_process(proc):
    assert proc.pid_alive(os.getpid()) is True


def test_reserved_pids_are_never_alive(proc):
    """0, 1 and negatives are refused outright.

    pid 1 *is* alive on POSIX (init), so this is a deliberate guard rather than a
    probe result: a truncated or zeroed pidfile must not convince a hook that its
    worker is still running.
    """
    for pid in (0, 1, -5):
        assert proc.pid_alive(pid) is False, pid


def test_a_reaped_child_is_not_alive(proc):
    child = subprocess.Popen([sys.executable, "-c", ""])
    child.wait()
    assert proc.pid_alive(child.pid) is False


def test_an_out_of_range_pid_is_not_alive_and_does_not_raise(proc):
    """A corrupt pidfile can yield an int the OS cannot represent."""
    assert proc.pid_alive(2**63) is False


def test_matches_the_hosts_own_binary(proc, suite):
    stem = suite.host_stem
    for exe in (f"{stem}.exe", f"{stem.capitalize()}.EXE", stem, f"{stem}-nightly.exe"):
        assert proc._matches_host_exe(exe, stem) is True, exe


def test_does_not_match_lookalikes(proc, suite):
    """``<stem>x`` and unrelated binaries must not pass for the host.

    The empty string matters on its own: a process table row with no exe name
    would otherwise match a caller that passed an empty stem.
    """
    stem = suite.host_stem
    for exe in (f"{stem}x.exe", "code.exe", "python.exe", ""):
        assert proc._matches_host_exe(exe, stem) is False, exe


def test_walk_ancestors_finds_the_nearest_host(proc, suite):
    # 400 (python hook) -> 300 (shell) -> 200 (the host) -> 100 (explorer) -> 1
    table = {
        400: (300, "python.exe"),
        300: (200, "cmd.exe"),
        200: (100, f"{suite.host_stem}.exe"),
        100: (1, "explorer.exe"),
    }
    assert proc._walk_ancestors(table, 400, suite.host_stem) == 200


def test_walk_ancestors_falls_back_to_the_start_pid(proc, suite):
    """No host in the chain means the caller keeps whatever pid it already had."""
    table = {400: (300, "python.exe"), 300: (1, "cmd.exe")}
    assert proc._walk_ancestors(table, 400, suite.host_stem) == 400


def test_walk_ancestors_terminates_on_a_cycle(proc, suite):
    """A process claiming itself as its own parent must not hang the hook."""
    assert proc._walk_ancestors({5: (5, "a.exe")}, 5, suite.host_stem) == 5


@pytest.mark.skipif(sys.platform != "win32", reason="Toolhelp process table is Win32-only")
def test_windows_process_table_resolves_this_process(proc):
    """The real Toolhelp snapshot, on the platform that has one."""
    table = proc._process_table_windows()
    assert isinstance(table, dict) and table, "Toolhelp snapshot came back empty"

    me = os.getpid()
    assert me in table, "this process is missing from its own process table"
    _ppid, exe = table[me]
    stem = os.path.splitext(exe)[0]
    assert stem, f"no executable name for pid {me}"

    # Walking from this process, looking for this process's own binary, must
    # resolve to this process — the base case of the ancestry search.
    assert proc.find_host_ancestor_windows(me, stem) == me
