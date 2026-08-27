"""hooks.json really registers the hooks the rest of the suite simulates.

The e2e and live tiers invoke hook scripts directly, which proves the scripts
behave — but not that the host would ever call them. That gap is real and cheap
to close: parse the manifest and assert every event the tests drive is wired to a
script that exists, with the argument the tests pass.

The regression this catches is mundane and likely: a script gets renamed, an
event is dropped during a refactor, or `--stop` / `--session-end` is lost. Every
other test would keep passing while the plugin silently stopped capturing.

Hermetic — reads the manifest off disk, no server, no subprocess.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

#: (event, script, required arg) — exactly what the e2e/live drivers invoke.
EXPECTED = [
    ("SessionStart", "session-start.py", None),
    ("UserPromptSubmit", "session-context-lookup.py", None),
    ("UserPromptSubmit", "store-user-prompt.py", None),
    ("PostToolUse", "store-to-session.py", None),
    ("Stop", "store-to-session.py", "--stop"),
    ("PreCompact", "pre-compact.py", None),
    ("SessionEnd", "sync-session-to-graph.py", "--session-end"),
]

#: ${CLAUDE_PLUGIN_ROOT} / ${PLUGIN_ROOT}, quoted or bare.
_PLUGIN_ROOT = re.compile(r'"?\$\{(?:CLAUDE_)?PLUGIN_ROOT\}"?')


@pytest.fixture
def manifest(suite) -> dict:
    spec = json.loads(suite.hooks_json.read_text(encoding="utf-8"))
    # Claude nests everything under "hooks"; keep both shapes working.
    return spec.get("hooks", spec)


def _commands(manifest: dict, event: str) -> list[str]:
    return [
        str(hook.get("command", ""))
        for group in manifest.get(event, [])
        for hook in group.get("hooks", [])
    ]


@pytest.mark.parametrize(("event", "script", "arg"), EXPECTED, ids=lambda v: str(v))
def test_event_is_wired_to_its_script(suite, manifest, event, script, arg):
    commands = _commands(manifest, event)
    assert commands, f"{suite.name}: no hooks registered for {event}"

    matching = [cmd for cmd in commands if script in cmd]
    assert matching, f"{suite.name}: {event} does not invoke {script}. Registered: {commands}"
    if arg:
        assert any(arg in cmd for cmd in matching), (
            f"{suite.name}: {event} invokes {script} without {arg}: {matching}"
        )


def test_every_python_hook_uses_version_checked_launcher(suite, manifest):
    """Claude hooks must not trust whichever old ``python3`` is first on PATH."""
    if suite.name != "claude-code":
        pytest.skip("the Codex plugin owns a separate launcher")

    missing = []
    for event, groups in manifest.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if ".py" not in command:
                    continue
                if "scripts/run-python.sh" not in command:
                    missing.append((event, command[:90]))
    assert not missing, (
        f"{suite.name}: Python hooks bypass the version-checked launcher: {missing}"
    )


def test_python_launcher_skips_incompatible_python3(suite, tmp_path: Path):
    """A Python 3.9-shaped ``python3`` must not win over a compatible runtime."""
    if suite.name != "claude-code":
        pytest.skip("the Codex plugin owns a separate launcher")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    incompatible = bin_dir / "python3"
    incompatible.write_text("#!/bin/sh\nexit 42\n", encoding="utf-8")
    incompatible.chmod(0o755)
    (bin_dir / "python3.12").symlink_to(sys.executable)
    probe = tmp_path / "probe.py"
    probe.write_text("print('compatible-python')\n", encoding="utf-8")

    env = os.environ.copy()
    env.pop("COGNEE_PYTHON", None)
    env["PATH"] = f"{bin_dir}:/usr/bin:/bin"
    result = subprocess.run(
        ["bash", str(suite.scripts_dir / "run-python.sh"), str(probe)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "compatible-python"


def test_doctor_wrapper_uses_version_checked_launcher(suite):
    if suite.name != "claude-code":
        pytest.skip("the Codex plugin owns a separate launcher")

    wrapper = (suite.scripts_dir / "cognee-doctor.sh").read_text(encoding="utf-8")
    assert "run-python.sh" in wrapper
    assert "exec python3 " not in wrapper


def test_every_registered_script_exists(suite, manifest):
    """A renamed or deleted script must fail here, not silently at runtime."""
    missing = []
    for event, groups in manifest.items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = str(hook.get("command", ""))
                for token in _PLUGIN_ROOT.sub("", command).split():
                    if not token.endswith((".py", ".sh")):
                        continue
                    name = token.strip("\"'").lstrip("/").removeprefix("scripts/")
                    if not (suite.scripts_dir / name).exists():
                        missing.append((event, name))
    assert not missing, f"{suite.name}: hooks.json points at missing scripts: {missing}"
