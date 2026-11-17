"""Local mode with no LLM chosen starts no server (Claude Code, SDK-1411).

cognee 1.6.6 falls back on its own to its keyless defaults when its server has
no usable LLM: the GLiNER extractor, whose first cognify pip-installs torch and
gliner2, and the fastembed embedder, which cannot search a dataset built with
a provider's embedder. That happened to every 1.8.1 user whose LLM_API_KEY sat
in ~/.cognee/.env, which 1.8.7 no longer reads. Local models are now a choice
(the local models option); with no LLM chosen the plugin starts no server and
says how to choose one.

The shared test environment turns the local models option on so the tests that
boot the mock server have an LLM choice; these tests clear it.
"""

from __future__ import annotations

import json
import subprocess

import pytest
from utils.options_holder import set_options

_NO_CHOICE = {"CLAUDE_PLUGIN_OPTION_LOCAL_MODELS": ""}


@pytest.fixture
def claude(suite):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the local models option is a Claude Code feature")
    return suite


@pytest.fixture
def session_start(claude, hook_module, monkeypatch):
    module = hook_module(claude, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return module


def _start(claude, run_hook, temp_home, url, env):
    payload = {"session_id": "no-llm-host", "cwd": str(temp_home), "source": "startup"}
    result = run_hook(claude, "session-start.py", stdin=payload, service_url=url, env=env)
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout.strip().splitlines()[-1])
    marker = json.loads(
        (temp_home / ".cognee-plugin" / "server-ready.json").read_text(encoding="utf-8")
    )
    return output, marker


def test_no_llm_chosen_starts_no_server_and_says_how_to_choose(
    claude, run_hook, temp_home, closed_port_url
):
    output, marker = _start(claude, run_hook, temp_home, closed_port_url, _NO_CHOICE)
    message = output["systemMessage"]
    assert "Cognee Memory paused" in message
    assert "local models option" in message
    assert "COGNEE_LLM_OBSERVER=true" in message
    assert marker["state"] == "no_llm_chosen"
    log = (temp_home / ".cognee-plugin" / "claude-code" / "hook.log").read_text(encoding="utf-8")
    assert "local_boot_skipped_no_llm" in log
    assert "server_bootstrap_lock_acquired" not in log


def test_an_ignored_llm_key_starts_no_server_and_skips_the_general_notice(
    claude, run_hook, temp_home, closed_port_url
):
    env = {**_NO_CHOICE, "LLM_API_KEY": "sk-legacy-from-the-shell"}
    output, marker = _start(claude, run_hook, temp_home, closed_port_url, env)
    # The key notice says what to do (see the next test); the general one would
    # repeat it.
    assert "Cognee Memory paused" not in output["systemMessage"]
    assert marker["state"] == "no_llm_chosen"


def test_the_ignored_key_notice_names_every_choice(session_start, monkeypatch):
    # The default local URL: only that one counts as local mode here.
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    set_options(monkeypatch, local_models="")
    monkeypatch.setenv("COGNEE_PLUGIN_IGNORED_SETTINGS", "LLM_API_KEY")
    message = session_start._key_options_message()
    assert "the local server is not started" in message
    assert "local models option" in message
    assert "COGNEE_LLM_OBSERVER=true" in message
    # With local models chosen, nothing is paused.
    set_options(monkeypatch, local_models="true")
    assert "paused" not in session_start._key_options_message()


def test_the_boot_point_refuses_before_any_install(session_start, closed_port_url, monkeypatch):
    set_options(monkeypatch, local_models="")

    def _no_install(*args, **kwargs):
        raise AssertionError("nothing may be installed without an LLM choice")

    monkeypatch.setattr(session_start, "ensure_cognee_installed", _no_install)
    monkeypatch.setattr(session_start, "_health_ok", lambda *a, **k: False)
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    with pytest.raises(RuntimeError, match="no LLM is set up"):
        session_start._ensure_local_server_running({"base_url": closed_port_url})


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
    return captured["env"]


def test_only_the_local_models_option_lets_cognee_install_gliner(
    session_start, closed_port_url, monkeypatch
):
    set_options(monkeypatch, local_models="true")
    env = _spawn_capturing_env(session_start, closed_port_url, monkeypatch)
    assert env["GLINER_AUTO_INSTALL"] == "true"


def test_a_server_with_an_llm_never_installs_gliner(session_start, closed_port_url, monkeypatch):
    set_options(monkeypatch, local_models="")
    set_options(monkeypatch, llm_api_key="sk-test")
    monkeypatch.setenv("GLINER_AUTO_INSTALL", "true")
    env = _spawn_capturing_env(session_start, closed_port_url, monkeypatch)
    assert env["GLINER_AUTO_INSTALL"] == "false"
