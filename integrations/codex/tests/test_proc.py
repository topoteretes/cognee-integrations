"""Unit tests for the cross-platform process helpers (_proc.py).

The Windows liveness path uses Win32 APIs and only runs on Windows; here we
exercise the POSIX liveness probe and the reserved-PID guard that every
platform shares. Run: `python integrations/codex/tests/test_proc.py`
(or via pytest).
"""

import os
import pathlib
import subprocess
import sys

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins" / "cognee" / "scripts")
)

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
    assert _proc._matches_host_exe("codex.exe", "codex") is True
    assert _proc._matches_host_exe("Codex.EXE", "codex") is True
    assert _proc._matches_host_exe("codex", "codex") is True
    assert _proc._matches_host_exe("codex-nightly.exe", "codex") is True
    assert _proc._matches_host_exe("claude.exe", "claude") is True
    assert _proc._matches_host_exe("codexy.exe", "codex") is False
    assert _proc._matches_host_exe("code.exe", "codex") is False
    assert _proc._matches_host_exe("", "codex") is False


def test_walk_ancestors_finds_host():
    # 400 (python hook) -> 300 (sh) -> 200 (codex.exe) -> 100 (explorer) -> 1
    table = {
        400: (300, "python.exe"),
        300: (200, "sh.exe"),
        200: (100, "codex.exe"),
        100: (1, "explorer.exe"),
    }
    assert _proc._walk_ancestors(table, 400, "codex") == 200


def test_walk_ancestors_returns_start_when_absent():
    table = {400: (300, "python.exe"), 300: (1, "sh.exe")}
    assert _proc._walk_ancestors(table, 400, "codex") == 400


def test_walk_ancestors_survives_cycle():
    assert _proc._walk_ancestors({5: (5, "a.exe")}, 5, "codex") == 5


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
