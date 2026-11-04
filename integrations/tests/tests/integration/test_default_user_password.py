"""The password-less default user of cognee >= 1.6.0 (SDK-740).

cognee 1.6.0 stopped baking ``default_password`` into the default user: the API
server creates that user at startup only when ``DEFAULT_USER_PASSWORD`` is set,
sets the password once, and never rewrites a stored one. Logging into a user
created without it answers ``400 "This user does not have a password"``.

Two consequences for the plugins, both asserted here for every suite:

* A server *this plugin boots* must be handed the well-known local credentials
  in its environment, or a fresh install can never mint its owner API key. An
  operator's own ``DEFAULT_USER_*`` export wins over the plugin's default.
* Against a server the plugin did *not* boot, the two 400 answers must produce
  an actionable message (start the server with ``DEFAULT_USER_PASSWORD``, or set
  ``COGNEE_API_KEY``) instead of the old generic "set the credentials correctly".
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest


@pytest.fixture
def session_start(suite, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return module


def _spawn_capturing_env(session_start, closed_port_url, monkeypatch) -> dict:
    """Drive the boot point with the uvicorn spawn faked; return its ``env``."""
    captured: dict = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    real_popen = subprocess.Popen

    def fake_popen(argv, **kwargs):
        if "uvicorn" not in [str(a) for a in argv]:
            return real_popen(argv, **kwargs)
        captured["argv"] = argv
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(session_start, "ensure_cognee_installed", lambda *a, **k: True)
    monkeypatch.setattr(session_start.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(session_start, "_health_ok", lambda *a, **k: "argv" in captured)
    monkeypatch.setattr(session_start, "write_server_pidfile", lambda *a, **k: None)
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    session_start._ensure_local_server_running({"base_url": closed_port_url}, health_timeout=2.0)
    assert "uvicorn" in captured["argv"]
    return captured["env"]


def test_spawned_server_is_given_the_local_default_user(
    session_start, closed_port_url, monkeypatch
):
    monkeypatch.delenv("DEFAULT_USER_EMAIL", raising=False)
    monkeypatch.delenv("DEFAULT_USER_PASSWORD", raising=False)
    env = _spawn_capturing_env(session_start, closed_port_url, monkeypatch)
    assert env["DEFAULT_USER_EMAIL"] == "default_user@example.com"
    assert env["DEFAULT_USER_PASSWORD"] == "default_password"
    # Still in agent mode: the new variables ride along, they do not replace.
    assert env["COGNEE_AGENT_MODE"] == "true"


def test_operator_default_user_export_wins(session_start, closed_port_url, monkeypatch):
    monkeypatch.setenv("DEFAULT_USER_EMAIL", "ops@example.com")
    monkeypatch.setenv("DEFAULT_USER_PASSWORD", "ops-secret")
    env = _spawn_capturing_env(session_start, closed_port_url, monkeypatch)
    assert env["DEFAULT_USER_EMAIL"] == "ops@example.com"
    assert env["DEFAULT_USER_PASSWORD"] == "ops-secret"


def test_server_default_matches_the_login_default(suite, session_start, isolated_modules):
    """The credentials handed to the server and the ones config.py logs in with
    must be the same pair, or a fresh install cannot bootstrap."""
    config = isolated_modules(suite, "config")
    assert session_start._LOCAL_DEFAULT_USER_EMAIL == config._DEFAULTS["user_email"]
    assert session_start._LOCAL_DEFAULT_USER_PASSWORD == config._DEFAULTS["user_password"]


def _login(session_start, mock_server):
    return asyncio.run(
        session_start._login_default_user_for_owner_api_key(
            mock_server.url,
            {"user_email": "default_user@example.com", "user_password": "default_password"},
        )
    )


def test_password_less_default_user_gets_an_actionable_error(session_start, mock_server):
    mock_server.identity.no_password_user = True
    with pytest.raises(RuntimeError) as excinfo:
        _login(session_start, mock_server)
    message = str(excinfo.value)
    assert "400" in message
    assert "DEFAULT_USER_PASSWORD" in message
    assert "COGNEE_API_KEY" in message
    assert "correctly" not in message  # not the old generic advice


def test_rejected_credentials_name_the_settings(session_start, mock_server):
    mock_server.identity.wrong_password = True
    with pytest.raises(RuntimeError) as excinfo:
        _login(session_start, mock_server)
    message = str(excinfo.value)
    assert "COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD" in message
    assert "COGNEE_API_KEY" in message
    assert "DEFAULT_USER_PASSWORD" not in message


def test_other_login_failures_keep_the_generic_advice(session_start, mock_server):
    mock_server.identity.reject_login = True  # 401
    with pytest.raises(RuntimeError, match="COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD"):
        _login(session_start, mock_server)


def test_healthy_login_still_mints_the_owner_key(session_start, mock_server):
    assert _login(session_start, mock_server).startswith("apikey-")
