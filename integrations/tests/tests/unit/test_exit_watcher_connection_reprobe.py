"""Regression tests for the exit-watcher's connection self-heal.

Status-line recovery from a failure verdict is otherwise prompt-driven: once
``server-ready.json`` says unreachable / server_error / not_responding /
auth_failed, nothing re-checks it until a hook runs, so a server that recovers
while the user is idle leaves a stale red ✕ for up to the renderer's 30-minute
fade. The exit-watcher is the only plugin process whose lifetime matches the
host session, so it hosts a throttled background re-probe. These pin the
contract: only failure states are probed; only this session's base_url; only a
positive "ready" is ever written; auth_failed heals only on an authenticated
success; timeouts are no verdict.
"""

from __future__ import annotations

import time

import pytest

_URL = "http://localhost:8011"
_OTHER_URL = "https://tenant-f8c21da4-6674-4cc5-bc56-de5e93db881d.aws.cognee.ai"


@pytest.fixture
def drive(suite, hook_module, isolated_modules, monkeypatch):
    """Run _reprobe_connection with patched seams; return what it did.

    Same load-order caveat as the credits tests: the watcher imports
    _plugin_common function-locally, so load the watcher first and then a fresh
    _plugin_common — the late import binds to that same sys.modules entry.
    """

    def _drive(*, marker, authed="unknown", health="unknown", service_url=_URL, api_key="k"):
        watcher = hook_module(suite, "exit-watcher.py")
        pc = isolated_modules(suite, "_plugin_common")
        monkeypatch.setattr(watcher, "_log", lambda *a, **k: None)
        monkeypatch.setattr(watcher, "_last_conn_reprobe_at", 0.0)

        calls = {"authed": 0, "health": 0, "ready": []}

        def _authed(url, key, timeout=0):
            calls["authed"] += 1
            return authed

        def _health(url, timeout=0):
            calls["health"] += 1
            return health

        monkeypatch.setattr(pc, "read_connection_state", lambda: dict(marker))
        monkeypatch.setattr(pc, "authed_liveness", _authed)
        monkeypatch.setattr(pc, "probe_health", _health)
        monkeypatch.setattr(
            pc, "mark_server_ready", lambda url, version="": calls["ready"].append(url)
        )
        watcher._reprobe_connection(service_url, api_key)
        return calls

    return _drive


def _stale(state, url=_URL):
    return {"state": state, "base_url": url, "checked_at": time.time() - 120}


def test_ready_marker_is_not_probed(drive):
    calls = drive(marker=_stale("ready"), authed="ready")
    assert calls["authed"] == 0 and calls["health"] == 0 and calls["ready"] == []


def test_empty_marker_is_not_probed(drive):
    calls = drive(marker={}, authed="ready")
    assert calls["authed"] == 0 and calls["ready"] == []


@pytest.mark.parametrize("state", ["unreachable", "server_error", "not_responding"])
def test_failure_state_heals_on_authed_ready(drive, state):
    calls = drive(marker=_stale(state), authed="ready")
    assert calls["ready"] == [_URL]
    assert calls["health"] == 0  # authed verdict was conclusive; no fallback


@pytest.mark.parametrize("state", ["unreachable", "server_error", "not_responding"])
def test_failure_state_heals_on_unauthed_health_when_no_key(drive, state):
    # authed_liveness returns "unknown" without a key → fall back to /health.
    calls = drive(marker=_stale(state), authed="unknown", health="ready", api_key="")
    assert calls["ready"] == [_URL]


def test_auth_failed_heals_only_on_authenticated_success(drive):
    healed = drive(marker=_stale("auth_failed"), authed="ready")
    assert healed["ready"] == [_URL]
    unauthed = drive(marker=_stale("auth_failed"), authed="unknown", health="ready")
    assert unauthed["ready"] == []  # /health 200 says nothing about the key


@pytest.mark.parametrize(
    "verdict", ["slow", "unknown", "unreachable", "server_error", "auth_failed"]
)
def test_non_ready_verdict_writes_nothing(drive, verdict):
    # The re-probe may heal a red, never paint or re-stamp one.
    calls = drive(marker=_stale("unreachable"), authed=verdict, health="slow")
    assert calls["ready"] == []


def test_other_base_url_is_left_alone(drive):
    # The marker is machine-wide; a cloud session must not clear a local verdict.
    calls = drive(marker=_stale("unreachable", url=_OTHER_URL), authed="ready")
    assert calls["authed"] == 0 and calls["ready"] == []


def test_trailing_slash_does_not_defeat_url_guard(drive):
    calls = drive(marker=_stale("unreachable", url=_URL + "/"), authed="ready")
    assert calls["ready"] == [_URL]


def test_fresh_marker_is_throttled(drive):
    fresh = {"state": "unreachable", "base_url": _URL, "checked_at": time.time()}
    calls = drive(marker=fresh, authed="ready")
    assert calls["authed"] == 0 and calls["ready"] == []


def test_recent_probe_is_throttled(suite, hook_module, isolated_modules, monkeypatch):
    watcher = hook_module(suite, "exit-watcher.py")
    pc = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(watcher, "_log", lambda *a, **k: None)
    monkeypatch.setattr(watcher, "_last_conn_reprobe_at", time.time() - 5)
    monkeypatch.setattr(pc, "read_connection_state", lambda: _stale("unreachable"))
    probed = []
    monkeypatch.setattr(pc, "authed_liveness", lambda *a, **k: probed.append(1) or "ready")
    monkeypatch.setattr(pc, "mark_server_ready", lambda *a, **k: probed.append("ready"))
    watcher._reprobe_connection(_URL, "k")
    assert probed == []


def test_empty_bootstrap_falls_back_to_local_default(drive, isolated_modules, suite):
    # No bootstrap URL → the plugin's local default; the marker must match it.
    pc = isolated_modules(suite, "_plugin_common")
    local = pc._normalize_service_url(pc._local_api_url())
    assert local
    healed = drive(marker=_stale("unreachable", url=local), authed="ready", service_url="")
    assert healed["ready"] == [local]
    other = drive(marker=_stale("unreachable", url=_OTHER_URL), authed="ready", service_url="")
    assert other["ready"] == []
