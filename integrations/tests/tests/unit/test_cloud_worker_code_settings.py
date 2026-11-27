"""The cloud worker runs each session's startup code-graph pass by its settings.

The worker is shared by every terminal and outlives edits of ~/.cognee/.env,
so its own environment holds whatever COGNEE_CODE_AUTOINDEX and
COGNEE_CODE_SERVER_SHARES_FILES were when it started. Each session's
SessionStart records the values it read, and the worker hands those to the
pass, so removing ``always`` takes effect from the next session.
"""

from __future__ import annotations

import importlib
import json
import sys
import time
import types

import pytest

TARGET = {"base_url": "https://tenant.cognee.example", "principal": "p"}


@pytest.fixture
def worker(suite, hook_module, monkeypatch, tmp_path):
    if suite.name != "claude-code":
        pytest.skip("only Claude Code has the cloud worker")
    hook_module(suite, "session-start.py")  # puts the suite's scripts on sys.path
    module = importlib.import_module("_cloud_worker")
    monkeypatch.setattr(module, "root", lambda: tmp_path / "worker")
    monkeypatch.setattr(module, "target", lambda: dict(TARGET))
    common = sys.modules["_plugin_common"]
    monkeypatch.setattr(common, "ensure_dataset_via_http", lambda *a, **k: None)
    monkeypatch.setattr(common, "set_session_key", lambda *a, **k: None)
    return module


def _register(worker, tmp_path, **extra):
    path = tmp_path / "worker" / "sessions" / "key-1" / "worker.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "target": dict(TARGET),
        "session_key": "key-1",
        "session_id": "session-1",
        "dataset": "agent_sessions",
        "agent_session_name": "session-1",
        "cwd": str(tmp_path),
        "created_at": time.time(),
        **extra,
    }
    path.write_text(json.dumps(record), encoding="utf-8")


def _run(worker, monkeypatch) -> dict:
    """Run the worker's bootstrap loop with the heavy session work faked."""
    seen: dict = {}

    async def run_heavy(*args, **kwargs):
        seen.update(kwargs)
        return "", "", True

    def exec_module(module):
        module._run_heavy = run_heavy

    spec = types.SimpleNamespace(loader=types.SimpleNamespace(exec_module=exec_module))
    monkeypatch.setattr(importlib.util, "spec_from_file_location", lambda *a, **k: spec)
    monkeypatch.setattr(importlib.util, "module_from_spec", lambda s: types.SimpleNamespace())
    worker._bootstrap_sessions()
    return seen


def test_the_worker_hands_the_session_its_own_settings(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")  # what the worker started with
    settings = {"COGNEE_CODE_AUTOINDEX": "ask", "COGNEE_CODE_SERVER_SHARES_FILES": ""}
    _register(worker, tmp_path, code_settings=settings)
    assert _run(worker, monkeypatch)["code_settings"] == settings


@pytest.mark.parametrize(
    ("retried", "given", "handed"),
    [
        ({}, "always", "always"),
        ({"attempts": 1}, "always", "ask"),
        ({"next_attempt": 1.0}, "always", "ask"),
        ({"attempts": 3}, "off", "off"),
        ({"attempts": 3}, "", ""),
    ],
)
def test_a_deferred_pass_indexes_nothing_new(worker, retried, given, handed):
    """The settings are a snapshot of the session's start. Once its registration
    has had to be retried, a standing yes may have been withdrawn since."""
    record = {"code_settings": {"COGNEE_CODE_AUTOINDEX": given}, **retried}
    assert worker.record_code_settings(record)["COGNEE_CODE_AUTOINDEX"] == handed


def test_a_record_from_an_older_build_indexes_nothing_new(worker, tmp_path, monkeypatch):
    monkeypatch.setenv("COGNEE_CODE_AUTOINDEX", "always")
    _register(worker, tmp_path)
    assert _run(worker, monkeypatch)["code_settings"] == {"COGNEE_CODE_AUTOINDEX": "ask"}
