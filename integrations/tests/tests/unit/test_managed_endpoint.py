"""COGNEE_MANAGED_ENDPOINT — never boot a local server over a managed deployment.

A self-hosted stack (docker compose, systemd, ...) commonly lives on loopback.
When it was down at session start, the boot decision saw "local URL, nothing
serving" and booted the embedded server on the stack's own port: the shadow
instance squatted the port, 401'd the stack's API keys and captured memory into
a database nobody reads. The opt-in flag declares the URL externally managed:
an outage is reported loudly and nothing is installed or booted.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from utils.isolation import run_hook
from utils.suites import cognee_home, plugin_root


@pytest.fixture
def _supported(suite):
    if suite.name not in ("claude-code", "codex"):
        pytest.skip(f"{suite.name}: no COGNEE_MANAGED_ENDPOINT support")


@pytest.fixture
def pc(suite, _supported, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


class _Booted(Exception):
    """Raised by the fake bootstrap spawn: the launch took the boot path."""


@pytest.fixture
def session_start(suite, _supported, hook_module, monkeypatch):
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "false")
    monkeypatch.setattr(module, "_LAZY_BOOTSTRAP", True)

    def _boot(*a, **k):
        raise _Booted()

    monkeypatch.setattr(module, "_spawn_bootstrap", _boot)
    return module


def _run_start(module, payloads, url, monkeypatch, *, verdict, heavy_ok=True):
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    monkeypatch.setattr(module, "server_presence", lambda *a, **k: (verdict, {}))
    heavy: list[bool] = []

    async def fake_heavy(*a, managed_endpoint, **k):
        heavy.append(managed_endpoint)
        return "", "", heavy_ok

    monkeypatch.setattr(module, "_run_heavy", fake_heavy)
    result = asyncio.run(module._start(payloads.session_start()))
    return result, heavy


# --- the flag -------------------------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on", " true "])
def test_truthy_values_enable_the_flag(pc, monkeypatch, value):
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", value)
    assert pc.managed_endpoint_enabled({}) is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "maybe"])
def test_other_values_leave_it_off(pc, monkeypatch, value):
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", value)
    assert pc.managed_endpoint_enabled({}) is False


def test_config_key_enables_the_flag(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_MANAGED_ENDPOINT", raising=False)
    assert pc.managed_endpoint_enabled({"managed_endpoint": "true"}) is True
    assert pc.managed_endpoint_enabled({"managed_endpoint": True}) is True
    assert pc.managed_endpoint_enabled({}) is False
    assert pc.managed_endpoint_enabled(None) is False


# --- the boot point ---------------------------------------------------------------


def test_boot_point_refuses_before_any_install(session_start, closed_port_url, monkeypatch):
    """The single spawn choke point: covers the recovery worker and any other
    path that reaches it, not only the session-start decision."""
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    installs: list[int] = []
    monkeypatch.setattr(
        session_start, "ensure_cognee_installed", lambda *a, **k: installs.append(1) or True
    )
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    with pytest.raises(RuntimeError, match="COGNEE_MANAGED_ENDPOINT"):
        session_start._ensure_local_server_running(
            {"base_url": closed_port_url}, health_timeout=0.5
        )
    assert not installs


# --- the session-start decision ------------------------------------------------------


def test_down_managed_endpoint_reports_offline_and_never_boots(
    session_start, payloads, temp_home, closed_port_url, monkeypatch
):
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    result, heavy = _run_start(
        session_start,
        payloads,
        closed_port_url,
        monkeypatch,
        verdict=session_start.PRESENCE_ABSENT,
    )
    assert not heavy
    # Claude Code displays the top-level systemMessage; keep the hook-specific copy.
    assert "Cognee Memory OFFLINE" in result["systemMessage"]
    hso = result["hookSpecificOutput"]
    assert "Cognee Memory OFFLINE" in hso["systemMessage"]
    assert "OFFLINE" in hso["additionalContext"]
    marker = json.loads((plugin_root(temp_home) / "server-ready.json").read_text())
    assert marker["state"] == "unreachable"
    assert marker["base_url"] == closed_port_url


def test_live_managed_endpoint_connects_normally(
    session_start, payloads, closed_port_url, monkeypatch
):
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    result, heavy = _run_start(
        session_start,
        payloads,
        closed_port_url,
        monkeypatch,
        verdict=session_start.PRESENCE_READY,
    )
    assert heavy == [True]  # connect only
    assert "OFFLINE" not in json.dumps(result)


def test_failed_connect_is_not_retried_by_a_boot(
    session_start, payloads, closed_port_url, monkeypatch
):
    """The out-of-band retry spawns the bootstrap worker, which can boot: a
    managed endpoint must never reach it."""
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    result, heavy = _run_start(
        session_start,
        payloads,
        closed_port_url,
        monkeypatch,
        verdict=session_start.PRESENCE_READY,
        heavy_ok=False,
    )
    assert heavy == [True]
    assert result == {}


def test_without_the_flag_a_down_local_endpoint_still_boots(
    session_start, payloads, closed_port_url, monkeypatch
):
    """Default behavior is unchanged: local mode boots its own server."""
    monkeypatch.delenv("COGNEE_MANAGED_ENDPOINT", raising=False)
    with pytest.raises(_Booted):
        _run_start(
            session_start,
            payloads,
            closed_port_url,
            monkeypatch,
            verdict=session_start.PRESENCE_ABSENT,
        )


# --- the Claude observer ---------------------------------------------------------------


def test_observer_stays_off_for_a_managed_endpoint(suite, isolated_modules, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the Claude observer is a Claude Code feature")
    observer = isolated_modules(suite, "_observer")
    monkeypatch.setenv("COGNEE_LLM_OBSERVER", "true")
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    decision = observer.resolve_observer({"base_url": "http://localhost:8011"})
    assert decision["active"] is False
    assert decision["reason"] == "managed_endpoint"
    assert decision["error"] == ""
    assert "COGNEE_MANAGED_ENDPOINT" in observer.describe(decision)


# --- the real hook, as a subprocess ------------------------------------------------------


@pytest.mark.parametrize("via", ["env", "dotenv"])
def test_hook_reports_offline_without_building_a_venv(
    suite, _supported, temp_home, payloads, closed_port_url, via
):
    extra = {}
    if via == "env":
        extra["COGNEE_MANAGED_ENDPOINT"] = "true"
    else:
        dotenv = cognee_home(temp_home) / ".env"
        dotenv.parent.mkdir(parents=True, exist_ok=True)
        dotenv.write_text("COGNEE_MANAGED_ENDPOINT=true\n", encoding="utf-8")
    result = run_hook(
        suite,
        "session-start.py",
        stdin=payloads.session_start(),
        home=temp_home,
        service_url=closed_port_url,
        extra_env={**extra, "COGNEE_LAZY_BOOTSTRAP": "1"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert "Cognee Memory OFFLINE" in output["systemMessage"]
    assert not (plugin_root(temp_home) / "venv").exists()
