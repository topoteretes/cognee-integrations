"""Manifest and plugin-surface contract for the Antigravity Cognee plugin."""

from __future__ import annotations

import importlib.util
import json
import shlex
import stat
import sys
from pathlib import Path

import pytest

INTEGRATIONS_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = INTEGRATIONS_ROOT / "antigravity"
PLUGIN_JSON = PLUGIN_ROOT / "plugin.json"
HOOKS_JSON = PLUGIN_ROOT / "hooks.json"
POSIX_HOOK_RUNNER = PLUGIN_ROOT / "scripts" / "run-agy-hook"
WINDOWS_HOOK_RUNNER = PLUGIN_ROOT / "scripts" / "run-agy-hook.cmd"
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


EXPECTED_HANDLER_ARGS = {
    ("cognee-bootstrap", "PreInvocation"): [("session-start.py",)],
    ("cognee-recall", "PreInvocation"): [("session-context-lookup.py",)],
    ("cognee-capture", "PreInvocation"): [("store-user-prompt.py",)],
    ("cognee-capture", "PostToolUse"): [("store-to-session.py",)],
    ("cognee-stop", "Stop"): [
        ("store-to-session.py", "--stop"),
        ("sync-session-to-graph.py", "--session-end"),
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


def test_stop_stores_assistant_message_then_syncs_session_end(manifest):
    commands = _commands(manifest, "cognee-stop", "Stop")
    assert [_command_tokens(command)[1] for command in commands] == EXPECTED_HANDLER_ARGS[
        ("cognee-stop", "Stop")
    ]


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


def test_high_risk_host_adaptations_keep_private_and_shared_roots_separate(plugin_root):
    scripts = plugin_root / "scripts"
    common = (scripts / "_plugin_common.py").read_text(encoding="utf-8")
    config = (scripts / "config.py").read_text(encoding="utf-8")
    session_start = (scripts / "session-start.py").read_text(encoding="utf-8")
    proc = (scripts / "_proc.py").read_text(encoding="utf-8")
    statusline = (scripts / "cognee_statusline_render.py").read_text(encoding="utf-8")
    remember = (scripts / "cognee-remember.sh").read_text(encoding="utf-8")
    search = (scripts / "cognee-search.sh").read_text(encoding="utf-8")

    assert '_PLUGIN_DIR = Path.home() / ".cognee-plugin" / "antigravity"' in common
    assert '_SHARED_PLUGIN_ROOT = Path.home() / ".cognee-plugin"' in common
    assert '_VENV_DIR = _SHARED_PLUGIN_ROOT / "venv"' in common
    assert '_API_KEY_CACHE = _SHARED_PLUGIN_ROOT / "api_key.json"' in common
    assert '_SERVER_READY_MARKER = _SHARED_PLUGIN_ROOT / "server-ready.json"' in common
    assert 'os.environ.get("AGY_CWD")' in common
    assert 'or "antigravity"' in common
    assert 'suffix = "_agy"' in common
    assert 'return _normalize("antigravity-agent")' in common

    assert '"session_prefix": "antigravity"' in config
    assert '"agent_name": "antigravity-agent"' in config
    assert 'os.environ.get("AGY_CWD", os.getcwd())' in config
    assert "def _find_agy_parent_pid()" in session_start
    assert 'find_host_ancestor_windows(fallback, "agy")' in session_start
    assert 're.compile(r"(?:^|/)agy(?:-[\\w.]+)?(?:\\s|$)")' in session_start
    assert "base}/api/v1/auth/api-keys" in session_start
    assert '"name": "antigravity-owner-bootstrap"' in session_start
    assert '(e.g. "claude" / "agy")' in proc

    assert "COGNEE_ANTIGRAVITY_PLUGIN_ROOT" in common
    assert 'Path(__file__).resolve().parent.parent / "plugin.json"' in common
    assert "integrations/antigravity/plugin.json" in common
    assert 'Path(__file__).resolve().parent.parent / "plugin.json"' in statusline
    assert 'PLUGIN_DIR="${HOME}/.cognee-plugin/antigravity"' in remember
    assert 'PLUGIN_DIR="${HOME}/.cognee-plugin/antigravity"' in search


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
