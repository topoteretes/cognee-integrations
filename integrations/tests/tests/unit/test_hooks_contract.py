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
import re

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


def test_every_python_hook_falls_back_to_python(suite, manifest):
    """Every `python3 x.py` must carry a `|| python x.py` fallback.

    Windows installs routinely provide `python` but not `python3`, so a bare
    `python3` invocation fails at the shell before the hook is ever reached — and
    a hook that never runs is silent by design: memory simply stops being captured
    with nothing to indicate why.

    Asserted for the *whole* manifest rather than the events in EXPECTED, because a
    hook added later without the fallback would be just as broken and there is
    nothing to remind whoever adds it.
    """
    missing = []
    for event, groups in manifest.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                if "python3 " not in command:
                    continue
                if "|| python " not in command:
                    missing.append((event, command[:90]))
    assert not missing, (
        f"{suite.name}: python3-only hook commands would not start on a Windows box "
        f"that ships only `python`: {missing}"
    )


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
                    name = token.lstrip("/").removeprefix("scripts/")
                    if not (suite.scripts_dir / name).exists():
                        missing.append((event, name))
    assert not missing, f"{suite.name}: hooks.json points at missing scripts: {missing}"
