"""Session setup hands project memory to ``route()`` whenever it ends unprepared.

``route()`` never runs the first ``prepare()``: session setup does, once the key
and the dataset are final. Whoever owns setup last (the SessionStart hook, or
the worker it handed the session to: a spawned bootstrap or the Claude cloud
worker) calls ``allow_retry`` when it ends, however it ends, so a later
``route()`` prepares a session that setup never prepared. A prepared or
backed-off state is left alone, and a session handed to a worker is the
worker's.

Claude Code and Codex only: the other hosts have no project memory retry.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys

import pytest
from utils.coding_memory import require_coding


class _NoLock:
    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


@pytest.fixture
def bootstrap(suite, hook_module, monkeypatch, tmp_path):
    require_coding(suite)
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

    monkeypatch.setattr(common, "_json_http_request", common._json_http_request)

    def run(*, healthy: bool, heavy=None, singleflight=None) -> dict:
        monkeypatch.setattr(module, "_health_ok", lambda *a, **k: healthy)
        monkeypatch.setattr(module, "_wait_for_health", lambda *a, **k: healthy)
        if heavy is not None:
            monkeypatch.setattr(module, "_run_heavy", heavy)
        if singleflight is not None:
            monkeypatch.setattr(module, "_bootstrap_singleflight", singleflight)
        try:
            asyncio.run(
                module._run_bootstrap(
                    {"session_id": "sid", "session_key": "k", "dataset": "primary"}
                )
            )
        except _Step1Failed:
            pass  # reaches main()'s handler; the state is what matters here
        return json.loads(pm._path("primary", "sid").read_text())

    return run


async def _refused(*a, **k):
    return "", "", False


async def _raised(*a, **k):
    raise RuntimeError("registration refused")


async def _registered(*a, on_registered=None, **k):
    # The real _run_heavy prepares before it calls back; the spec has node_set.
    common = sys.modules["_plugin_common"]
    spec = {"components": {"schemas": {"QAEntry": {"properties": {"node_set": {}}}}}}
    common._json_http_request = lambda *a, **k: spec
    importlib.import_module("_project_memory").prepare("primary", "sid")
    if on_registered:
        on_registered()
    return "", "", True


class _Step1Failed(Exception):
    pass


def _singleflight_raises():
    raise _Step1Failed("lock dir unwritable")


@pytest.mark.parametrize(
    "healthy, heavy, singleflight, expect_retry_at",
    [
        # The server never came up, registration was refused or raised, or the
        # boot step itself raised: the bootstrap gave up, so a later hook
        # prepares (allow_retry set a retry_at).
        (False, None, None, True),
        (True, _refused, None, True),
        (True, _raised, None, True),
        (False, None, _singleflight_raises, True),
        # Registered: prepare() ran, so the state is no longer pending.
        (True, _registered, None, False),
    ],
    ids=["never-came-up", "refused", "raised", "boot-step-raised", "registered"],
)
def test_a_bootstrap_hands_an_unprepared_session_to_route(
    bootstrap, healthy, heavy, singleflight, expect_retry_at
):
    state = bootstrap(healthy=healthy, heavy=heavy, singleflight=singleflight)
    assert state["pending"] is expect_retry_at
    assert ("retry_at" in state) is expect_retry_at


# --- the SessionStart hook ----------------------------------------------------------------


@pytest.fixture
def start(suite, hook_module, monkeypatch, tmp_path, closed_port_url):
    """Run ``_start`` against a local URL; returns the project memory state."""
    require_coding(suite)
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    common = sys.modules["_plugin_common"]
    monkeypatch.setattr(common, "_PLUGIN_DIR", tmp_path)
    monkeypatch.setattr(common, "resolved_http_endpoint_auth", lambda: ("https://tenant", "key"))
    monkeypatch.setenv("COGNEE_PROJECT_NODE_SET", "project-fixed")
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.delenv("COGNEE_MANAGED_ENDPOINT", raising=False)
    monkeypatch.setattr(module, "_spawn_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(module, "_spawn_exit_watcher", lambda *a, **k: None)
    # The suite sets COGNEE_LAZY_BOOTSTRAP=0; these tests choose the path, and
    # never boot or install anything.
    monkeypatch.setattr(module, "_LAZY_BOOTSTRAP", True)
    monkeypatch.setattr(module, "_spawn_bootstrap", lambda *a, **k: pytest.fail("spawned"))

    async def _no_heavy(*a, **k):
        pytest.fail("the heavy path ran")

    monkeypatch.setattr(module, "_run_heavy", _no_heavy)

    def run(*, live: bool, heavy=None, spawned=None, lazy=True) -> dict:
        monkeypatch.setattr(module, "_LAZY_BOOTSTRAP", lazy)
        presence = module.PRESENCE_READY if live else module.PRESENCE_ABSENT
        monkeypatch.setattr(module, "server_presence", lambda *a, **k: (presence, {}))
        if heavy is not None:
            monkeypatch.setattr(module, "_run_heavy", heavy)
        if spawned is not None:
            monkeypatch.setattr(module, "_spawn_bootstrap", lambda *a, **k: spawned)
        try:
            asyncio.run(module._start({"session_id": "sid", "source": "startup"}))
        except RuntimeError:
            pass  # an exception after begin(): main() logs it
        (state_file,) = (tmp_path / "project-memory").glob("*.json")
        return json.loads(state_file.read_text())

    return run


def test_an_inline_registration_that_raised_is_handed_to_route(start):
    state = start(live=True, heavy=_raised)
    assert state["pending"] and "retry_at" in state


def test_an_inline_refused_registration_is_handed_to_route(start):
    # Without the lazy bootstrap no worker retries it (with it, one is spawned).
    state = start(live=True, heavy=_refused, lazy=False)
    assert state["pending"] and "retry_at" in state


def test_a_managed_endpoint_down_is_handed_to_route(start, monkeypatch):
    # Memory is OFFLINE for the session; when the deployment comes back, a
    # later route() prepares instead of holding capture for good.
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    state = start(live=False)
    assert state["pending"] and "retry_at" in state


@pytest.mark.parametrize("spawned", [True, False], ids=["spawned", "spawn-failed"])
def test_a_lazy_bootstrap_owns_the_session_only_once_spawned(start, spawned):
    state = start(live=False, spawned=spawned)
    # Spawned: the worker prepares (or gives up) itself, so the hook leaves the
    # state alone. Not spawned: nobody else will, so the hook hands it to route().
    assert state["pending"]
    assert ("retry_at" in state) is (not spawned)


def test_a_session_handed_to_the_cloud_worker_is_left_to_it(suite, start, monkeypatch):
    # The cloud worker retries registration and prepares on success: a hook
    # preparing ahead of it could bind the session to a key not minted yet.
    if suite.name != "claude-code":
        pytest.skip("only Claude Code has the cloud worker")
    worker = importlib.import_module("_cloud_worker")
    registered: list = []
    monkeypatch.setattr(worker, "register_session", lambda item: registered.append(item))
    monkeypatch.setattr(worker, "enabled", lambda *a, **k: True)
    state = start(live=True)
    assert registered and state["pending"] and "retry_at" not in state
