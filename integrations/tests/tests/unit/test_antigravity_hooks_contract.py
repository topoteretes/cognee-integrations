"""Manifest and plugin-surface contract for the Antigravity Cognee plugin."""

from __future__ import annotations

import importlib.util
import json
import shlex
import sys
from pathlib import Path

import pytest

INTEGRATIONS_ROOT = Path(__file__).resolve().parents[3]
PLUGIN_ROOT = INTEGRATIONS_ROOT / "antigravity"
PLUGIN_JSON = PLUGIN_ROOT / "plugin.json"
HOOKS_JSON = PLUGIN_ROOT / "hooks.json"
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
    halves = command.split("||")
    assert len(halves) == 2, f"command must have python3/python fallback: {command!r}"
    first, second = (shlex.split(half.strip()) for half in halves)
    assert first[0] == "python3", first
    assert second[0] == "python", second
    assert first[1:] == second[1:], command
    assert first[1] == "scripts/agy_hook.py", first
    return first[0], tuple(first[2:])


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


def test_all_hook_commands_are_relative_agy_wrapper_commands_with_safe_second_timeouts(manifest):
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
