"""Session setup lets a later hook prepare project memory only once it gives up.

``route()`` never runs the first ``prepare()``: session setup does, once the key
and the dataset are final. When the lazy bootstrap gives up before ``prepare()``
(the server never came up, registration was refused or raised), nothing else
will run it, so setup calls ``allow_retry`` and a later ``route()`` prepares the
session. A bootstrap that registered leaves the state to its own ``prepare()``.

Claude Code and Codex only: the other hosts have no project memory retry.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys

import pytest

SUITES = {"claude-code", "codex"}


class _NoLock:
    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


@pytest.fixture
def bootstrap(suite, hook_module, monkeypatch, tmp_path):
    if suite.name not in SUITES:
        pytest.skip("the project memory retry ships for Claude Code and Codex only")
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    for name in ("phase_begin", "phase_end", "phase_touch"):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, lambda *a, **k: None)
    monkeypatch.setattr(module, "_bootstrap_singleflight", lambda: _NoLock())
    monkeypatch.setattr(module, "_ensure_local_server_running", lambda *a, **k: None)
    # The real _project_memory_gave_up -> allow_retry, on a real pending state.
    common = sys.modules["_plugin_common"]
    monkeypatch.setattr(common, "_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "key"))
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", "project-fixed")
    pm = importlib.import_module("_project_memory")
    pm.begin("primary", "sid", str(tmp_path))

    def run(*, healthy: bool, heavy=None) -> dict:
        monkeypatch.setattr(module, "_health_ok", lambda *a, **k: healthy)
        monkeypatch.setattr(module, "_wait_for_health", lambda *a, **k: healthy)
        if heavy is not None:
            monkeypatch.setattr(module, "_run_heavy", heavy)
        asyncio.run(
            module._run_bootstrap({"session_id": "sid", "session_key": "k", "dataset": "primary"})
        )
        return json.loads(pm._path("primary", "sid").read_text())

    return run


def test_a_server_that_never_came_up_lets_a_later_hook_prepare(bootstrap):
    state = bootstrap(healthy=False)
    assert state["pending"] and "retry_at" in state


def test_a_refused_registration_lets_a_later_hook_prepare(bootstrap):
    async def refused(*a, **k):
        return "", "", False

    state = bootstrap(healthy=True, heavy=refused)
    assert state["pending"] and "retry_at" in state


def test_a_registration_that_raised_lets_a_later_hook_prepare(bootstrap):
    async def boom(*a, **k):
        raise RuntimeError("registration refused")

    state = bootstrap(healthy=True, heavy=boom)
    assert state["pending"] and "retry_at" in state


def test_a_registered_session_leaves_the_state_to_its_own_prepare(bootstrap):
    async def registered(*a, on_registered=None, **k):
        if on_registered:
            on_registered()
        return "", "", True

    # _run_heavy is faked, so prepare() did not run: the state stays pending,
    # and without a retry_at no hook prepares it ahead of setup.
    state = bootstrap(healthy=True, heavy=registered)
    assert state["pending"] and "retry_at" not in state
