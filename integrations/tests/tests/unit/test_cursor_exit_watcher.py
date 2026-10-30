"""Cursor's exit watcher: which process it waits on, and one watcher per chat.

Unlike the Codex twin, ``_find_cursor_parent_pid`` has no fallback to the hook's
own parent: under Cursor that parent is ``cursor_hook.py``, gone as soon as
SessionStart returns, so a watcher on it ran the final sync and unregistered the
connection seconds into the session. And in the IDE every chat of a window shares
the extension host, so the pidfile is keyed by host pid *and* session, and
``cursor_hook.py`` removes it when that chat's sessionEnd sync has run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from utils.suites import CURSOR

ADAPTER_PATH = CURSOR.scripts_dir / "cursor_hook.py"


@pytest.fixture
def start(hook_module, monkeypatch):
    module = hook_module(CURSOR, "session-start.py")
    monkeypatch.delenv("CURSOR_PID", raising=False)
    return module


def _ps(module, monkeypatch, rows: list[tuple[int, int, str]]) -> None:
    table = "\n".join(f"{pid} {ppid} {command}" for pid, ppid, command in rows)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *a, **k: table)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps walk")
def test_no_cursor_ancestor_means_no_host_not_the_hooks_own_parent(start, monkeypatch):
    me, parent = os.getpid(), os.getppid()
    _ps(
        start,
        monkeypatch,
        [
            (parent, 1, "/usr/bin/python3 /plugin/scripts/cursor_hook.py session-start.py"),
            (me, parent, "/usr/bin/python3 hook_runner.py session-start.py"),
        ],
    )
    assert start._find_cursor_parent_pid() == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ps walk")
@pytest.mark.parametrize(
    "command",
    [
        "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper (Plugin).app/x/cursor --type=x",
        "/home/u/.local/bin/cursor-agent --resume",
        "node /home/u/.local/share/cursor-agent/versions/2026.09.26/index.js",
    ],
)
def test_the_nearest_cursor_ancestor_is_the_host(start, monkeypatch, command):
    parent = os.getppid()
    _ps(
        start,
        monkeypatch,
        [(4242, 1, command), (parent, 4242, "/usr/bin/python3 /plugin/scripts/cursor_hook.py")],
    )
    assert start._find_cursor_parent_pid() == 4242


def test_alive_cursor_pid_wins(start, monkeypatch):
    monkeypatch.setenv("CURSOR_PID", str(os.getpid()))
    assert start._find_cursor_parent_pid() == os.getpid()


def test_no_watcher_without_a_host(start, monkeypatch):
    monkeypatch.setattr(start, "_find_cursor_parent_pid", lambda: 0)
    popen = Mock()
    monkeypatch.setattr(start.subprocess, "Popen", popen)
    start._spawn_exit_watcher("sess", "ds", session_key="conv-1")
    popen.assert_not_called()


def test_each_chat_in_a_window_gets_its_own_watcher(start, monkeypatch):
    """Two chats under one extension host: both watchers start, each keyed to
    its own session, so each chat is synced when the window closes."""
    monkeypatch.setattr(start, "_find_cursor_parent_pid", lambda: 4242)
    popen = Mock()
    monkeypatch.setattr(start.subprocess, "Popen", popen)
    start._spawn_exit_watcher("sess-a", "ds", session_key="conv-a")
    start._spawn_exit_watcher("sess-b", "ds", session_key="conv-b")
    assert popen.call_count == 2
    pidfiles = [json.loads(call.args[0][-1])["pidfile"] for call in popen.call_args_list]
    assert [Path(p).name for p in pidfiles] == ["4242-conv-a.pid", "4242-conv-b.pid"]


@pytest.fixture
def adapter(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    spec = importlib.util.spec_from_file_location("cursor_hook_exit_watcher", ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop("cursor_hook_exit_watcher", None)


class _Stdin:
    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


def test_session_end_stops_only_that_chats_watcher(adapter, monkeypatch, capsys):
    watchers = adapter.state_dir() / "exit-watchers"
    watchers.mkdir(parents=True)
    for name in ("4242-conv-a.pid", "4242-conv-b.pid"):
        (watchers / name).write_text("123", encoding="utf-8")

    monkeypatch.setattr(adapter, "_run_script", lambda payload, script, flags: "{}")
    end = {"hook_event_name": "sessionEnd", "conversation_id": "conv-a", "reason": "user_close"}
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(end)))
    assert adapter.main(["sync-session-to-graph.py", "--session-end"]) == 0
    capsys.readouterr()
    assert sorted(p.name for p in watchers.iterdir()) == ["4242-conv-b.pid"]

    # A failed sync keeps the watcher: it is then the only way the chat gets synced.
    def boom(payload, script, flags):
        raise RuntimeError("server down")

    monkeypatch.setattr(adapter, "_run_script", boom)
    end_b = dict(end, conversation_id="conv-b")
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(end_b)))
    assert adapter.main(["sync-session-to-graph.py", "--session-end"]) == 0
    capsys.readouterr()
    assert sorted(p.name for p in watchers.iterdir()) == ["4242-conv-b.pid"]
