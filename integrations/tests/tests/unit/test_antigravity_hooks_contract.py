"""Manifest and plugin-surface contract for the Antigravity Cognee plugin."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest
from utils.suites import ANTIGRAVITY

INTEGRATIONS_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = INTEGRATIONS_ROOT / "antigravity"
PLUGIN_JSON = PLUGIN_ROOT / "plugin.json"
HOOKS_JSON = PLUGIN_ROOT / "hooks.json"
POSIX_HOOK_RUNNER = PLUGIN_ROOT / "scripts" / "run-agy-hook"
WINDOWS_HOOK_RUNNER = PLUGIN_ROOT / "scripts" / "run-agy-hook.cmd"
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
EXPECTED_EVENTS = {"PreInvocation", "PostToolUse", "Stop"}
NAMED_HOOKS = ("cognee-bootstrap", "cognee-recall", "cognee-capture", "cognee-stop")


@pytest.fixture
def plugin_root() -> Path:
    if not PLUGIN_JSON.is_file():
        pytest.skip(f"Antigravity plugin has not been implemented: {PLUGIN_ROOT}")
    return PLUGIN_ROOT


@pytest.fixture
def manifest(plugin_root: Path) -> dict:
    assert HOOKS_JSON.is_file(), f"missing Antigravity hook manifest: {HOOKS_JSON}"
    return json.loads(HOOKS_JSON.read_text(encoding="utf-8"))


def _flat_handlers(manifest: dict, name: str, event: str) -> list[dict]:
    hooks = manifest[name][event]
    assert isinstance(hooks, list), f"{event} must contain a list of hook entries"
    assert all(isinstance(hook, dict) for hook in hooks), f"{event} hooks must be objects"
    return hooks


def _grouped_handlers(manifest: dict, name: str, event: str) -> list[dict]:
    groups = _flat_handlers(manifest, name, event)
    handlers = [handler for group in groups for handler in group.get("hooks", [])]
    assert all(isinstance(handler, dict) for handler in handlers)
    return handlers


def _handlers(manifest: dict, name: str, event: str) -> list[dict]:
    if event == "PostToolUse":
        return _grouped_handlers(manifest, name, event)
    return _flat_handlers(manifest, name, event)


def _commands(manifest: dict, name: str, event: str) -> list[str]:
    return [str(hook.get("command", "")) for hook in _handlers(manifest, name, event)]


def _command_tokens(command: str) -> tuple[str, tuple[str, ...]]:
    assert not any(token in command for token in ("$", "`", "$(", "~", "..")), command
    tokens = shlex.split(command)
    assert tokens[0] == "scripts/run-agy-hook", tokens
    assert "||" not in command, command
    return tokens[0], tuple(tokens[1:])


@contextmanager
def _isolated_plugin_scripts(monkeypatch, tmp_path):
    """Load plugin modules against an empty, temporary home directory."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for key in (
        "COGNEE_ENV_FILE",
        "COGNEE_AGENT_NAME",
        "COGNEE_SESSION_PREFIX",
        "COGNEE_SESSION_KEY",
        "COGNEE_ANTIGRAVITY_PLUGIN_ROOT",
        "AGY_CWD",
        "SYSTEM_ROOT_DIRECTORY",
        "DATA_ROOT_DIRECTORY",
        "CACHE_ROOT_DIRECTORY",
    ):
        monkeypatch.delenv(key, raising=False)

    names = (
        "_env_file",
        "_proc",
        "_recall_http",
        "_plugin_common",
        "config",
        "cognee_statusline_render",
    )
    saved_modules = {name: sys.modules.pop(name, None) for name in names}
    sys.path.insert(0, str(SCRIPTS_DIR))
    try:
        yield home
    finally:
        sys.path.remove(str(SCRIPTS_DIR))
        for name, module in saved_modules.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module


def _load_script_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS_DIR / filename)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _has_posix_bash() -> bool:
    return sys.platform != "win32" and shutil.which("bash") is not None


EXPECTED_HANDLER_ARGS = {
    ("cognee-bootstrap", "PreInvocation"): [("session-start.py",)],
    ("cognee-recall", "PreInvocation"): [("session-context-lookup.py",)],
    ("cognee-capture", "PreInvocation"): [("store-user-prompt.py",)],
    ("cognee-capture", "PostToolUse"): [("store-to-session.py",)],
    ("cognee-stop", "Stop"): [
        ("store-to-session.py", "--stop"),
        ("sync-session-to-graph.py",),
    ],
}


def test_plugin_manifest_exists_at_antigravity_root():
    assert PLUGIN_JSON.is_file(), f"missing Antigravity plugin manifest: {PLUGIN_JSON}"


def test_plugin_manifest_identifies_cognee_with_a_version_string(plugin_root):
    spec = json.loads(PLUGIN_JSON.read_text(encoding="utf-8"))

    assert spec["name"] == "cognee"
    assert spec["version"] == "1.4.3"


def test_hooks_manifest_is_at_plugin_root_not_claude_hooks_directory(manifest):
    assert HOOKS_JSON.is_file()
    assert not (PLUGIN_ROOT / "hooks" / "hooks.json").exists()


def test_hooks_manifest_uses_named_hooks_top_level_not_a_claude_wrapper(manifest):
    assert set(manifest) == set(NAMED_HOOKS)
    assert "hooks" not in manifest
    assert all(isinstance(manifest[name], dict) for name in NAMED_HOOKS)


def test_manifest_registers_only_antigravity_events(manifest):
    events = {event for name in NAMED_HOOKS for event in manifest[name]}

    assert events == EXPECTED_EVENTS
    assert not events & {
        "SessionStart",
        "UserPromptSubmit",
        "PreCompact",
        "BeforeAgent",
        "AfterTool",
    }


def test_pre_invocation_wraps_start_lookup_and_prompt_scripts(manifest):
    for (name, event), expected_args in EXPECTED_HANDLER_ARGS.items():
        if event != "PreInvocation":
            continue
        actual_args = [_command_tokens(command)[1] for command in _commands(manifest, name, event)]
        assert actual_args == expected_args


def test_post_tool_use_matches_all_tools_and_wraps_non_stop_storage(manifest):
    hooks = _flat_handlers(manifest, "cognee-capture", "PostToolUse")
    commands = _commands(manifest, "cognee-capture", "PostToolUse")

    assert all(hook.get("matcher") == "*" for hook in hooks)
    assert [_command_tokens(command)[1] for command in commands] == EXPECTED_HANDLER_ARGS[
        ("cognee-capture", "PostToolUse")
    ]


def test_stop_stores_assistant_message_then_syncs_without_session_teardown(manifest):
    """Catches treating a normal execution-loop Stop as host/session exit."""
    commands = _commands(manifest, "cognee-stop", "Stop")
    assert [_command_tokens(command)[1] for command in commands] == EXPECTED_HANDLER_ARGS[
        ("cognee-stop", "Stop")
    ]


def test_execution_stop_defers_to_a_nonfinal_worker(hook_module, monkeypatch):
    """Catches running the expensive improve pipeline in the synchronous Stop window."""
    sync = hook_module(ANTIGRAVITY, "sync-session-to-graph.py")
    spawned = []
    direct_syncs = []

    async def direct_sync(*args, **kwargs):
        direct_syncs.append((args, kwargs))

    monkeypatch.setattr(
        sync, "_spawn_detached_sync", lambda *, final: spawned.append(final) or True
    )
    monkeypatch.setattr(sync, "_sync", direct_sync)
    monkeypatch.setattr(
        sync, "resolve_session_key_from_payload", lambda _payload: ("host-1", "test")
    )
    monkeypatch.setattr(sync, "set_session_key", lambda value: value)
    monkeypatch.setattr(sync, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync.sys, "argv", ["sync-session-to-graph.py", "--execution-stop"])
    monkeypatch.setattr(
        sync.sys,
        "stdin",
        io.StringIO(
            json.dumps({"hook_event_name": "Stop", "session_id": "host-1", "fullyIdle": True})
        ),
    )

    sync.main()

    assert spawned == [False]
    assert direct_syncs == []


def test_nonfinal_worker_cannot_inherit_unregister_authority(hook_module, monkeypatch):
    """Catches granting execution Stop the exit watcher's lifecycle authority."""
    sync = hook_module(ANTIGRAVITY, "sync-session-to-graph.py")
    launches = []

    def fake_popen(command, **kwargs):
        launches.append((command, kwargs))
        return object()

    monkeypatch.setattr(sync.subprocess, "Popen", fake_popen)
    monkeypatch.setenv("COGNEE_UNREGISTER_ON_FINISH", "1")

    assert sync._spawn_detached_sync(final=False) is True
    assert launches[0][0][-1] == "--detached-execution"
    assert "COGNEE_UNREGISTER_ON_FINISH" not in launches[0][1]["env"]

    assert sync._spawn_detached_sync(final=True) is True
    assert launches[1][0][-1] == "--detached-final"
    assert launches[1][1]["env"]["COGNEE_UNREGISTER_ON_FINISH"] == "1"


def test_failed_execution_dispatch_is_retryable_without_adapter_done_marker(
    hook_module, monkeypatch, tmp_path
):
    """Catches a failed worker spawn being acknowledged as completed Stop work."""
    adapter_spec = importlib.util.spec_from_file_location(
        "antigravity_dispatch_retry_adapter", SCRIPTS_DIR / "agy_hook.py"
    )
    assert adapter_spec and adapter_spec.loader
    adapter = importlib.util.module_from_spec(adapter_spec)
    monkeypatch.setitem(sys.modules, adapter_spec.name, adapter)
    adapter_spec.loader.exec_module(adapter)
    sync = hook_module(ANTIGRAVITY, "sync-session-to-graph.py")
    dispatches = iter([False, True])
    monkeypatch.setattr(sync, "_spawn_detached_sync", lambda *, final: next(dispatches))
    monkeypatch.setattr(
        sync, "resolve_session_key_from_payload", lambda _payload: ("host-1", "test")
    )
    monkeypatch.setattr(sync, "set_session_key", lambda value: value)
    monkeypatch.setattr(sync, "hook_log", lambda *_args, **_kwargs: None)
    payload = adapter.normalize_payload(
        {
            "conversationId": "conversation-retry-dispatch",
            "executionId": "execution-retry-dispatch",
            "fullyIdle": True,
        },
        "sync-session-to-graph.py",
    )

    def runner(inner_payload, _script):
        monkeypatch.setattr(sync.sys, "argv", ["sync-session-to-graph.py", "--execution-stop"])
        monkeypatch.setattr(sync.sys, "stdin", io.StringIO(json.dumps(inner_payload)))
        return_code = sync.main()
        if return_code:
            raise subprocess.CalledProcessError(return_code, sync.sys.argv)
        return {}

    with pytest.raises(subprocess.CalledProcessError):
        adapter.run_inner_hook(
            payload,
            "sync-session-to-graph.py",
            runner=runner,
            marker_dir=tmp_path,
        )
    assert not list(tmp_path.glob("*.done"))

    assert (
        adapter.run_inner_hook(
            payload,
            "sync-session-to-graph.py",
            runner=runner,
            marker_dir=tmp_path,
        )
        == {}
    )
    assert len(list(tmp_path.glob("*.done"))) == 1


def test_detached_execution_worker_retries_strictly_without_final_authority(
    hook_module, monkeypatch
):
    """Catches a busy/incomplete execution sync being treated as one-shot success."""
    sync = hook_module(ANTIGRAVITY, "sync-session-to-graph.py")
    attempts = []
    sleeps = []

    async def flaky_sync(*, stop_watcher, unregister_on_finish, strict):
        attempts.append((stop_watcher, unregister_on_finish, strict))
        if len(attempts) < 3:
            raise RuntimeError("busy or ambiguous improve")

    monkeypatch.setattr(sync, "_sync", flaky_sync)
    monkeypatch.setattr(
        sync,
        "_claim_final_sync_once",
        lambda: pytest.fail("execution worker claimed final-sync authority"),
    )
    monkeypatch.setattr(sync, "_stop_idle_watcher", lambda: pytest.fail("watcher stopped"))
    monkeypatch.setattr(sync.time, "sleep", sleeps.append)
    monkeypatch.setattr(sync, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync.sys, "argv", ["sync-session-to-graph.py", "--detached-execution"])
    monkeypatch.setenv("COGNEE_SYNC_RETRIES", "3")
    monkeypatch.setenv("COGNEE_SYNC_RETRY_DELAY", "0.25")
    monkeypatch.setenv("COGNEE_UNREGISTER_ON_FINISH", "1")

    assert sync.main() == 0
    assert attempts == [(False, False, True)] * 3
    assert sleeps == [0.25, 0.25]


def test_detached_execution_worker_returns_nonzero_after_retry_exhaustion(hook_module, monkeypatch):
    """Catches an exhausted execution worker being reported as successful."""
    sync = hook_module(ANTIGRAVITY, "sync-session-to-graph.py")
    attempts = []

    async def failed_sync(*, stop_watcher, unregister_on_finish, strict):
        attempts.append((stop_watcher, unregister_on_finish, strict))
        raise RuntimeError("busy")

    monkeypatch.setattr(sync, "_sync", failed_sync)
    monkeypatch.setattr(sync.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(sync, "hook_log", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(sync.sys, "argv", ["sync-session-to-graph.py", "--detached-execution"])
    monkeypatch.setenv("COGNEE_SYNC_RETRIES", "2")
    monkeypatch.setenv("COGNEE_SYNC_RETRY_DELAY", "0")

    assert sync.main() == 1
    assert attempts == [(False, False, True)] * 2


def test_all_hook_commands_are_relative_runner_commands_with_safe_second_timeouts(manifest):
    failures: list[str] = []
    for name in NAMED_HOOKS:
        for event in manifest[name]:
            for hook in _handlers(manifest, name, event):
                command = str(hook.get("command", ""))
                timeout = hook.get("timeout")
                try:
                    _command_tokens(command)
                except (AssertionError, IndexError, ValueError) as error:
                    failures.append(f"{name}/{event}: invalid command {command!r}: {error}")
                if not command:
                    failures.append(f"{name}/{event}: invalid command {command!r}")
                if (
                    not isinstance(timeout, (int, float))
                    or isinstance(timeout, bool)
                    or not 0 < timeout <= 600
                ):
                    failures.append(
                        f"{name}/{event}: timeout must be numeric seconds <= 600, got {timeout!r}"
                    )

    assert not failures, "\n".join(failures)


def test_hook_runner_selects_an_interpreter_before_running_the_adapter(plugin_root):
    assert POSIX_HOOK_RUNNER.is_file(), f"missing POSIX hook runner: {POSIX_HOOK_RUNNER}"
    assert WINDOWS_HOOK_RUNNER.is_file(), f"missing Windows hook runner: {WINDOWS_HOOK_RUNNER}"
    assert POSIX_HOOK_RUNNER.stat().st_mode & stat.S_IXUSR

    posix = POSIX_HOOK_RUNNER.read_text(encoding="utf-8")
    windows = WINDOWS_HOOK_RUNNER.read_text(encoding="utf-8")

    assert "command -v python3" in posix
    assert 'exec python3 "$(dirname "$0")/agy_hook.py" "$@"' in posix
    assert 'exec python "$(dirname "$0")/agy_hook.py" "$@"' in posix
    assert "||" not in posix

    assert "where python3 >nul 2>nul" in windows
    assert "if errorlevel 1 goto use_python" in windows
    assert 'python3 "%~dp0agy_hook.py" %*' in windows
    assert 'python "%~dp0agy_hook.py" %*' in windows
    assert "||" not in windows


def test_session_identity_uses_agy_cwd_and_antigravity_agent_suffix(monkeypatch, tmp_path):
    """Catches a regression to CODEX_CWD, a Codex prefix, or a non-_agy agent."""
    workspace = tmp_path / "workspace-from-agy"
    workspace.mkdir()

    with _isolated_plugin_scripts(monkeypatch, tmp_path):
        monkeypatch.setenv("AGY_CWD", str(workspace))
        common = _load_script_module("_plugin_common", "_plugin_common.py")

        assert (
            common._generate_session_id(host_key="conversation-17") == "antigravity_conversation-17"
        )
        assert common._generate_session_id().startswith("antigravity_workspace-from-agy_")
        assert common._resolve_agent_name() == "antigravity-agent_agy"


def test_private_hook_state_and_shared_runtime_roots_are_used(monkeypatch, tmp_path):
    """Catches moving logs into shared root or moving server/key state into private root."""
    with _isolated_plugin_scripts(monkeypatch, tmp_path) as home:
        common = _load_script_module("_plugin_common", "_plugin_common.py")

        common.hook_log("behavioral-root-check")
        common.save_cached_api_key("http://cognee.test", "cached-key")
        common.write_server_pidfile(8123, 4242, version="1.5.0")

        private_root = home / ".cognee-plugin" / "antigravity"
        shared_root = home / ".cognee-plugin"
        assert json.loads((private_root / "hook.log").read_text(encoding="utf-8"))["event"] == (
            "behavioral-root-check"
        )
        assert common.load_cached_api_key("http://cognee.test") == "cached-key"
        assert json.loads((shared_root / "api_key.json").read_text(encoding="utf-8"))[
            "api_key"
        ] == ("cached-key")
        assert (
            json.loads((shared_root / "server-8123.pid").read_text(encoding="utf-8"))["pid"] == 4242
        )
        assert not (private_root / "api_key.json").exists()
        assert not (private_root / "server-8123.pid").exists()


def test_shared_config_file_is_resolved_outside_private_state(monkeypatch, tmp_path):
    """Catches config.json moving into the Antigravity-private state directory."""
    with _isolated_plugin_scripts(monkeypatch, tmp_path) as home:
        config = _load_script_module("config", "config.py")
        settings = dict(config._DEFAULTS)
        settings["agent_name"] = "configured-agent"
        config.save_config(settings)

        shared_config = home / ".cognee-plugin" / "config.json"
        private_config = home / ".cognee-plugin" / "antigravity" / "config.json"
        assert (
            json.loads(shared_config.read_text(encoding="utf-8"))["agent_name"]
            == "configured-agent"
        )
        assert config.load_config()["agent_name"] == "configured-agent"
        assert not private_config.exists()


def test_shared_venv_path_is_resolved_outside_private_state(monkeypatch, tmp_path):
    """Catches the managed venv moving into the Antigravity-private state directory."""
    with _isolated_plugin_scripts(monkeypatch, tmp_path) as home:
        common = _load_script_module("_plugin_common", "_plugin_common.py")

        expected_venv = home / ".cognee-plugin" / "venv"
        assert common.venv_python().parent.parent == expected_venv
        assert not str(common.venv_python()).startswith(
            str(home / ".cognee-plugin" / "antigravity")
        )


def test_shared_cognee_database_dirs_are_resolved_outside_private_state(monkeypatch, tmp_path):
    """Catches Cognee system/data/cache roots moving under plugin-private state."""
    with _isolated_plugin_scripts(monkeypatch, tmp_path) as home:
        _load_script_module("_plugin_common", "_plugin_common.py")

        expected = home / ".cognee"
        assert Path(os.environ["SYSTEM_ROOT_DIRECTORY"]) == expected / "system"
        assert Path(os.environ["DATA_ROOT_DIRECTORY"]) == expected / "data"
        assert Path(os.environ["CACHE_ROOT_DIRECTORY"]) == expected / "cache"


def test_posix_shell_guard_disables_bash_fixture_on_windows(monkeypatch):
    """Catches removing the Windows guard before the POSIX shell subprocess runs."""
    monkeypatch.setattr(sys, "platform", "win32")
    assert not _has_posix_bash()


@pytest.mark.skipif(not _has_posix_bash(), reason="requires POSIX bash; Windows uses cmd /c")
def test_search_shell_exports_the_private_antigravity_state_dir(monkeypatch, tmp_path):
    """Catches cognee-search.sh regressing its exported breaker state to another host."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture = tmp_path / "state-dir.txt"
    fake_cli = bin_dir / "cognee-cli"
    fake_cli.write_text(
        "#!/bin/sh\n"
        'printf \'%s\' "$COGNEE_PLUGIN_STATE_DIR" > "$COGNEE_CAPTURE"\n'
        "printf '[]\\n'\n",
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "PATH": f"{bin_dir}{os.pathsep}{env['PATH']}",
            "COGNEE_CAPTURE": str(capture),
            "COGNEE_BASE_URL": "http://127.0.0.1:1",
            "COGNEE_API_KEY": "",
            "COGNEE_LOCAL_API_URL": "",
        }
    )
    result = subprocess.run(
        ["bash", str(SCRIPTS_DIR / "cognee-search.sh"), "question", "--session"],
        cwd=tmp_path,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert result.stdout.strip() == "[]"
    assert capture.read_text(encoding="utf-8") == str(home / ".cognee-plugin" / "antigravity")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process ancestry fixture")
def test_parent_discovery_finds_a_real_agy_ancestor(tmp_path):
    """Catches changing the POSIX host matcher from agy to another executable stem."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    agy = bin_dir / "agy"
    agy.write_text(
        '#!/bin/sh\n"$PYTHON_EXECUTABLE" -c "$1"\nstatus=$?\nexit $status\n',
        encoding="utf-8",
    )
    agy.chmod(0o755)

    session_start = str(SCRIPTS_DIR / "session-start.py")
    child = f"""\
import importlib.util
import json
import os
import sys
sys.path.insert(0, {str(SCRIPTS_DIR)!r})
spec = importlib.util.spec_from_file_location(
    "session_start_under_agy", {session_start!r}
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
payload = {{
    "parent": os.getppid(),
    "found": module._find_agy_parent_pid(),
    "agy": int(os.environ["AGY_FIXTURE_PID"]),
}}
print(json.dumps(payload))
"""
    wrapper = (
        "import subprocess, sys; "
        f"print(subprocess.check_output([sys.executable, '-c', {child!r}], text=True), end='')"
    )
    launcher = f"""\
import os
import subprocess
import sys
env = os.environ.copy()
env["AGY_FIXTURE_PID"] = str(os.getppid())
result = subprocess.check_output([sys.executable, "-c", {wrapper!r}], env=env, text=True)
print(result, end="")
"""
    env = os.environ.copy()
    env.update({"HOME": str(home), "USERPROFILE": str(home), "PYTHON_EXECUTABLE": sys.executable})
    result = subprocess.run(
        [str(agy), launcher],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )

    payload = json.loads(result.stdout.splitlines()[-1])
    assert payload["found"] == payload["agy"]
    assert payload["found"] != payload["parent"]


def test_manifest_version_lookup_reads_the_override_plugin_root(monkeypatch, tmp_path):
    """Catches version lookup regressing to .codex-plugin or another host root."""
    override_root = tmp_path / "override-plugin"
    override_root.mkdir()
    (override_root / "plugin.json").write_text('{"version":"9.9.9"}', encoding="utf-8")
    legacy = override_root / ".codex-plugin"
    legacy.mkdir()
    (legacy / "plugin.json").write_text('{"version":"0.0.1"}', encoding="utf-8")

    with _isolated_plugin_scripts(monkeypatch, tmp_path):
        monkeypatch.setenv("COGNEE_ANTIGRAVITY_PLUGIN_ROOT", str(override_root))
        common = _load_script_module("_plugin_common", "_plugin_common.py")
        statusline = _load_script_module("cognee_statusline_render", "cognee_statusline_render.py")

        assert common._installed_plugin_version() == "9.9.9"
        assert statusline._running_plugin_version() == "9.9.9"


def test_operator_brief_and_four_skills_are_shipped(plugin_root):
    required = [
        PLUGIN_ROOT / "ANTIGRAVITY.md",
        PLUGIN_ROOT / "skills" / "memory" / "SKILL.md",
        PLUGIN_ROOT / "skills" / "setup" / "SKILL.md",
        PLUGIN_ROOT / "skills" / "codebase" / "SKILL.md",
        PLUGIN_ROOT / "skills" / "local-ui" / "SKILL.md",
    ]

    assert all(path.is_file() for path in required), (
        f"missing Antigravity plugin assets: {required}"
    )


def test_config_and_env_file_keep_antigravity_specific_state_agent_and_backend_contract(
    plugin_root, tmp_path, monkeypatch
):
    config = PLUGIN_ROOT / "scripts" / "config.py"
    env_file = PLUGIN_ROOT / "scripts" / "_env_file.py"

    assert config.is_file(), f"missing Antigravity config module: {config}"
    assert env_file.is_file(), f"missing Antigravity env-file module: {env_file}"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.delenv("COGNEE_ENV_FILE", raising=False)
    saved_modules = {name: sys.modules.pop(name, None) for name in ("_env_file", "_agy_config")}
    sys.path.insert(0, str(config.parent))
    try:
        spec = importlib.util.spec_from_file_location("_agy_config", config)
        assert spec and spec.loader
        config_module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = config_module
        spec.loader.exec_module(config_module)
        env_module = sys.modules["_env_file"]

        assert config_module._STATE_DIR == home / ".cognee-plugin" / "antigravity"
        assert config_module._DEFAULTS["agent_name"] == "antigravity-agent"
        assert config_module._ENV_MAP["COGNEE_ANTIGRAVITY_BACKEND"] == "backend"
        assert env_module._PLUGIN_BACKEND_VAR == "COGNEE_ANTIGRAVITY_BACKEND"
    finally:
        sys.path.remove(str(config.parent))
        for name, module in saved_modules.items():
            sys.modules.pop(name, None)
            if module is not None:
                sys.modules[name] = module
