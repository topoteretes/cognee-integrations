"""The warmup buffer replays over real HTTP, in order.

Entries buffered while the server was warming are replayed as
POST /api/v1/remember/entry once it is ready. The claim worth proving on the
wire is the one the server-side session cache depends on: each entry arrives as
a real request body, in the order it was captured, under the right dataset and
session id. A 503 from the server must arm the local backoff rather than
grinding the session forever.

The lock/budget/mutex bookkeeping around this is covered in
unit/test_warmup_drain.py.
"""

from __future__ import annotations

import pytest

ENTRY = "/api/v1/remember/entry"


@pytest.fixture
def pc(suite, isolated_modules, mock_server, tmp_path, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setenv("COGNEE_BASE_URL", mock_server.url)
    monkeypatch.setenv("COGNEE_API_KEY", "principal-key")
    monkeypatch.setattr(common, "_bridge_file", lambda sid="": tmp_path / "bridge.json")
    monkeypatch.setattr(common, "_DRAIN_LOCK", tmp_path / "drain.lock")
    monkeypatch.setattr(common, "_BUFFER_LOCK", tmp_path / "buffer.lock")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def test_buffered_entries_reach_the_server_in_order(pc, mock_server):
    pc.append_warmup_entry("agent_sessions", "sid-1", {"type": "trace", "origin_function": "Bash"})
    pc.append_warmup_entry("agent_sessions", "sid-1", {"type": "trace", "origin_function": "Read"})
    pc.append_warmup_entry(
        "agent_sessions", "sid-1", {"type": "qa", "question": "q", "answer": "a"}
    )

    assert pc.drain_warmup_entries("agent_sessions", "sid-1") == (3, 0)

    posted = [c for c in mock_server.calls if c["path"] == ENTRY]
    assert len(posted) == 3
    # Order is the capture order, and every request carries the scoping keys.
    kinds = [
        c["json"]["entry"].get("origin_function") or c["json"]["entry"]["type"] for c in posted
    ]
    assert kinds == ["Bash", "Read", "qa"]
    for call in posted:
        assert call["json"]["dataset_name"] == "agent_sessions"
        assert call["json"]["session_id"] == "sid-1"
        assert call["headers"].get("X-Api-Key") == "principal-key"


def test_server_503_leaves_the_entry_buffered_and_arms_backoff(pc, mock_server):
    pc.append_warmup_entry("agent_sessions", "sid-1", {"type": "trace", "origin_function": "Bash"})
    mock_server.force_response("POST", ENTRY, 503, {"detail": "busy"})

    assert pc.drain_warmup_entries("agent_sessions", "sid-1") == (0, 1)
    mock_server.assert_called("POST", ENTRY)

    state = pc._load_json_file(pc._bridge_file("sid-1")).get(
        pc._bridge_cache_key("agent_sessions", "sid-1"), {}
    )
    assert int(state.get("drain_fail_count") or 0) == 1
    assert state.get("pending_entries")  # nothing lost


def test_absent_server_leaves_the_entry_buffered(pc, monkeypatch, closed_port_url):
    pc.append_warmup_entry("agent_sessions", "sid-1", {"type": "trace", "origin_function": "Bash"})
    monkeypatch.setenv("COGNEE_BASE_URL", closed_port_url)
    assert pc.drain_warmup_entries("agent_sessions", "sid-1") == (0, 1)
    state = pc._load_json_file(pc._bridge_file("sid-1")).get(
        pc._bridge_cache_key("agent_sessions", "sid-1"), {}
    )
    assert state.get("pending_entries")  # nothing lost
