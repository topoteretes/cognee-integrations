"""Qwen packing: Gemini-style extension manifests and Claude-style hooks."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

_INTEGRATIONS = Path(__file__).resolve().parents[3]
_REPO = _INTEGRATIONS.parent
QWEN = _INTEGRATIONS / "qwen"

_CLAUDE_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "PostToolUse",
    "Stop",
    "PreCompact",
    "SessionEnd",
)
_GEMINI_ONLY_EVENTS = ("BeforeAgent", "AfterAgent", "BeforeTool", "AfterTool", "PreCompress")
_PLUGIN_ROOT = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}")


def _manifest(name: str) -> dict:
    return json.loads((QWEN / name).read_text(encoding="utf-8"))


def _hooks() -> dict:
    spec = json.loads((QWEN / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    return spec.get("hooks", spec)


def _command_hooks():
    for event, groups in _hooks().items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if isinstance(hook, dict) and hook.get("type") == "command":
                    yield event, hook


def test_gemini_extension_manifest_has_required_keys():
    manifest = _manifest("gemini-extension.json")
    assert manifest["name"] == "cognee"
    assert manifest["version"]
    assert manifest["contextFileName"] in ("GEMINI.md", "QWEN.md", ["GEMINI.md", "QWEN.md"])


def test_qwen_extension_manifest_mirrors_name_and_version():
    gemini = _manifest("gemini-extension.json")
    qwen = _manifest("qwen-extension.json")
    assert qwen["name"] == gemini["name"] == "cognee"
    assert qwen["version"] == gemini["version"]


def test_manifests_reference_existing_context_and_hooks_files():
    for name in ("gemini-extension.json", "qwen-extension.json"):
        manifest = _manifest(name)
        context_files = manifest["contextFileName"]
        if isinstance(context_files, str):
            context_files = [context_files]
        assert context_files
        assert all((QWEN / context_file).is_file() for context_file in context_files)
        assert manifest["hooks"] == "hooks/hooks.json"


def test_qwen_ships_memory_setup_and_codebase_skills():
    for skill in ("memory", "setup", "codebase"):
        assert (QWEN / "skills" / skill / "SKILL.md").is_file()


def test_installed_memory_skill_resolves_extension_scripts_from_injected_base(tmp_path):
    """Qwen injects the installed skill base, not a plugin-root environment variable."""
    installed_extension = tmp_path / "installed-extensions" / "cognee"
    shutil.copytree(QWEN, installed_extension)
    installed_skill = installed_extension / "skills" / "memory" / "SKILL.md"
    skill = installed_skill.read_text(encoding="utf-8")

    assert "${CLAUDE_PLUGIN_ROOT}" not in skill
    assert "two levels above" in skill
    assert 'EXTENSION_ROOT="$(cd "<qwen-injected-skill-base>/../.." && pwd)"' in skill

    extension_root = installed_skill.parent.parent.parent
    scripts = re.findall(r"\$EXTENSION_ROOT/scripts/([\w.-]+)", skill)
    assert scripts, "memory skill does not name any extension-root script commands"
    assert not [script for script in scripts if not (extension_root / "scripts" / script).is_file()]


def test_hooks_use_qwen_extension_layout():
    assert (QWEN / "hooks" / "hooks.json").is_file()
    assert not (QWEN / "hooks.json").exists()


def test_claude_event_names_are_wired():
    hooks = _hooks()
    for event in _CLAUDE_EVENTS:
        assert event in hooks, f"Qwen missing event {event}"


def test_gemini_only_event_names_are_not_used():
    hooks = _hooks()
    for event in _GEMINI_ONLY_EVENTS:
        assert event not in hooks, f"Qwen must not use Gemini CLI event {event}"


def test_command_timeouts_are_milliseconds():
    too_small = []
    missing = []
    for event, hook in _command_hooks():
        timeout = hook.get("timeout")
        if timeout is None:
            missing.append(event)
        elif int(timeout) < 1000:
            too_small.append((event, timeout))
    assert not missing, f"Qwen command hooks need explicit millisecond timeouts: {missing}"
    assert not too_small, f"Qwen command timeouts must be milliseconds (>=1000): {too_small}"


def test_commands_expand_qwen_claude_plugin_root():
    missing = []
    for event, hook in _command_hooks():
        command = str(hook.get("command", ""))
        if "python" in command and not _PLUGIN_ROOT.search(command):
            missing.append((event, command[:80]))
    assert not missing, f"Qwen hooks must use ${{CLAUDE_PLUGIN_ROOT}}: {missing}"


def test_qwen_hook_root_contract_rejects_extension_path():
    assert _PLUGIN_ROOT.search("${extensionPath}/scripts/session-start.py") is None


def test_python3_commands_fall_back_to_python():
    missing = []
    for event, hook in _command_hooks():
        command = str(hook.get("command", ""))
        if "python3 " in command and "|| python " not in command:
            missing.append(event)
    assert not missing, f"python3-only Qwen hooks: {missing}"


def test_state_backend_and_cwd_are_qwen_namespaced():
    config = (QWEN / "scripts" / "config.py").read_text(encoding="utf-8")
    env_file = (QWEN / "scripts" / "_env_file.py").read_text(encoding="utf-8")
    common = (QWEN / "scripts" / "_plugin_common.py").read_text(encoding="utf-8")
    session_start = (QWEN / "scripts" / "session-start.py").read_text(encoding="utf-8")

    assert "COGNEE_QWEN_BACKEND" in env_file
    assert "COGNEE_QWEN_BACKEND" in config
    assert "COGNEE_CODEX_BACKEND" not in env_file
    assert '.cognee-plugin" / "qwen"' in common
    assert "qwen-agent" in config
    assert "QWEN_CWD" in common
    assert "QWEN_CWD" in session_start


def test_linux_ci_routes_qwen_changes_to_the_shared_suite():
    workflow = (_REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "claude-code|codex|qwen|tests" in workflow


def test_windows_ci_checks_out_and_triggers_on_qwen():
    workflow = (_REPO / ".github" / "workflows" / "plugin-windows-tests.yml").read_text(
        encoding="utf-8"
    )
    assert workflow.count("integrations/qwen/**") == 2
    assert "            integrations/qwen" in workflow
