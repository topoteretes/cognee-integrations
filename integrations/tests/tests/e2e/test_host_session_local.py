"""Local mode, with every hook launched the way the host launches it.

Local mode is what a user gets with no ``COGNEE_BASE_URL``: the plugin targets
``http://localhost:8011``, logs in as the default user to mint its own API key,
and installs cognee into ``~/.cognee-plugin/venv`` and boots a server there only
when nothing answers. Once that venv exists, every hook re-execs into it on
import (``_plugin_common._reexec_into_venv``).

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

# The README's local-mode snippet puts only this in ~/.cognee/.env.
_LOCAL_ENV_FILE = 'LLM_API_KEY="sk-test-not-a-real-key"\n'


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


def _local_home(tmp_path: Path, profile: str) -> tuple[Path, Path]:
    home = make_home(tmp_path, profile)
    (home / ".cognee" / ".env").write_text(_LOCAL_ENV_FILE, encoding="utf-8")
    project = home / "project"
    project.mkdir()
    return home, project


def _context(output: dict) -> str:
    return output["hookSpecificOutput"]["additionalContext"]


def _assert_session_worked(session: HostSession) -> None:
    session.assert_every_hook_succeeded()
    registered = session.calls("/api/v1/agents/register")
    assert registered and registered[-1].get("dataset_ids"), registered
    lookups = session.outputs("UserPromptSubmit", "session-context-lookup.py")
    assert lookups and all(MEMORY in _context(o) for o in lookups), lookups
    entries = [c["entry"] for c in session.calls("/api/v1/remember/entry")]
    assert any(e.get("type") == "qa" for e in entries), entries


@pytest.mark.parametrize("profile", ["cognee", "Test User"], ids=["plain", "space"])
def test_a_local_session_uses_the_server_already_running(host, local_server, tmp_path, profile):
    home, project = _local_home(tmp_path, profile)
    local_server.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, local_server)
    session.full("what is the project codename?", "and what did you just tell me?")

    _assert_session_worked(session)
    # No key configured: local mode logs in as the default user and mints one.
    local_server.assert_called("POST", "/api/v1/auth/login")
    # A running server is used, never installed over or booted.
    assert not (home / ".cognee-plugin" / "uv").exists()


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
        "import os, sys, json; os.environ.get('COGNEE_PLUGIN_IN_VENV') == '1' and "
        "open(os.path.join(sys.prefix, 'reexec.jsonl'), 'a', encoding='utf-8')"
        ".write(json.dumps(sys.argv) + chr(10))\n",
        encoding="utf-8",
    )
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
    home, project = _local_home(tmp_path, profile)
    venv = _make_plugin_venv(home)
    local_server.set_recall_results([{"source": "graph", "text": MEMORY}])

    session = HostSession(host, home, project, local_server)
    session.full("what is the project codename?", "and what did you just tell me?")

    _assert_session_worked(session)
    log = venv / "reexec.jsonl"
    assert log.is_file(), "no hook re-execed into the plugin venv"
    starts = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines() if line]
    # Every hook imports _plugin_common, so every hook the host launched
    # re-execs, and through the runner. Detached children the hooks start
    # themselves (the exit watcher, Codex's deferred SessionEnd sync) run
    # under the venv interpreter too, without it, and are not counted.
    through_runner = [argv[1:] for argv in starts if Path(argv[0]).name == hl.RUNNER]
    assert len(through_runner) >= len(session.runs), (len(session.runs), starts)
    reexeced = {Path(argv[0]).name for argv in through_runner if argv}
    assert {r.script for r in session.runs} <= reexeced, (reexeced, starts)
