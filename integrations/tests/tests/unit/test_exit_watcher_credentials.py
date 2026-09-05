"""A detached watcher receives credentials through its environment, never argv."""

import json
from unittest.mock import Mock

import pytest


@pytest.mark.parametrize("explicit_key", ["watcher-test-key", ""])
def test_exit_watcher_spawn_keeps_key_out_of_argv(suite, hook_module, monkeypatch, explicit_key):
    start = hook_module(suite, "session-start.py")
    # The host lookup differs between suites; neither needs a real host process.
    for name in ("_find_claude_parent_pid", "_find_codex_parent_pid", "_find_agy_parent_pid"):
        if hasattr(start, name):
            monkeypatch.setattr(start, name, lambda: 123456)
    monkeypatch.setenv("COGNEE_API_KEY", "inherited-test-key")
    popen = Mock()
    monkeypatch.setattr(start.subprocess, "Popen", popen)

    start._spawn_exit_watcher("session-123", "test-dataset", api_key=explicit_key)

    popen.assert_called_once()
    argv = popen.call_args.args[0]
    bootstrap = json.loads(argv[-1])
    assert "api_key" not in bootstrap
    assert "watcher-test-key" not in " ".join(argv)
    assert "inherited-test-key" not in " ".join(argv)
    assert popen.call_args.kwargs["env"]["COGNEE_API_KEY"] == (explicit_key or "inherited-test-key")


def test_exit_watcher_passes_environment_key_to_final_sync(suite, hook_module, monkeypatch):
    watcher = hook_module(suite, "exit-watcher.py")
    monkeypatch.setenv("COGNEE_API_KEY", "environment-test-key")
    monkeypatch.setattr(
        watcher.sys,
        "argv",
        ["exit-watcher.py", json.dumps({"parent_pid": 123456, "session_id": "session-123"})],
    )
    monkeypatch.setattr(watcher, "_pid_alive", lambda pid: False)
    sync = Mock()
    monkeypatch.setattr(watcher, "_spawn_sync", sync)

    watcher.main()

    sync.assert_called_once()
    assert sync.call_args.kwargs["api_key"] == "environment-test-key"
