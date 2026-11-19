"""A dataset switch starts the sync of the session it leaves and never waits.

It used to run ``sync-session-to-graph.py --strict`` and block until the bridge
finished. In cloud mode that meant waiting for the cloud worker to reach the
request behind other sessions' queued improves, for many minutes, while the band
read "switching dataset…". Now the cloud request is only filed (as ``switch``),
and locally the sync runs detached.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def switch(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code dataset switch")
    module = hook_module(suite, "switch-dataset.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module.subprocess, "run", _forbidden("subprocess.run"))
    return module


def _forbidden(name):
    def call(*a, **k):
        raise AssertionError(f"the switch must not wait on {name}")

    return call


def test_cloud_files_a_switch_request_and_returns(switch, monkeypatch):
    worker = importlib.import_module("_cloud_worker")
    filed: list = []
    monkeypatch.setattr(worker, "enabled", lambda *_: True)
    monkeypatch.setattr(worker, "request_improve", lambda *a: filed.append(a))
    monkeypatch.setattr(switch.subprocess, "Popen", _forbidden("subprocess.Popen"))
    assert switch._sync_current("host-1", "sess-1", "agent_sessions") == ""
    assert filed == [("agent_sessions", "sess-1", "switch")]


def test_local_starts_a_detached_sync_of_the_retiring_pair(switch, monkeypatch):
    worker = importlib.import_module("_cloud_worker")
    monkeypatch.setattr(worker, "enabled", lambda *_: False)
    seen: dict = {}

    class Proc:
        pid = 4242

    def popen(argv, **kwargs):
        seen.update(argv=argv, **kwargs)
        return Proc()

    monkeypatch.setattr(switch.subprocess, "Popen", popen)
    assert switch._sync_current("host-1", "sess-1", "agent_sessions") == ""
    assert seen["argv"][1].endswith("sync-session-to-graph.py") and len(seen["argv"]) == 2
    assert seen["start_new_session"] is True
    env = seen["env"]
    assert env["COGNEE_SESSION_KEY"] == "host-1"
    assert env["COGNEE_SYNC_SESSION_ID"] == "sess-1"
    assert env["COGNEE_SYNC_DATASET"] == "agent_sessions"


def test_a_sync_that_cannot_start_is_reported_not_raised(switch, monkeypatch):
    worker = importlib.import_module("_cloud_worker")

    def busy(*_):
        raise OSError("improve request busy")

    monkeypatch.setattr(worker, "enabled", lambda *_: True)
    monkeypatch.setattr(worker, "request_improve", busy)
    assert switch._sync_current("host-1", "sess-1", "agent_sessions") == "improve request busy"
