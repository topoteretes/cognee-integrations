"""Coding memory (SDK-1336) at SessionStart and the shared local server's mode.

A shared local server keeps the distillation mode it booted with; a session that
joins it learns that mode from the server pidfile and says so when it differs
from its own setting (it never restarts the shared server). In coding mode,
SessionStart keeps what other parts put in the model's context (an OFFLINE
instruction, the sync notice) and appends the lessons block after a compaction.
"""

from __future__ import annotations

import asyncio
import importlib
import io
import json
import os
import subprocess
import sys

import pytest

CODING_SUITES = {"claude-code", "codex"}


@pytest.fixture
def session_start(suite, hook_module, monkeypatch):
    if suite.name not in CODING_SUITES:
        pytest.skip("coding memory ships for Claude Code and Codex only")
    module = hook_module(suite, "session-start.py")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_PRESENCE_REPROBE_DELAY", "0.05")
    return module


@pytest.fixture
def pc(session_start, monkeypatch):
    """The ``_plugin_common`` the session-start module is bound to."""
    module = sys.modules["_plugin_common"]
    # The test process stands in for the server: alive, and "looks like" it.
    monkeypatch.setattr(module, "_pid_looks_like_server", lambda pid: True)
    return module


@pytest.fixture
def cm(session_start, monkeypatch):
    """``_coding_memory`` with the server check and the background spawn faked."""
    module = importlib.import_module("_coding_memory")
    monkeypatch.setattr(module, "check_server_support", lambda **k: {"ok": True})
    spawned: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawned.append(a))
    module.spawned = spawned
    return module


def _port(url: str) -> int:
    return int(url.rsplit(":", 1)[1])


# --- the pidfile records the spawn's mode ------------------------------------------------


def _spawn(session_start, closed_port_url, monkeypatch) -> list:
    """Drive the boot point with uvicorn faked; return the pidfile write calls."""
    writes: list = []
    spawned: dict = {}

    class _FakeProc:
        pid = 4242

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        spawned["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(session_start, "ensure_cognee_installed", lambda *a, **k: True)
    monkeypatch.setattr(session_start.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(session_start, "_start_console_capture", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "_health_ok", lambda *a, **k: "env" in spawned)
    monkeypatch.setattr(
        session_start, "write_server_pidfile", lambda *a, **k: writes.append((a, k))
    )
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    session_start._ensure_local_server_running({"base_url": closed_port_url}, health_timeout=2.0)
    return writes


def test_spawn_records_coding_mode(session_start, closed_port_url, monkeypatch):
    monkeypatch.setenv("DISTILLATION_MODE", "coding")
    writes = _spawn(session_start, closed_port_url, monkeypatch)
    assert writes and writes[0][1]["coding_memory"] is True


def test_spawn_records_normal_mode(session_start, closed_port_url, monkeypatch):
    monkeypatch.delenv("DISTILLATION_MODE", raising=False)
    writes = _spawn(session_start, closed_port_url, monkeypatch)
    assert writes and writes[0][1]["coding_memory"] is False


def test_pidfile_keeps_the_field(pc):
    pc.write_server_pidfile(8123, os.getpid(), version="1.0", coding_memory=True)
    assert pc.live_server_record(8123)["coding_memory"] is True
    pc.write_server_pidfile(8124, os.getpid(), version="1.0")
    assert "coding_memory" not in pc.live_server_record(8124)


# --- the note when a session joins a running server ---------------------------------------


def _join(session_start, closed_port_url, monkeypatch, *, coding: bool) -> dict:
    """Run ``_start`` against an already-running local server (heavy path faked)."""
    if coding:
        monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    else:
        monkeypatch.delenv("COGNEE_CODING_MEMORY", raising=False)
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_READY, {})
    )

    async def fake_heavy(*a, **k):
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", fake_heavy)
    monkeypatch.setattr(session_start, "_spawn_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "_spawn_exit_watcher", lambda *a, **k: None)
    return asyncio.run(session_start._start({"session_id": "s1", "source": "startup"}))


def _messages(result: dict) -> str:
    return str(result.get("systemMessage") or "") + "\n" + json.dumps(result)


def test_coding_session_on_a_normal_server_is_told(session_start, pc, closed_port_url, monkeypatch):
    pid = os.getpid()
    pc.write_server_pidfile(_port(closed_port_url), pid, coding_memory=False)
    result = _join(session_start, closed_port_url, monkeypatch, coding=True)
    note = result["systemMessage"]
    assert f"(pid {pid}) was started without coding mode" in note
    assert f"`kill {pid}`" in note
    assert "close the other Claude Code and Codex sessions" in note
    assert result["hookSpecificOutput"]["systemMessage"].endswith(note.split("\n\n")[-1])


def test_normal_session_on_a_coding_server_is_told(session_start, pc, closed_port_url, monkeypatch):
    pid = os.getpid()
    pc.write_server_pidfile(_port(closed_port_url), pid, coding_memory=True)
    result = _join(session_start, closed_port_url, monkeypatch, coding=False)
    note = result["systemMessage"]
    assert f"(pid {pid}) was started in coding memory mode" in note
    assert f"`kill {pid}`" in note


@pytest.mark.parametrize("coding", [True, False])
def test_matching_modes_give_no_note(session_start, pc, closed_port_url, monkeypatch, coding):
    pc.write_server_pidfile(_port(closed_port_url), os.getpid(), coding_memory=coding)
    result = _join(session_start, closed_port_url, monkeypatch, coding=coding)
    messages = _messages(result)
    # Every mode note names how the server was started and how to restart it.
    assert "was started" not in messages and "`kill " not in messages


def test_unknown_server_mode_is_told_to_a_coding_session(
    session_start, pc, closed_port_url, monkeypatch
):
    # An older pidfile (no field) — or none at all: a server started elsewhere.
    pc.write_server_pidfile(_port(closed_port_url), os.getpid())
    result = _join(session_start, closed_port_url, monkeypatch, coding=True)
    assert "unknown whether it runs coding mode" in result["systemMessage"]


def test_no_pidfile_is_unknown_for_a_coding_session(
    session_start, pc, closed_port_url, monkeypatch
):
    result = _join(session_start, closed_port_url, monkeypatch, coding=True)
    assert "unknown whether it runs coding mode" in result["systemMessage"]


def test_unknown_server_mode_is_quiet_for_a_normal_session(
    session_start, pc, closed_port_url, monkeypatch
):
    pc.write_server_pidfile(_port(closed_port_url), os.getpid())
    result = _join(session_start, closed_port_url, monkeypatch, coding=False)
    assert "coding mode" not in _messages(result)


def test_no_pidfile_says_shut_down_and_restart(session_start, pc, closed_port_url, monkeypatch):
    # A live local server with no pidfile record was not started by the plugin:
    # it never stops on its own when the sessions close.
    result = _join(session_start, closed_port_url, monkeypatch, coding=True)
    note = result["systemMessage"]
    assert "wasn't started by the plugin" in note
    assert "Shut it down and restart the plugin" in note
    assert "stops on its own" not in note
    assert "COGNEE_BASE_URL" not in note


def test_older_pidfile_keeps_the_close_sessions_advice(
    session_start, pc, closed_port_url, monkeypatch
):
    pc.write_server_pidfile(_port(closed_port_url), os.getpid())
    note = _join(session_start, closed_port_url, monkeypatch, coding=True)["systemMessage"]
    assert "stops on its own" in note
    assert "wasn't started by the plugin" not in note


def test_spawned_normal_server_keeps_the_kill_advice(
    session_start, pc, closed_port_url, monkeypatch
):
    pid = os.getpid()
    pc.write_server_pidfile(_port(closed_port_url), pid, coding_memory=False)
    note = _join(session_start, closed_port_url, monkeypatch, coding=True)["systemMessage"]
    assert f"`kill {pid}`" in note
    assert "wasn't started by the plugin" not in note


def _join_remote(session_start, monkeypatch, url: str, *, coding: bool = True) -> dict:
    """Run ``_start`` against a live remote URL (heavy path and cloud worker faked)."""
    if coding:
        monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    else:
        monkeypatch.delenv("COGNEE_CODING_MEMORY", raising=False)
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    monkeypatch.setattr(session_start, "_health_ok", lambda *a, **k: True)

    async def fake_heavy(*a, **k):
        return "", "", True

    monkeypatch.setattr(session_start, "_run_heavy", fake_heavy)
    monkeypatch.setattr(session_start, "_spawn_idle_watcher", lambda *a, **k: None)
    monkeypatch.setattr(session_start, "_spawn_exit_watcher", lambda *a, **k: None)
    if hasattr(session_start, "server_ready_hint"):
        monkeypatch.setattr(session_start, "server_ready_hint", lambda *a, **k: True)
    try:
        cloud_worker = importlib.import_module("_cloud_worker")
        monkeypatch.setattr(cloud_worker, "register_session", lambda *a, **k: None)
    except ImportError:
        pass
    return asyncio.run(session_start._start({"session_id": "s1", "source": "startup"}))


def test_remote_url_in_coding_mode_names_the_server(session_start, pc, monkeypatch):
    url = "https://memory.example.com"
    note = _join_remote(session_start, monkeypatch, url)["systemMessage"]
    assert f"the Cognee server at {url} wasn't started by the plugin" in note
    assert "Shut it down and restart the plugin" in note
    assert "Remove COGNEE_BASE_URL" in note
    assert "reconnects to the same server" in note


def test_remote_url_in_normal_mode_gives_no_note(session_start, pc, monkeypatch):
    result = _join_remote(session_start, monkeypatch, "https://memory.example.com", coding=False)
    assert "coding mode" not in _messages(result)


def test_cognee_cloud_gives_no_note(session_start, pc, monkeypatch):
    result = _join_remote(session_start, monkeypatch, "https://tenant-abc.aws.cognee.ai")
    assert "coding mode" not in _messages(result)
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    assert (
        session_start._coding_server_note(
            "https://memory.example.com", True, managed=False, cloud_backend=True
        )
        == ""
    )


def test_managed_endpoint_names_the_setting(session_start, pc, closed_port_url, monkeypatch):
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    note = session_start._coding_server_note(closed_port_url, True, managed=True)
    assert f"the Cognee server at {closed_port_url} wasn't started by the plugin" in note
    assert "Remove COGNEE_MANAGED_ENDPOINT" in note
    assert "COGNEE_BASE_URL" not in note
    remote = session_start._coding_server_note("https://memory.example.com", True, managed=True)
    assert "Remove COGNEE_BASE_URL and COGNEE_MANAGED_ENDPOINT" in remote


def test_queued_project_memory_note_reaches_both_messages(
    session_start, pc, closed_port_url, monkeypatch
):
    # Showing it once is pending_notice's job (test_project_memory_retry); this
    # pins that session start puts its note in both output copies.
    pm = importlib.import_module("_project_memory")
    monkeypatch.setattr(pm, "pending_notice", lambda dataset, session_id: pm.PENDING_NOTICE)
    result = _join(session_start, closed_port_url, monkeypatch, coding=False)
    assert pm.PENDING_NOTICE in result["systemMessage"]
    assert pm.PENDING_NOTICE in result["hookSpecificOutput"]["systemMessage"]


def test_doctor_line_shows_the_server_mode(suite, session_start, pc, monkeypatch):
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setenv("COGNEE_LOCAL_API_URL", "http://localhost:8125")
    cm = importlib.import_module("_coding_memory")
    monkeypatch.setattr(cm, "check_server_support", lambda **k: {"ok": True})
    monkeypatch.setattr(cm, "delete_permission", lambda dataset: True)
    pid = os.getpid()
    pc.write_server_pidfile(8125, pid, coding_memory=False)
    assert f"local server (pid {pid}) not in coding mode" in cm.doctor_line("ds")
    pc.write_server_pidfile(8125, pid, coding_memory=True)
    assert f"local server (pid {pid}) in coding mode" in cm.doctor_line("ds")
    pc.write_server_pidfile(8125, pid)
    assert "coding mode unknown" in cm.doctor_line("ds")


# --- SessionStart output in coding mode ---------------------------------------------------


def _run_main(session_start, monkeypatch, capsys, payload: dict) -> dict:
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    session_start.main()
    out = capsys.readouterr().out.strip().splitlines()
    return json.loads(out[-1])


def _fake_start(session_start, monkeypatch, output: dict) -> None:
    async def fake(payload=None):
        return json.loads(json.dumps(output))

    monkeypatch.setattr(session_start, "_start", fake)


def test_coding_mode_keeps_the_offline_instruction(
    session_start, cm, closed_port_url, monkeypatch, capsys
):
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setenv("COGNEE_MANAGED_ENDPOINT", "true")
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    monkeypatch.setattr(
        session_start, "server_presence", lambda *a, **k: (session_start.PRESENCE_ABSENT, {})
    )
    result = _run_main(
        session_start, monkeypatch, capsys, {"session_id": "s1", "source": "startup"}
    )
    assert "Cognee Memory OFFLINE" in result["systemMessage"]
    assert "OFFLINE" in result["hookSpecificOutput"]["additionalContext"]


def test_coding_mode_keeps_the_sync_notice_on_resume(session_start, cm, monkeypatch, capsys):
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    _fake_start(session_start, monkeypatch, {})
    recall_text = importlib.import_module("_recall_text")
    monkeypatch.setattr(recall_text, "credit_enabled", lambda *a: True)
    monkeypatch.setattr(recall_text, "sync_notice", lambda state: ("SYNC NOTICE", 123.0))
    monkeypatch.setattr(recall_text, "notice_due", lambda *a: True)
    monkeypatch.setattr(recall_text, "mark_notice_shown", lambda *a: None)
    result = _run_main(session_start, monkeypatch, capsys, {"session_id": "s1", "source": "resume"})
    assert "SYNC NOTICE" in result["systemMessage"]
    assert "SYNC NOTICE" in result["hookSpecificOutput"]["additionalContext"]


def test_compaction_appends_the_lessons_block(suite, session_start, cm, monkeypatch):
    sys.modules["_plugin_common"].set_session_key("host-session-1")
    monkeypatch.setattr(cm, "reset_delivery", lambda key: None)
    monkeypatch.setattr(cm, "last_request", lambda key: "fix it")
    monkeypatch.setattr(cm, "deliver", lambda key, request, cwd: "LESSONS BLOCK")
    output = {
        "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "NOTICE"}
    }
    result = session_start._coding_session_start(output, {"source": "compact", "cwd": "/tmp"})
    context = result["hookSpecificOutput"]["additionalContext"]
    assert context.index("NOTICE") < context.index("LESSONS BLOCK")


def test_compaction_through_main_keeps_both(suite, session_start, cm, monkeypatch, capsys):
    if suite.name != "claude-code":
        pytest.skip("Codex answers a compaction with the lessons block alone")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    monkeypatch.setattr(cm, "reset_delivery", lambda key: None)
    monkeypatch.setattr(cm, "last_request", lambda key: "fix it")
    monkeypatch.setattr(cm, "deliver", lambda key, request, cwd: "LESSONS BLOCK")
    monkeypatch.setattr(session_start, "get_session_key", lambda: "host-session-1")
    _fake_start(
        session_start,
        monkeypatch,
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "NOTICE"}},
    )
    result = _run_main(
        session_start, monkeypatch, capsys, {"session_id": "s1", "source": "compact"}
    )
    context = result["hookSpecificOutput"]["additionalContext"]
    assert "NOTICE" in context and "LESSONS BLOCK" in context


def test_coding_session_is_recorded_without_a_recap(suite, session_start, cm, monkeypatch, capsys):
    if suite.name != "claude-code":
        pytest.skip("session stats are a Claude Code feature")
    monkeypatch.setenv("COGNEE_CODING_MEMORY", "true")
    _fake_start(session_start, monkeypatch, {})
    stats = importlib.import_module("_session_stats")
    updates: list = []
    monkeypatch.setattr(stats, "update", lambda key, **k: updates.append(key) or True)
    monkeypatch.setattr(stats, "summary", lambda previous: "LAST SESSION RECAP")
    result = _run_main(
        session_start, monkeypatch, capsys, {"session_id": "s1", "source": "startup"}
    )
    assert len(updates) == 1
    assert "LAST SESSION RECAP" not in json.dumps(result)
