"""A dataset switch whose pre-switch sync times out fails cleanly.

``subprocess.TimeoutExpired`` used to escape ``_sync_current``: the switch was
reported as a crash instead of a sync failure, and ``--force`` (meant to push
past a failed sync) could not. Nothing has changed at that point, so the old
session simply stays the registered one.
"""

from __future__ import annotations

import subprocess

import pytest


@pytest.fixture
def switch(suite, hook_module, monkeypatch):
    module = hook_module(suite, "switch-dataset.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    return module


def _run_raising_timeout(seen: dict):
    def run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    return run


def test_sync_timeout_is_a_sync_failure(switch, monkeypatch):
    seen: dict = {}
    monkeypatch.setattr(switch.subprocess, "run", _run_raising_timeout(seen))
    monkeypatch.setenv("COGNEE_SWITCH_SYNC_TIMEOUT", "5")
    with pytest.raises(switch.SwitchError, match="timed out after 5s") as err:
        switch._sync_current("host-1", "sess-1", "agent_sessions")
    assert err.value.code == switch.EXIT_SYNC_FAILED
    assert seen["timeout"] == 5.0


@pytest.mark.parametrize("value", ["", "soon", "0", "-1", "inf"])
def test_sync_timeout_falls_back_to_the_default(switch, monkeypatch, value):
    seen: dict = {}
    monkeypatch.setattr(switch.subprocess, "run", _run_raising_timeout(seen))
    monkeypatch.setenv("COGNEE_SWITCH_SYNC_TIMEOUT", value)
    with pytest.raises(switch.SwitchError):
        switch._sync_current("host-1", "sess-1", "agent_sessions")
    assert seen["timeout"] == 900.0
