"""Unit tests for the cross-platform process helpers (_proc.py).

The Windows liveness path uses Win32 APIs and only runs on Windows; here we
exercise the POSIX liveness probe and the reserved-PID guard that every
platform shares. Run: `python integrations/claude-code/tests/test_proc.py`
(or via pytest).
"""

import os
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import _proc  # noqa: E402


def test_pid_alive_true_for_current_process():
    assert _proc.pid_alive(os.getpid()) is True


def test_pid_alive_false_for_reserved_pids():
    assert _proc.pid_alive(0) is False
    assert _proc.pid_alive(1) is False
    assert _proc.pid_alive(-5) is False


def test_pid_alive_false_for_reaped_child():
    proc = subprocess.Popen([sys.executable, "-c", ""])
    proc.wait()
    assert _proc.pid_alive(proc.pid) is False


def test_pid_alive_false_for_out_of_range_pid():
    # A corrupt pidfile can yield an int too large for the OS; the probe must
    # report not-alive, never raise into a hook.
    assert _proc.pid_alive(2**63) is False


def test_matches_host_exe():
    assert _proc._matches_host_exe("claude.exe", "claude") is True
    assert _proc._matches_host_exe("Claude.EXE", "claude") is True
    assert _proc._matches_host_exe("claude", "claude") is True
    assert _proc._matches_host_exe("claude-nightly.exe", "claude") is True
    assert _proc._matches_host_exe("codex.exe", "codex") is True
    assert _proc._matches_host_exe("claudex.exe", "claude") is False
    assert _proc._matches_host_exe("code.exe", "claude") is False
    assert _proc._matches_host_exe("", "claude") is False


def test_walk_ancestors_finds_host():
    # 400 (python hook) -> 300 (cmd) -> 200 (claude.exe) -> 100 (explorer) -> 1
    table = {
        400: (300, "python.exe"),
        300: (200, "cmd.exe"),
        200: (100, "claude.exe"),
        100: (1, "explorer.exe"),
    }
    assert _proc._walk_ancestors(table, 400, "claude") == 200


def test_walk_ancestors_returns_start_when_absent():
    table = {400: (300, "python.exe"), 300: (1, "cmd.exe")}
    assert _proc._walk_ancestors(table, 400, "claude") == 400


def test_walk_ancestors_survives_cycle():
    assert _proc._walk_ancestors({5: (5, "a.exe")}, 5, "claude") == 5


def test_process_table_and_ancestry_on_windows():
    # _process_table_windows() uses Win32 (Toolhelp); on POSIX the pure
    # _walk_ancestors / _matches_host_exe tests above cover the walk logic.
    if sys.platform != "win32":
        return
    table = _proc._process_table_windows()
    assert isinstance(table, dict) and table  # Toolhelp snapshot succeeded
    me = os.getpid()
    assert me in table  # this process is present, with (ppid, exe basename)
    _ppid, exe = table[me]
    stem = os.path.splitext(exe)[0]
    assert stem
    # the ancestry walk resolves this process by its own executable base name
    assert _proc.find_host_ancestor_windows(me, stem) == me


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("PASS", name)
            except AssertionError as e:
                failures += 1
                print("FAIL", name, e)
    sys.exit(1 if failures else 0)
