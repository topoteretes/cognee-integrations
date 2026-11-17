"""A detached watcher receives credentials through its environment, never argv."""

import json
import time
from unittest.mock import Mock

import pytest
from utils.options_holder import set_key


@pytest.mark.parametrize("explicit_key", ["watcher-test-key", ""])
def test_exit_watcher_spawn_keeps_key_out_of_argv(suite, hook_module, monkeypatch, explicit_key):
    start = hook_module(suite, "session-start.py")
    # The host lookup differs between suites; neither needs a real host process.
    for name in (
        "_find_claude_parent_pid",
        "_find_codex_parent_pid",
        "_find_agy_parent_pid",
        "_find_cursor_parent_pid",
    ):
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
    env = popen.call_args.kwargs["env"]
    if suite.api_key_env == "COGNEE_API_KEY":
        assert env["COGNEE_API_KEY"] == (explicit_key or "inherited-test-key")
    else:
        # claude-code hands the watcher no key: it works its own out from the
        # plugin option, so the key it was given travels nowhere.
        assert "watcher-test-key" not in env.values()


def test_exit_watcher_passes_environment_key_to_final_sync(suite, hook_module, monkeypatch):
    if suite.name == "claude-code":
        pytest.skip("claude-code hands the final sync its options on a pipe (below)")
    watcher = hook_module(suite, "exit-watcher.py")
    monkeypatch.setenv("COGNEE_BASE_URL", "http://managed-cognee.internal:8012")
    set_key(monkeypatch, suite, "environment-test-key")
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


def test_claude_final_sync_has_the_key_after_the_session_holder_is_gone(
    suite, hook_module, monkeypatch, tmp_path
):
    """SDK-1620 review #1: the final sync starts after Claude Code (and with it
    the session's options holder) has gone. It must still have the key, which
    the watcher read while the session ran and hands over on a pipe."""
    if suite.name != "claude-code":
        pytest.skip("the options holder is claude-code's")
    from utils.options_holder import _holder

    watcher = hook_module(suite, "exit-watcher.py")
    state = tmp_path / "holder" / "4242.json"
    _, close_holder = _holder().serve({"cognee_api_key": "ck-after-exit"}, state)
    monkeypatch.setenv("COGNEE_OPTIONS_HOLDER", str(state))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(suite.scripts_dir.parent))

    out = tmp_path / "sync-saw.json"
    probe = tmp_path / "probe_sync.py"
    probe.write_text(
        "import json, sys\n"
        "sys.path.insert(0, %r)\n"
        "import _options\n"
        "open(%r, 'w').write(json.dumps(_options.value('cognee_api_key')))\n"
        % (str(suite.scripts_dir), str(out)),
        encoding="utf-8",
    )
    monkeypatch.setattr(watcher, "_SYNC_SCRIPT", probe)
    monkeypatch.setattr(
        watcher.sys,
        "argv",
        ["exit-watcher.py", json.dumps({"parent_pid": 123456, "session_id": "session-123"})],
    )

    def claude_code_exits(pid):
        # Claude Code is gone: its holder closes and removes its record.
        close_holder()
        return False

    monkeypatch.setattr(watcher, "_pid_alive", claude_code_exits)
    watcher.main()

    assert not state.exists()
    for _ in range(200):
        if out.exists() and out.read_text(encoding="utf-8"):
            break
        time.sleep(0.05)
    assert json.loads(out.read_text(encoding="utf-8")) == "ck-after-exit"
