"""Local mode, with every hook launched the way the host launches it.

Local mode is what a user gets with no Cloud URL configured: the plugin targets
``http://localhost:8011`` and logs in as the default user for its own API key.
When nothing answers there, Codex installs cognee into ``~/.cognee-plugin/venv``
and boots a server; the Claude Code plugin installs nothing (SDK-1411) and
boots only from a venv the user built, printing the install command otherwise.
Once that venv exists, every hook re-execs into it on import
(``_plugin_common._reexec_into_venv``).

Registration differs by host: with a plugin identity (Codex) the session is
registered with ``dataset_ids``; without one (Claude Code, where every request
runs as the user's own key) it goes by dataset name.

The cheap tier stands in for the expensive parts:

* a mock server on the real local port, so SessionStart finds a local server
  already serving and neither installs nor boots (a busy port skips the test);
* a real, empty venv (``python -m venv --without-pip``) at the plugin's venv
  path, whose ``sitecustomize`` records each interpreter start, so the re-exec
  path runs for real without cognee in it.

Everything else is e2e/test_host_session.py's harness: the plugin installed
under the test HOME, hooks run from hooks.json through the host's shell. The
real install, boot and LLM calls are the live tier's job.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_httpserver import HTTPServer
from utils import host_launch as hl
from utils.host_launch import HostSession, make_home
from utils.mock_cognee import MockCogneeServer

MEMORY = "The project codename is BLUEFIN."
_LOCAL_PORT = 8011

# Local mode needs only an LLM key. Codex reads it from ~/.cognee/.env; the
# Claude Code plugin takes it from its plugin option and ignores the file.
_LLM_KEY = "sk-test-not-a-real-key"
_LOCAL_ENV_FILE = f'LLM_API_KEY="{_LLM_KEY}"\n'


@pytest.fixture
def host(suite):
    if not hl.launches_through_runner(suite):
        pytest.skip(f"{suite.name}: hooks are not launched through hooks.json command strings here")
    if not hl.host_shell_available(suite):
        pytest.skip(f"{suite.name}: the host's shell is not installed")
    return suite


@pytest.fixture
def local_server():
    """A mock Cognee server on the plugin's local port, standing in for a booted one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if probe.connect_ex(("127.0.0.1", _LOCAL_PORT)) == 0:
            pytest.skip(f"port {_LOCAL_PORT} is in use (a real local Cognee server?)")
    server = HTTPServer(host="127.0.0.1", port=_LOCAL_PORT)
    server.start()
    mock = MockCogneeServer(server)
    try:
        yield mock
    finally:
        server.stop()


def _local_home(suite, tmp_path: Path, profile: str) -> tuple[Path, Path]:
    home = make_home(tmp_path, profile)
    if not suite.keys_from_plugin_options:
        (home / ".cognee" / ".env").write_text(_LOCAL_ENV_FILE, encoding="utf-8")
    project = home / "project"
    project.mkdir()
    return home, project


def _local_options(suite) -> dict[str, str]:
    """The LLM key as Claude Code hands it to the plugin's hooks, for that host."""
    if not suite.keys_from_plugin_options:
        return {}
    return {"CLAUDE_PLUGIN_OPTION_LLM_API_KEY": _LLM_KEY}


def _context(output: dict) -> str:
    return output["hookSpecificOutput"]["additionalContext"]


def _assert_session_worked(host, session: HostSession) -> None:
    session.assert_every_hook_succeeded()
    registered = session.calls("/api/v1/agents/register")
    assert registered, "SessionStart never registered the session"
    if host.has_plugin_identity:
        assert registered[-1].get("dataset_ids"), registered[-1]
    else:
        # As the user's own key, the session goes by dataset name.
        assert registered[-1].get("dataset_names") == [host.default_dataset], registered[-1]
    lookups = session.outputs("UserPromptSubmit", "session-context-lookup.py")
    assert lookups and all(MEMORY in _context(o) for o in lookups), lookups
    entries = [c["entry"] for c in session.calls("/api/v1/remember/entry")]
    assert any(e.get("type") == "qa" for e in entries), entries


@pytest.mark.parametrize("profile", ["cognee", "Test User"], ids=["plain", "space"])
def test_a_local_session_uses_the_server_already_running(host, local_server, tmp_path, profile):
    home, project = _local_home(host, tmp_path, profile)
    local_server.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, local_server, env_extra=_local_options(host))
    session.full("what is the project codename?", "and what did you just tell me?")

    _assert_session_worked(host, session)
    # No key configured: local mode logs in as the default user and mints one.
    local_server.assert_called("POST", "/api/v1/auth/login")
    # A running server is used, never installed over or booted.
    assert not (home / ".cognee-plugin" / "uv").exists()


# One file per interpreter start, written aside and renamed into place. A shared
# append-mode log tore lines on Windows, where append is not atomic across the
# hooks and detached children a host starts at the same moment.
_REEXEC_RECORDER = """\
import json
import os
import sys
import uuid

if os.environ.get("COGNEE_PLUGIN_IN_VENV") == "1":
    _dir = os.path.join(sys.prefix, "reexec")
    os.makedirs(_dir, exist_ok=True)
    _name = uuid.uuid4().hex
    _tmp = os.path.join(_dir, _name + ".tmp")
    with open(_tmp, "w", encoding="utf-8") as _fh:
        json.dump(sys.argv, _fh)
    os.replace(_tmp, os.path.join(_dir, _name + ".json"))
"""


def _make_plugin_venv(home: Path) -> Path:
    """A real venv at the plugin's venv path, logging every interpreter start."""
    venv = home / ".cognee-plugin" / "venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, timeout=300
    )
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    purelib = subprocess.run(
        [str(python), "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout.strip()
    # An import line in a .pth runs at every start of the venv interpreter (a
    # sitecustomize.py would not: some builds, Homebrew's among them, ship one
    # in the stdlib that shadows it). The plugin's re-exec sets
    # COGNEE_PLUGIN_IN_VENV=1 before it execs, so only re-execs are recorded.
    Path(purelib, "cognee_test_reexec.pth").write_text(
        "import cognee_test_reexec_record\n", encoding="utf-8"
    )
    Path(purelib, "cognee_test_reexec_record.py").write_text(_REEXEC_RECORDER, encoding="utf-8")
    return venv


def _reexec_xfail(profile: str):
    if os.name == "nt" and " " in profile:
        # os.execv on Windows does not quote its arguments, so the re-exec'd
        # interpreter gets the plugin path split at the space.
        return [
            pytest.mark.xfail(
                strict=True,
                raises=AssertionError,
                reason="os.execv on Windows splits a profile path containing a space",
            )
        ]
    return []


@pytest.mark.parametrize(
    "profile",
    [
        pytest.param("cognee", id="plain"),
        pytest.param("Test User", id="space", marks=_reexec_xfail("Test User")),
    ],
)
def test_hooks_re_exec_into_the_plugin_venv(host, local_server, tmp_path, profile):
    home, project = _local_home(host, tmp_path, profile)
    venv = _make_plugin_venv(home)
    local_server.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, local_server, env_extra=_local_options(host))
    session.full("what is the project codename?", "and what did you just tell me?")

    _assert_session_worked(host, session)
    records = sorted((venv / "reexec").glob("*.json"))
    assert records, "no hook re-execed into the plugin venv"
    starts = [json.loads(record.read_text(encoding="utf-8")) for record in records]
    # Every hook imports _plugin_common, so every hook the host launched
    # re-execs, and through the runner. Detached children the hooks start
    # themselves (the exit watcher, Codex's deferred SessionEnd sync) run
    # under the venv interpreter too, without it, and are not counted.
    through_runner = [argv[1:] for argv in starts if Path(argv[0]).name == hl.RUNNER]
    assert len(through_runner) >= len(session.runs), (len(session.runs), starts)
    reexeced = {Path(argv[0]).name for argv in through_runner if argv}
    assert {r.script for r in session.runs} <= reexeced, (reexeced, starts)
