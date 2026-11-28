"""The stranded-queue worker as real processes against the mock server.

``sync-session-to-graph.py --deliver-stranded`` is what SessionStart starts in
the background. Here it runs to completion in a temp HOME holding the state an
ended session leaves (a launch record and a replay buffer with queued entries),
and the mock server's request log is the evidence: the entries arrive in order,
the improve follows the last one, once. Unit-level behaviour is in
tests/unit/test_stranded_queue_delivery.py.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from utils.hooklog import hook_events

KEY = "0b2f6c1e-ended-session"
SESSION_ID = f"claude_{KEY}"
DATASET = "agent_sessions"
ENTRY = "/api/v1/remember/entry"
IMPROVE = "/api/v1/improve"


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


@pytest.fixture
def ended_session(suite, isolated_modules, monkeypatch):
    """Write an ended session's launch record and replay buffer; returns the module."""
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: queue delivery after the session is Claude Code only")
    common = isolated_modules(suite, "_plugin_common")

    def _write(server_url: str) -> None:
        common._write_map_record(
            KEY,
            {
                "session_id": SESSION_ID,
                "dataset": DATASET,
                "host_key": KEY,
                "host_pid": _dead_pid(),
                "base_url": server_url,
            },
        )
        monkeypatch.setenv("COGNEE_SESSION_KEY", KEY)
        for n in range(2):
            common.append_warmup_entry(
                DATASET,
                SESSION_ID,
                {
                    "type": "trace",
                    "origin_function": "Read",
                    "status": "success",
                    "method_params": {"file_path": f"inventory/file{n}.py"},
                    "method_return_value": f"contents {n}",
                },
            )
        common.append_warmup_entry(
            DATASET,
            SESSION_ID,
            {
                "type": "qa",
                "question": "why is the total wrong?",
                "answer": "fixed",
                "context": "{}",
            },
        )
        common.bump_save_counter(SESSION_ID, "answer", buffered=True)
        monkeypatch.delenv("COGNEE_SESSION_KEY")

    common.write_ended_session = _write
    return common


def _writes(mock_server) -> list[str]:
    kinds = []
    for call in mock_server.calls:
        if call["path"] == ENTRY:
            kinds.append(
                str((call.get("json") or {}).get("entry", call.get("json") or {}).get("type"))
            )
        elif call["path"] == IMPROVE:
            kinds.append("improve")
    return kinds


def test_an_ended_sessions_queue_is_delivered_and_then_improved_once(
    suite, ended_session, run_hook, mock_server, temp_home
):
    ended_session.write_ended_session(mock_server.url)

    result = run_hook(
        suite,
        "sync-session-to-graph.py",
        "--deliver-stranded",
        service_url=mock_server.url,
        env={"COGNEE_SESSION_KEY": "the-new-session"},
        timeout=90.0,
    )

    assert result.returncode == 0, result.stderr
    assert _writes(mock_server) == ["trace", "trace", "qa", "improve"]
    improve = [call for call in mock_server.calls if call["path"] == IMPROVE][0]["json"]
    assert improve["session_ids"] == [SESSION_ID]
    assert ended_session.warmup_backlog()["pending"] == 0
    events = hook_events(suite, temp_home)
    fired = [detail for event, detail in events if event == "improve_fired"]
    assert [(f["session"], f["trigger"], f["ok"]) for f in fired] == [(SESSION_ID, "replay", True)]
    assert not [event for event, _ in events if event == "delivery_gave_up"]

    # A second session start finds nothing left: no request at all.
    seen = len(mock_server.calls)
    run_hook(
        suite,
        "sync-session-to-graph.py",
        "--deliver-stranded",
        service_url=mock_server.url,
        env={"COGNEE_SESSION_KEY": "another-new-session"},
    )
    assert len(mock_server.calls) == seen


def test_an_unreachable_server_leaves_the_queue_and_says_so(
    suite, ended_session, run_hook, closed_port_url, temp_home
):
    ended_session.write_ended_session(closed_port_url)

    # The worker SessionStart starts: it probes, logs and stops.
    run_hook(
        suite,
        "sync-session-to-graph.py",
        "--deliver-stranded",
        service_url=closed_port_url,
        env={"COGNEE_SESSION_KEY": "the-new-session"},
    )
    # The session's own end, with the server gone: every attempt fails.
    started = time.monotonic()
    run_hook(
        suite,
        "sync-session-to-graph.py",
        "--detached-final",
        service_url=closed_port_url,
        env={
            "COGNEE_SESSION_KEY": KEY,
            "COGNEE_SYNC_START_DELAY": "0",
            "COGNEE_SYNC_RETRY_DELAY": "0",
        },
        timeout=90.0,
    )
    assert time.monotonic() - started < 60

    assert ended_session.warmup_backlog()["pending"] == 3
    events = hook_events(suite, temp_home)
    stopped = [detail for event, detail in events if event == "stranded_delivery_stopped"]
    assert stopped == [{"sessions": 1, "left": 3, "reason": "unreachable"}]
    gave_up = [detail for event, detail in events if event == "delivery_gave_up"]
    assert gave_up == [{"session": KEY, "left": 3, "reason": "unreachable"}]
    assert not [event for event, _ in events if event == "warmup_drained"]
