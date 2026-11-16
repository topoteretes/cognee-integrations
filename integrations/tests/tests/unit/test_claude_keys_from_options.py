"""The Claude Code plugin takes its keys from plugin options only (SDK-1411).

The plugin directory holds a plugin that reads a credential from the user's
machine. So the Cognee API key, the LLM key, the embedding key and the login
are plugin options (``userConfig``, the secret ones ``sensitive``), which
Claude Code hands to hooks as ``CLAUDE_PLUGIN_OPTION_*``. This file pins that:

* a key in the shell or in ``~/.cognee/.env`` is dropped, never used, and
  named (name only) so session start can say so;
* the option is the key for the configured server, never for the plugin's own
  local server, and is never written to the key cache;
* a script run from a shell, which has no options, reaches the server through
  the relay a hook started (``_relay.py``).
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from utils.suites import CLAUDE

_SCRIPTS = CLAUDE.scripts_dir
_OPTION = "CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY"
_REMOTE = "https://tenant.cognee.test"


def _python(code: str, home: Path, **env: str) -> dict:
    """Run ``code`` in a fresh interpreter in the scripts folder; its JSON output."""
    base = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "USERPROFILE": str(home),
        "COGNEE_PLUGIN_IN_VENV": "1",
        "COGNEE_IDLE_DISABLED": "1",
    }
    base.update(env)
    ran = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_SCRIPTS),
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert ran.returncode == 0, ran.stderr
    return json.loads(ran.stdout.strip().splitlines()[-1])


_SEEN = """
import json, os, _env_file
_env_file.load_env_file()
print(json.dumps({
    "env": {n: os.environ.get(n) for n in (
        "COGNEE_API_KEY", "COGNEE_PRINCIPAL_API_KEY", "LLM_API_KEY", "EMBEDDING_API_KEY",
        "COGNEE_USER_EMAIL", "COGNEE_USER_PASSWORD", "COGNEE_BASE_URL", "LLM_MODEL")},
    "ignored": _env_file.ignored_key_names(),
}))
"""


def test_a_key_in_the_shell_is_dropped_and_named(tmp_path):
    seen = _python(
        _SEEN,
        tmp_path,
        COGNEE_API_KEY="ck_shell",
        LLM_API_KEY="sk_shell",
        COGNEE_USER_PASSWORD="pw",
        COGNEE_BASE_URL=_REMOTE,
    )
    assert seen["env"]["COGNEE_API_KEY"] is None
    assert seen["env"]["LLM_API_KEY"] is None
    assert seen["env"]["COGNEE_USER_PASSWORD"] is None
    assert seen["env"]["COGNEE_BASE_URL"] == _REMOTE
    assert seen["ignored"] == ["COGNEE_API_KEY", "COGNEE_USER_PASSWORD", "LLM_API_KEY"]


def test_a_key_in_the_env_file_is_never_loaded(tmp_path):
    (tmp_path / ".cognee").mkdir()
    (tmp_path / ".cognee" / ".env").write_text(
        f'COGNEE_BASE_URL="{_REMOTE}"\nCOGNEE_API_KEY="ck_file"\n'
        'export LLM_API_KEY=sk_file\nLLM_MODEL="openai/gpt-4o-mini"\nEMBEDDING_API_KEY=\n',
        encoding="utf-8",
    )
    seen = _python(_SEEN, tmp_path)
    assert seen["env"]["COGNEE_API_KEY"] is None
    assert seen["env"]["LLM_API_KEY"] is None
    # Everything that is not a key still comes from the file.
    assert seen["env"]["COGNEE_BASE_URL"] == _REMOTE
    assert seen["env"]["LLM_MODEL"] == "openai/gpt-4o-mini"
    # An empty value is not a key the user has to move.
    assert seen["ignored"] == ["COGNEE_API_KEY", "LLM_API_KEY"]


def test_the_parser_never_returns_a_keys_value(tmp_path):
    path = tmp_path / "some.env"
    path.write_text("COGNEE_API_KEY=ck_file\nCOGNEE_PLUGIN_DATASET=notes\n", encoding="utf-8")
    code = f"""
import json, _env_file
from pathlib import Path
named = []
values = _env_file.parse_env_file(Path({str(path)!r}), named)
print(json.dumps({{"values": values, "named": named}}))
"""
    seen = _python(code, tmp_path)
    assert seen["values"] == {"COGNEE_API_KEY": "", "COGNEE_PLUGIN_DATASET": "notes"}
    assert seen["named"] == ["COGNEE_API_KEY"]


_KEYS = """
import json, os, _plugin_common as pc, config
cfg = config.load_config()
option = os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "")
pc.save_cached_api_key("https://tenant.cognee.test", option)
print(json.dumps({
    "config": cfg.get("api_key"),
    "remote": pc.user_cloud_key("https://tenant.cognee.test"),
    "own_local": pc.user_cloud_key("http://localhost:8011"),
    "resolved": pc._api_key_with_source("https://tenant.cognee.test"),
    "cached": pc.load_cached_api_key("https://tenant.cognee.test"),
    "in_env": os.environ.get("COGNEE_API_KEY"),
}))
"""


def test_the_option_is_the_key_for_the_configured_server_only(tmp_path):
    seen = _python(_KEYS, tmp_path, COGNEE_BASE_URL=_REMOTE, **{_OPTION: "ck_option"})
    assert seen["config"] == "ck_option"
    assert seen["remote"] == "ck_option"
    assert seen["resolved"] == ["ck_option", "plugin_option"]
    # The plugin's own local server has a key of its own.
    assert seen["own_local"] == ""
    # Never written to the cache, never copied into a variable.
    assert seen["cached"] == ""
    assert seen["in_env"] is None


def test_the_option_reaches_a_local_address_only_when_the_terminal_is_pinned_to_cloud(tmp_path):
    code = 'import json, _plugin_common as pc; print(json.dumps(pc.user_cloud_key("http://localhost:8011")))'
    assert _python(code, tmp_path, **{_OPTION: "ck_option"}) == ""
    assert _python(code, tmp_path, COGNEE_BACKEND="cloud", **{_OPTION: "ck_option"}) == "ck_option"


def test_the_local_server_gets_the_llm_and_embedding_key_options(tmp_path):
    code = """
import json, os, _env_file
_env_file.load_env_file()
print(json.dumps({"server": _env_file.server_key_env(), "own": os.environ.get("LLM_API_KEY")}))
"""
    seen = _python(
        code,
        tmp_path,
        CLAUDE_PLUGIN_OPTION_LLM_API_KEY="sk_option",
        CLAUDE_PLUGIN_OPTION_EMBEDDING_API_KEY="ek_option",
    )
    assert seen["server"] == {"LLM_API_KEY": "sk_option", "EMBEDDING_API_KEY": "ek_option"}
    assert seen["own"] is None


def test_the_login_comes_from_its_options(tmp_path):
    code = (
        "import json, config; c = config.load_config(); "
        'print(json.dumps([c["user_email"], c["user_password"]]))'
    )
    assert _python(code, tmp_path, COGNEE_USER_EMAIL="shell@x", COGNEE_USER_PASSWORD="pw") == [
        "default_user@example.com",
        "default_password",
    ]
    assert _python(
        code,
        tmp_path,
        CLAUDE_PLUGIN_OPTION_COGNEE_USER_EMAIL="me@x",
        CLAUDE_PLUGIN_OPTION_COGNEE_USER_PASSWORD="secret",
    ) == ["me@x", "secret"]


# --- the server URL option ----------------------------------------------------

_URL = (
    "import json, os, _env_file; _env_file.load_env_file(); "
    'print(json.dumps(os.environ.get("COGNEE_BASE_URL", "")))'
)


def test_the_url_option_is_used_by_hooks_and_recorded_for_the_rest(tmp_path):
    hook = {"CLAUDE_PLUGIN_ROOT": "/plugin"}
    assert _python(_URL, tmp_path, CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE, **hook) == _REMOTE
    # A process with no options (the status line) resolves the same server.
    assert _python(_URL, tmp_path) == _REMOTE
    # A real export wins over the option.
    assert (
        _python(
            _URL,
            tmp_path,
            COGNEE_BASE_URL="https://export.test",
            CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE,
            **hook,
        )
        == "https://export.test"
    )
    # An emptied option removes the record.
    assert _python(_URL, tmp_path, **hook) == ""
    assert _python(_URL, tmp_path) == ""


def test_the_url_option_wins_over_the_env_file(tmp_path):
    (tmp_path / ".cognee").mkdir()
    (tmp_path / ".cognee" / ".env").write_text(
        'COGNEE_BASE_URL="https://file.test"\n', encoding="utf-8"
    )
    assert _python(_URL, tmp_path) == "https://file.test"
    assert (
        _python(
            _URL,
            tmp_path,
            CLAUDE_PLUGIN_ROOT="/plugin",
            CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL=_REMOTE,
        )
        == _REMOTE
    )


# --- the relay ----------------------------------------------------------------


def _start_relay(home: Path, url: str, key: str, **env: str) -> dict:
    """Start the relay the way a hook does; its state once it is up."""
    code = "import json, _relay; _relay.ensure_relay(); print(json.dumps(1))"
    hook_env = dict(env, COGNEE_BASE_URL=url, **{_OPTION: key})
    hook_env["COGNEE_IDLE_DISABLED"] = "0"
    _python(code, home, **hook_env)
    state_file = home / ".cognee-plugin" / "claude-code" / "relay" / "relay.json"
    for _ in range(100):
        if state_file.is_file():
            return json.loads(state_file.read_text(encoding="utf-8"))
        time.sleep(0.05)
    raise AssertionError("the relay did not start")


def _stop(state: dict) -> None:
    try:
        os.kill(int(state["pid"]), signal.SIGTERM)
    except OSError:
        pass


def _shell(home: Path, *argv: str, **env: str) -> subprocess.CompletedProcess:
    """Run a script as a skill's shell does: no options in the environment."""
    base = {"PATH": os.environ.get("PATH", ""), "HOME": str(home), "USERPROFILE": str(home)}
    base.update({"COGNEE_PLUGIN_IN_VENV": "1", "COGNEE_IDLE_DISABLED": "1"})
    base.update(env)
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(_SCRIPTS),
        env=base,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("port_mode", ["", "1"], ids=["unix-socket", "loopback-port"])
def test_a_shell_script_reaches_the_server_through_the_relay(temp_home, mock_server, port_mode):
    if not port_mode and not hasattr(__import__("socket"), "AF_UNIX"):
        pytest.skip("no Unix sockets here")
    extra = {"COGNEE_RELAY_PORT_MODE": port_mode} if port_mode else {}
    state = _start_relay(temp_home, mock_server.url, "ck_option", **extra)
    try:
        assert bool(state.get("port")) is bool(port_mode)
        ran = _shell(temp_home, "list-datasets.py", COGNEE_BASE_URL=mock_server.url)
        assert ran.returncode == 0, ran.stderr
        # The shell had no key; the request the relayed script sent carried the option.
        sent = mock_server.assert_called("GET", "/api/v1/datasets")
        assert sent["headers"].get("X-Api-Key") == "ck_option"
        assert "ck_option" not in ran.stdout + ran.stderr
    finally:
        _stop(state)


def test_without_a_relay_a_shell_script_runs_in_place_and_has_no_key(temp_home, mock_server):
    ran = _shell(temp_home, "list-datasets.py", COGNEE_BASE_URL=mock_server.url)
    sent = mock_server.assert_called("GET", "/api/v1/datasets")
    assert not sent["headers"].get("X-Api-Key")
    assert ran.returncode in (0, 1)


def test_the_relay_runs_only_the_plugins_listed_scripts(temp_home, mock_server):
    state = _start_relay(temp_home, mock_server.url, "ck_option")
    try:
        for script in ("evil.sh", "../../outside.py", "session-start.py", "/bin/sh"):
            ran = _shell(temp_home, "_relay.py", "run", script)
            assert ran.returncode == 2, (script, ran.stderr)
            assert "does not run" in ran.stderr
    finally:
        _stop(state)


def test_the_loopback_relay_refuses_a_request_without_its_code(temp_home, mock_server):
    import socket

    state = _start_relay(temp_home, mock_server.url, "ck_option", COGNEE_RELAY_PORT_MODE="1")
    try:
        with socket.create_connection(("127.0.0.1", int(state["port"])), timeout=5) as conn:
            request = {"script": "list-datasets.py", "argv": [], "code": "not-the-code"}
            conn.sendall(json.dumps(request).encode() + b"\n")
            reply = json.loads(conn.recv(4096).decode())
        assert reply["code"] == 2 and "refused" in reply["stderr"]
        assert [c for c in mock_server.calls if c["path"] == "/api/v1/datasets"] == []
    finally:
        _stop(state)


def test_the_relay_does_not_start_without_the_key_option(tmp_path):
    code = "import json, _relay; _relay.ensure_relay(); print(json.dumps(1))"
    _python(code, tmp_path, COGNEE_IDLE_DISABLED="0", COGNEE_BASE_URL=_REMOTE)
    time.sleep(0.3)
    assert not (tmp_path / ".cognee-plugin" / "claude-code" / "relay" / "relay.json").exists()


# --- what session start tells a user whose keys are elsewhere -----------------


@pytest.fixture
def start(hook_module, temp_home, tmp_path, monkeypatch):
    module = hook_module(CLAUDE, "session-start.py")
    monkeypatch.setattr(module, "_KEY_OPTIONS_NOTICE", tmp_path / "notice.json")
    for name in ("COGNEE_BASE_URL", "COGNEE_BACKEND", "COGNEE_PLUGIN_IGNORED_SETTINGS", _OPTION):
        monkeypatch.delenv(name, raising=False)
    return module


def test_cloud_without_the_key_option_is_said_at_every_start(start, monkeypatch):
    monkeypatch.setenv("COGNEE_BASE_URL", _REMOTE)
    monkeypatch.setenv("COGNEE_PLUGIN_IGNORED_SETTINGS", "COGNEE_API_KEY")
    first = start._key_options_message()
    assert "Cognee is not connected" in first
    assert "COGNEE_API_KEY in your shell or ~/.cognee/.env is no longer read" in first
    assert "/plugin" in first
    assert start._key_options_message() == first
    monkeypatch.setenv(_OPTION, "ck_option")
    assert start._key_options_message() == ""


def test_an_ignored_local_key_is_said_once(start, monkeypatch):
    monkeypatch.setenv("COGNEE_PLUGIN_IGNORED_SETTINGS", "LLM_API_KEY")
    first = start._key_options_message()
    assert "no longer reads LLM_API_KEY" in first
    assert start._key_options_message() == ""
    # A key whose option is filled in is not mentioned at all.
    monkeypatch.setenv("COGNEE_PLUGIN_IGNORED_SETTINGS", "LLM_API_KEY,EMBEDDING_API_KEY")
    monkeypatch.setenv("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "sk_option")
    assert "no longer reads EMBEDDING_API_KEY from" in start._key_options_message()


def test_nothing_is_said_when_no_key_was_found(start, monkeypatch):
    assert start._key_options_message() == ""
