"""The skills' commands, run the way the agent runs them, on the machines people have.

A skill is instructions for the agent: "run ``cognee-search.sh "<query>" 10
--graph``", "run ``python3 "${…_PLUGIN_ROOT}/scripts/list-datasets.py"
--others``". The agent runs them through its shell tool, after the session's
hooks have connected it, with the plugin-root variables absent from its
environment (Claude Code documents this; Codex hands the agent no plugin root at
all, so it reads the path from the installed SKILL.md).

The Windows Codex report showed both kinds failing: the ``.sh`` scripts because
they call ``python3`` by name everywhere (and swallow the failure with ``||
true``), the direct commands because a python.org install has ``python`` and
``py`` but no ``python3``. Those setups are marked as strict xfails, pinned to
that bug, so the day the skills stop depending on a ``python3`` on PATH they
flip to XPASS and the marks must come off.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest
from utils import host_launch as hl
from utils.fixtures import DEFAULT_TEST_API_KEY
from utils.host_launch import HostSession, make_home, write_env_file

MEMORY = "The project codename is BLUEFIN."

#: How the machine provides Python. "python3" is the machine running the tests
#: as it is; the others hide its python3 behind a shim first on PATH.
_PYTHON_SETUPS = {
    "python3": {},
    # python.org's installer: `python` and `py`, no `python3`.
    "python-only": {"present": ("python",), "missing": ("python3",)},
    # ...plus the Microsoft Store alias that Windows puts on PATH for python3.
    "store-alias-python3": {"present": ("python",), "store_stubs": ("python3",)},
}

_NEEDS_PYTHON3 = (
    "the skills call `python3` by name (in the .sh scripts and in SKILL.md), which a "
    "python.org install on Windows does not provide"
)

#: Direct `python3 …` commands from SKILL.md that only read state, safe to run
#: as-is against the mock. Commands that switch, sync or take a placeholder
#: argument are not run.
_READ_ONLY = {"list-datasets.py", "doctor.py"}
# switch-dataset.py --list is read-only too, but in both plugins it finds the
# session through the launch record SessionStart keyed by the host process it
# ran under (the codex / claude process), and this harness has no host process.
_SKILL_ROOT_VAR = {"codex": "CODEX_PLUGIN_ROOT", "claude-code": "CLAUDE_PLUGIN_ROOT"}


def _bash() -> str | None:
    return hl.git_bash() if os.name == "nt" else shutil.which("bash")


@pytest.fixture
def connected(suite, private_mock_server, tmp_path):
    """A cloud session whose SessionStart has run, as it has before the agent acts."""
    if not hl.launches_through_runner(suite):
        pytest.skip(f"{suite.name}: not covered by the host-launch harness")
    if not _bash():
        pytest.skip("no bash for the agent to run the skill scripts with")
    mock = private_mock_server
    home = make_home(tmp_path, "Test User")
    write_env_file(home, mock.url, DEFAULT_TEST_API_KEY)
    project = home / "project"
    project.mkdir()
    mock.set_recall_results([{"source": "graph", "text": MEMORY}])
    session = HostSession(suite, home, project, mock)
    session.start()
    session.assert_every_hook_succeeded()
    return session


def _agent_env(session: HostSession, tmp_path, setup: str) -> dict[str, str]:
    env = dict(session.env)
    for name in ("PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_DATA", "CLAUDE_PLUGIN_DATA"):
        env.pop(name, None)
    shims = _PYTHON_SETUPS[setup]
    if shims:
        directory = hl.make_shims(tmp_path / "bin", **shims)
        env["PATH"] = os.pathsep.join([str(directory), env.get("PATH", "")])
    return env


def _python_setups():
    return [
        pytest.param(
            name,
            marks=[]
            if name == "python3"
            else [pytest.mark.xfail(strict=True, raises=AssertionError, reason=_NEEDS_PYTHON3)],
        )
        for name in _PYTHON_SETUPS
    ]


def _run(session: HostSession, env: dict, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        env=env,
        cwd=str(session.project),
        capture_output=True,
        timeout=120,
    )


@pytest.mark.parametrize("setup", _python_setups())
def test_search_skill_finds_the_memory(connected, tmp_path, setup):
    env = _agent_env(connected, tmp_path, setup)
    script = connected.root / "scripts" / "cognee-search.sh"
    result = _run(
        connected, env, [_bash(), str(script), "what is the project codename?", "10", "--graph"]
    )
    out = result.stdout.decode("utf-8", "replace")
    err = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, err
    assert MEMORY in out, f"stdout={out!r} stderr={err!r}"


@pytest.mark.parametrize("setup", _python_setups())
def test_remember_skill_stores_the_text(connected, tmp_path, setup):
    env = _agent_env(connected, tmp_path, setup)
    script = connected.root / "scripts" / "cognee-remember.sh"
    text = "I prefer tabs over spaces"
    result = _run(connected, env, [_bash(), str(script), text, "--node-set", "user_context"])
    err = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 0, err
    stored = [
        c for c in connected.mock.calls if c["method"] == "POST" and c["path"] == "/api/v1/remember"
    ]
    assert stored, f"nothing reached /api/v1/remember; stderr={err!r}"


def _read_only_skill_commands(session: HostSession) -> list[str]:
    var = _SKILL_ROOT_VAR[session.suite.name]
    pattern = re.compile(
        r'^\s*(python3 "\$\{' + var + r'\}/scripts/([\w.-]+)"((?: --[\w-]+)*))\s*$',
        re.MULTILINE,
    )
    commands = set()
    for skill in sorted((session.root / "skills").glob("*/SKILL.md")):
        for command, script, _ in pattern.findall(skill.read_text(encoding="utf-8")):
            if script in _READ_ONLY:
                commands.add(command.replace("${" + var + "}", session.root.as_posix()))
    return sorted(commands)


@pytest.mark.parametrize("setup", _python_setups())
def test_direct_python_commands_from_the_skills_run(connected, tmp_path, setup):
    commands = _read_only_skill_commands(connected)
    if not commands:
        pytest.skip(f"{connected.suite.name}: no read-only python3 commands in its skills")
    env = _agent_env(connected, tmp_path, setup)
    for command in commands:
        result = _run(connected, env, [_bash(), "-c", command])
        err = result.stderr.decode("utf-8", "replace")
        assert result.returncode == 0, f"{command}: {err}"
        assert result.stdout.strip(), f"{command}: printed nothing; stderr={err!r}"
