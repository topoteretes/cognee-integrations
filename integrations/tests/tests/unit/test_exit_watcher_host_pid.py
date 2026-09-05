"""``_find_<host>_parent_pid`` picks the pid the exit watcher waits on.

The ancestor walk returns the nearest host-named process; on the Claude desktop
app (Windows) that is the hook-runner, gone within a second of SessionStart, so
the watcher fired at once and unregistered the live connection (#391). Both
hosts export their pid (``CLAUDE_PID`` / ``CODEX_PID``): an alive value must win,
anything else must fall back to the walk.
"""

from __future__ import annotations

import os

import pytest

_HOST = {
    "claude-code": ("_find_claude_parent_pid", "CLAUDE_PID"),
    "codex": ("_find_codex_parent_pid", "CODEX_PID"),
}


@pytest.fixture
def find_host_pid(suite, hook_module, monkeypatch):
    fn_name, env_var = _HOST[suite.name]
    module = hook_module(suite, "session-start.py")
    fn = getattr(module, fn_name)
    monkeypatch.delenv("CLAUDE_PID", raising=False)
    monkeypatch.delenv("CODEX_PID", raising=False)
    return fn, env_var, module


def test_alive_exported_pid_wins(find_host_pid, monkeypatch):
    fn, env_var, _ = find_host_pid
    monkeypatch.setenv(env_var, str(os.getpid()))
    assert fn() == os.getpid()


@pytest.mark.parametrize("bad", ["999999999", "0", "-5", "garbage", ""])
def test_unusable_exported_pid_falls_back(find_host_pid, monkeypatch, bad):
    fn, env_var, module = find_host_pid
    monkeypatch.setenv(env_var, bad)
    pid = fn()
    assert isinstance(pid, int) and pid > 1
    assert str(pid) != bad
    assert module._pid_alive(pid)


def test_absent_exported_pid_falls_back(find_host_pid):
    fn, _, module = find_host_pid
    pid = fn()
    assert isinstance(pid, int) and pid > 1
    assert module._pid_alive(pid)
