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

#: A plugin-root placeholder on its own: ${CLAUDE_PLUGIN_ROOT} or ${PLUGIN_ROOT}.
_PLUGIN_ROOT = re.compile(r"\$\{(?:CLAUDE_)?PLUGIN_ROOT\}")

#: A *quoted* plugin-root path — "${CLAUDE_PLUGIN_ROOT}/scripts/foo.py" — capturing
#: the path that follows the placeholder. The surrounding quotes are mandatory; see
#: test_every_plugin_root_reference_is_quoted.
_QUOTED_PLUGIN_ROOT_PATH = re.compile(r'"\$\{(?:CLAUDE_)?PLUGIN_ROOT\}(/[^"]*)"')


@pytest.fixture
def manifest(suite) -> dict:
    if suite.hook_manifest_style == "named":
        pytest.skip(
            f"{suite.name}: named hook manifests are covered by the dedicated contract test"
        )
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


def test_pretooluse_read_is_wired_to_file_context(suite, manifest):
    """Claude Code only: file-scoped memory rides on PreToolUse with a ``Read`` matcher.

    The matcher matters as much as the script: without it the hook would run on
    every tool call (Bash, Edit, ...) and pay a server round trip for nothing.
    """
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: PreToolUse file context is a Claude Code feature")
    groups = manifest.get("PreToolUse", [])
    wired = [
        g
        for g in groups
        if any("file-context.py" in str(h.get("command", "")) for h in g.get("hooks", []))
    ]
    assert wired, f"PreToolUse does not invoke file-context.py: {groups}"
    assert all(g.get("matcher") == "Read" for g in wired), (
        f"file-context.py must be scoped to the Read tool: {wired}"
    )
    for g in wired:
        for hook in g.get("hooks", []):
            if "file-context.py" in str(hook.get("command", "")):
                assert not hook.get("async"), "file context must be synchronous to inject context"
                assert 0 < int(hook.get("timeout", 0)) <= 15, "file context must stay cheap"


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


def test_every_plugin_root_reference_is_quoted(suite, manifest):
    """Every ${CLAUDE_PLUGIN_ROOT} path must be wrapped in double quotes.

    Hook commands run through a shell, and the plugin root is not ours to pick —
    it follows the host's config home. Anthropic's own managed defaults contain a
    space on both platforms (`/Library/Application Support/Claude/org-plugins`,
    `%ProgramFiles%\\Claude\\org-plugins`), so a bare path word-splits and the
    interpreter dies on a truncated filename.

    PreCompact is the only event loud enough to notice: it exits non-zero, which
    Claude Code treats as a blocking error, so /compact is refused. Every other
    hook here is async or non-blocking, so it fails *silently* — memory capture
    simply stops with nothing to indicate why. Asserted across the whole manifest
    rather than the events in EXPECTED, for the same reason the python fallback is.

    Reported as topoteretes/cognee#5154.
    """
    unquoted = []
    for event, groups in manifest.items():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                command = str(hook.get("command", "")) if isinstance(hook, dict) else ""
                # Drop the references that are correctly quoted; a placeholder
                # surviving that means it was bare.
                if _PLUGIN_ROOT.search(_QUOTED_PLUGIN_ROOT_PATH.sub("", command)):
                    unquoted.append((event, command[:90]))
    assert not unquoted, (
        f"{suite.name}: unquoted plugin-root paths break on any install whose root "
        f"contains a space (e.g. a managed install under "
        f"'/Library/Application Support/Claude/org-plugins'): {unquoted}"
    )


def test_every_registered_script_exists(suite, manifest):
    """A renamed or deleted script must fail here, not silently at runtime."""
    missing = []
    resolved = 0
    for event, groups in manifest.items():
        for group in groups:
            for hook in group.get("hooks", []):
                command = str(hook.get("command", ""))
                for path in _QUOTED_PLUGIN_ROOT_PATH.findall(command):
                    if not path.endswith((".py", ".sh")):
                        continue
                    resolved += 1
                    name = path.lstrip("/").removeprefix("scripts/")
                    if not (suite.scripts_dir / name).exists():
                        missing.append((event, name))
    assert not missing, f"{suite.name}: hooks.json points at missing scripts: {missing}"
    # Guard the guard: the matcher above only sees *quoted* paths, so if quoting
    # ever regressed it would find nothing and this test would pass having checked
    # no scripts at all.
    assert resolved, f"{suite.name}: no quoted plugin-root script paths found in hooks.json"
