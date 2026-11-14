"""E2e: the Bash-run skills stop before any request when they cannot authenticate.

Claude Code passes the plugin's Cognee Cloud key option to the plugin's hooks
only. A skill run from the Bash tool reads the URL option from the user
settings (``pluginConfigs``) but gets no key, so against a remote server it
could only send an unauthenticated request and come back with a 401 that sends
the user to an option they already set. Every shell entry point therefore exits
2 first, with the reason, and nothing reaches the server (SDK-1411).

The remote URL is ``http://127.1:<port>``: the plugin reads it as remote (only
``localhost``/``127.0.0.1``/``::1``/``0.0.0.0`` are local), yet it still
resolves to the mock server, so "no request arrived" means the script stopped
itself rather than failing to connect. A control run with the key option set
proves the URL reaches the server.

Also covered: with no CLI fallback any more, an unreachable server makes search
and remember exit 1 with a message and never start ``cognee-cli``.

The guard itself is ``require_shell_auth`` in ``_plugin_common``; these tests
drive it through the scripts end to end.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import urlparse

import pytest
from utils.isolation import build_env, usable_bash

BASH = usable_bash()

GUARD_TEXT = "passes to the plugin's hooks only"

#: The host session id of the launch record the switch commands act on.
LAUNCH_KEY = "host-shell-guard-1"


@pytest.fixture
def claude_only(suite):
    if not suite.keys_from_plugin_options:
        pytest.skip(f"{suite.name}: keys do not come from host plugin options")
    return suite


def _remote_alias(url: str) -> str:
    """The mock server's URL under a host name the plugin reads as remote."""
    return f"http://127.1:{urlparse(url).port}"


def _with_url_option(home, url: str) -> None:
    """Save the URL option where Claude Code keeps non-secret option values."""
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(
        json.dumps(
            {"pluginConfigs": {"cognee-memory@cognee": {"options": {"cognee_base_url": url}}}}
        ),
        encoding="utf-8",
    )


def _shell_env(suite, home, *, api_key=None, extra=None):
    """A Bash-tool environment: no COGNEE_BASE_URL, no CLAUDE_PLUGIN_ROOT."""
    env = build_env(suite, home, api_key=api_key, extra=extra)
    env.pop("COGNEE_BASE_URL", None)
    env.pop("CLAUDE_PLUGIN_ROOT", None)
    return env


def _command(suite, name: str, home) -> list[str]:
    scripts = suite.scripts_dir
    if name == "search":
        return [BASH, str(scripts / "cognee-search.sh"), "what did we decide", "5"]
    if name == "code-search":
        return [BASH, str(scripts / "cognee-search.sh"), "Server", "5", "--code"]
    if name == "remember":
        return [
            BASH,
            str(scripts / "cognee-remember.sh"),
            "we use tabs",
            "--node-set",
            "user_context",
        ]
    if name == "recap":
        return [sys.executable, str(scripts / "cognee-recap.py"), "standup", "--since", "24h"]
    if name == "index-repo":
        return [BASH, str(scripts / "cognee-index-repo.sh"), str(home)]
    if name == "sync":
        return [sys.executable, str(scripts / "sync-session-to-graph.py")]
    if name == "list-datasets":
        return [sys.executable, str(scripts / "list-datasets.py"), "--others"]
    if name == "switch-dataset":
        return [
            sys.executable,
            str(scripts / "switch-dataset.py"),
            "other-dataset",
            "--json",
            "--session-key",
            LAUNCH_KEY,
        ]
    if name == "switch-dataset-list":
        return [
            sys.executable,
            str(scripts / "switch-dataset.py"),
            "--list",
            "--json",
            "--session-key",
            LAUNCH_KEY,
        ]
    raise AssertionError(name)


def _run(cmd, env, home):
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(home),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )


@pytest.mark.parametrize(
    "name",
    [
        "search",
        "code-search",
        "remember",
        "recap",
        "index-repo",
        "sync",
        "list-datasets",
        "switch-dataset",
        "switch-dataset-list",
    ],
)
def test_remote_url_without_a_key_exits_2_before_any_request(
    claude_only, temp_home, mock_server, isolated_modules, assert_clean_real_home, name
):
    cmd = _command(claude_only, name, temp_home)
    if cmd[0] is None:
        pytest.skip("requires a working POSIX bash (wrapper is not run on Windows)")
    _with_url_option(temp_home, _remote_alias(mock_server.url))
    if name.startswith("switch-dataset"):
        # A launch SessionStart left behind, so the switch gets past finding
        # its launch (which exits 2 on its own) and reaches the guard.
        pc = isolated_modules(claude_only, "_plugin_common")
        pc.ensure_launch_record(LAUNCH_KEY, str(temp_home), dataset="agent_sessions")
    result = _run(cmd, _shell_env(claude_only, temp_home), temp_home)
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert GUARD_TEXT in result.stderr, result.stderr
    assert mock_server.calls == []


def test_the_remote_alias_reaches_the_server_when_a_key_is_set(
    claude_only, temp_home, mock_server, assert_clean_real_home
):
    """Control: the same URL with the key option set gets through, so the
    guard, not a connection failure, is what kept the requests away."""
    if BASH is None:
        pytest.skip("requires a working POSIX bash (wrapper is not run on Windows)")
    _with_url_option(temp_home, _remote_alias(mock_server.url))
    env = _shell_env(claude_only, temp_home, api_key="k-option")
    result = _run(_command(claude_only, "search", temp_home), env, temp_home)
    assert result.returncode == 0, result.stderr
    call = mock_server.assert_called("POST", "/api/v1/recall")
    assert call["headers"].get("X-Api-Key") == "k-option"


# ── unreachable server: no CLI fallback ───────────────────────────────────


@pytest.fixture
def recording_cli(tmp_path):
    """A ``cognee-cli`` on PATH that records any run."""
    if BASH is None or os.name == "nt":
        pytest.skip("needs bash and POSIX shims")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "cli-ran.txt"
    cli = bin_dir / "cognee-cli"
    cli.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\necho \'[]\'\n', encoding="utf-8")
    cli.chmod(0o755)
    return {"path": str(bin_dir), "log": log}


@pytest.mark.parametrize(
    "name, message",
    [
        ("search", "[cognee-search] server unreachable — search not run"),
        ("remember", "[cognee-remember] server unreachable — content not saved"),
    ],
)
def test_unreachable_server_exits_1_without_running_the_cli(
    claude_only, temp_home, closed_port_url, recording_cli, assert_clean_real_home, name, message
):
    env = build_env(
        claude_only,
        temp_home,
        service_url=closed_port_url,
        api_key="k",
        extra={"PATH": recording_cli["path"] + os.pathsep + os.environ.get("PATH", "")},
    )
    result = _run(_command(claude_only, name, temp_home), env, temp_home)
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert message in result.stderr, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "UNREACHABLE"
    assert not recording_cli["log"].exists(), "cognee-cli was started"
