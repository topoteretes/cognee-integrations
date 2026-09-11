"""A write the server never received is not a save (SDK-467).

While the backend was unreachable for two and a half weeks, every prompt's
recall header kept reading ``saved last turn 1 prompt / 6 trace / 1 answer``:
the store hooks diverted every trace and answer to the warmup buffer and bumped
the same save counter as a real write, so a durable outage produced no visible
signal at all. What these tests pin:

  * the counter keeps buffered writes apart from persisted ones, and the store
    hooks record every buffered branch (warming spillway, retryable failure)
    under the buffered kind — and only those;
  * the buffer stamps each entry with when it was buffered, strips the stamp
    before the entry reaches the server, and can report how many entries wait
    across every session on the machine and how long the oldest has waited;
  * every host's header shows buffered writes and the replay backlog as their
    own segments, and reads exactly as before when there is nothing to say;
  * a prompt whose recall is skipped because the server is known bad still gets
    a header — it names the outage and reads (and resets) the save counter, so
    the first header after recovery does not present weeks of buffered writes
    as one turn's saves.
"""

from __future__ import annotations

import asyncio
import os
import time
import urllib.error

import pytest
from utils.recall import HIT, URL, drive_recall, load_lookup

_SID = "sid"


# ── the counter ─────────────────────────────────────────────────────────────


@pytest.fixture
def pc(suite, isolated_modules, tmp_path, monkeypatch):
    """_plugin_common with the save counter, bridge dir and locks in a temp dir."""
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "_SAVE_COUNTER", tmp_path / "save_counter.json")
    monkeypatch.setattr(common, "_BRIDGE_DIR", tmp_path / "bridge")
    monkeypatch.setattr(common, "_bridge_file", lambda sid="": tmp_path / "bridge" / f"{sid}.json")
    monkeypatch.setattr(common, "_DRAIN_LOCK", tmp_path / "drain.lock")
    monkeypatch.setattr(common, "_BUFFER_LOCK", tmp_path / "buffer.lock")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


def test_buffered_bumps_land_under_their_own_kind(pc):
    pc.bump_save_counter(_SID, "prompt")
    pc.bump_save_counter(_SID, "trace")
    pc.bump_save_counter(_SID, "trace", buffered=True)
    pc.bump_save_counter(_SID, "trace", buffered=True)
    pc.bump_save_counter(_SID, "answer", buffered=True)
    pc.bump_save_counter(_SID, "prompt", buffered=True)  # prompts are never buffered: ignored

    assert pc.read_and_reset_save_counter(_SID) == {
        "prompt": 1,
        "trace": 1,
        "answer": 0,
        "trace_buffered": 2,
        "answer_buffered": 1,
    }
    # Reset zeroes every kind, buffered ones included.
    assert set(pc.read_and_reset_save_counter(_SID).values()) == {0}


# ── the store hook: which branch bumps what ─────────────────────────────────


@pytest.fixture
def store(suite, hook_module, monkeypatch):
    """The store hook with its write seams stubbed; returns ``(module, bumps)``."""
    module = hook_module(suite, "store-to-session.py")
    bumps: list[tuple] = []
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(module, "notify", lambda *a, **k: None)
    monkeypatch.setattr(module, "resolve_runtime_mode", lambda: {"mode": "http", "base_url": URL})
    monkeypatch.setattr(module, "_load_session", lambda: (_SID, "ds", "uid"))
    monkeypatch.setattr(module, "append_warmup_entry", lambda *a, **k: None)
    monkeypatch.setattr(module, "touch_activity", lambda: None)
    monkeypatch.setattr(module, "bump_turn_counter", lambda sid: (1, False))
    monkeypatch.setattr(module, "pop_pending_prompt", lambda sid, **k: {"prompt": "q"})
    monkeypatch.setattr(
        module,
        "bump_save_counter",
        lambda sid, kind, buffered=False: bumps.append((kind, buffered)),
    )
    return module, bumps


_TOOL = {"tool_name": "Read", "tool_input": {"file_path": "/x"}, "tool_output": "ok"}
_STOP = {"assistant_message": "done", "turn_id": "t1"}
_PATHS = [("_store_tool_call", _TOOL, "trace"), ("_store_assistant_stop", _STOP, "answer")]


def _raising(code: int):
    def _boom(*a, **k):
        raise urllib.error.HTTPError(URL, code, "boom", hdrs=None, fp=None)

    return _boom


@pytest.mark.parametrize("run, payload, kind", _PATHS)
def test_stored_write_bumps_the_plain_kind(store, monkeypatch, run, payload, kind):
    module, bumps = store
    monkeypatch.setattr(module, "server_usable", lambda url="": True)
    monkeypatch.setattr(module, "remember_entry_via_http", lambda *a, **k: {"entry_id": "e"})
    asyncio.run(getattr(module, run)(payload))
    assert bumps == [(kind, False)]


@pytest.mark.parametrize("run, payload, kind", _PATHS)
def test_warming_spillway_bumps_the_buffered_kind(store, monkeypatch, run, payload, kind):
    module, bumps = store
    monkeypatch.setattr(module, "server_usable", lambda url="": False)
    asyncio.run(getattr(module, run)(payload))
    assert bumps == [(kind, True)]


@pytest.mark.parametrize("run, payload, kind", _PATHS)
def test_retryable_failure_bumps_the_buffered_kind(store, monkeypatch, run, payload, kind):
    module, bumps = store
    monkeypatch.setattr(module, "server_usable", lambda url="": True)
    monkeypatch.setattr(module, "remember_entry_via_http", _raising(503))
    asyncio.run(getattr(module, run)(payload))
    assert bumps == [(kind, True)]


@pytest.mark.parametrize("run, payload, kind", _PATHS)
def test_dropped_4xx_bumps_nothing(store, monkeypatch, run, payload, kind):
    """A rejected write is neither saved nor buffered, so it must not count as either."""
    module, bumps = store
    monkeypatch.setattr(module, "server_usable", lambda url="": True)
    monkeypatch.setattr(module, "remember_entry_via_http", _raising(422))
    asyncio.run(getattr(module, run)(payload))
    assert bumps == []


# ── the buffer: stamp, strip, backlog ───────────────────────────────────────

_TRACE = {"type": "trace", "origin_function": "Bash", "status": "success"}


def _pending(pc, sid: str = _SID) -> list:
    cache = pc._load_json_file(pc._bridge_file(sid))
    return (cache.get(pc._bridge_cache_key("ds", sid)) or {}).get("pending_entries") or []


def test_buffered_entry_is_stamped_with_when_it_was_buffered(pc):
    before = time.time()
    pc.append_warmup_entry("ds", _SID, dict(_TRACE))
    (entry,) = _pending(pc)
    assert before <= entry[pc._BUFFERED_AT_KEY] <= time.time()
    # The caller's dict is left alone.
    assert pc._BUFFERED_AT_KEY not in _TRACE


def test_stamp_never_reaches_the_server(pc, monkeypatch):
    pc.append_warmup_entry("ds", _SID, dict(_TRACE))
    pc.append_warmup_entry("ds", _SID, dict(_TRACE), ambiguous=True)
    monkeypatch.setattr(
        pc, "get_session_detail_via_http", lambda sid, **k: {"traces": [], "qas": []}
    )
    sent: list = []
    monkeypatch.setattr(pc, "remember_entry_via_http", lambda d, s, e, **k: sent.append(e) or {})

    assert pc.drain_warmup_entries("ds", _SID) == (2, 0)
    assert sent == [_TRACE, _TRACE], "buffer bookkeeping must be stripped before the send"


def test_backlog_is_empty_without_a_bridge_dir(pc):
    assert pc.warmup_backlog() == {"pending": 0, "oldest_age_seconds": None}


def test_backlog_counts_every_session_and_reports_the_oldest_entry(pc, monkeypatch):
    now = 1_800_000_000.0
    monkeypatch.setattr(pc.time, "time", lambda: now - 20 * 86400)
    pc.append_warmup_entry("ds", "old-session", dict(_TRACE))
    monkeypatch.setattr(pc.time, "time", lambda: now - 90)
    pc.append_warmup_entry("ds", _SID, dict(_TRACE))
    pc.append_warmup_entry("ds", _SID, {"type": "qa", "question": "q", "answer": "a"})
    monkeypatch.setattr(pc.time, "time", lambda: now)

    backlog = pc.warmup_backlog()
    assert backlog["pending"] == 3
    assert backlog["oldest_age_seconds"] == pytest.approx(20 * 86400)


def test_backlog_skips_drained_sessions_and_unreadable_files(pc):
    pc.append_warmup_entry("ds", _SID, dict(_TRACE))
    drained = pc._bridge_file("drained")
    pc._write_json_file(drained, {pc._bridge_cache_key("ds", "drained"): {"pending_entries": []}})
    (pc._BRIDGE_DIR / "corrupt.json").write_text("{not json", encoding="utf-8")

    assert pc.warmup_backlog()["pending"] == 1


def test_unstamped_legacy_entry_takes_the_file_mtime(pc):
    """Entries buffered before the stamp existed still get an age — a lower bound."""
    path = pc._bridge_file(_SID)
    pc._write_json_file(
        path, {pc._bridge_cache_key("ds", _SID): {"pending_entries": [dict(_TRACE)]}}
    )
    week_ago = time.time() - 7 * 86400
    os.utime(path, (week_ago, week_ago))

    backlog = pc.warmup_backlog()
    assert backlog["pending"] == 1
    assert backlog["oldest_age_seconds"] == pytest.approx(7 * 86400, abs=5)


def test_format_age_is_coarse(pc):
    assert [pc.format_age(s) for s in (0, 45, 61, 3599, 3600, 86399, 86400, 20 * 86400)] == [
        "0s",
        "45s",
        "1m",
        "59m",
        "1h",
        "23h",
        "1d",
        "20d",
    ]


def test_segments_are_silent_when_healthy(pc):
    saves = {"prompt": 1, "trace": 3, "answer": 1, "trace_buffered": 0, "answer_buffered": 0}
    assert pc.buffered_saves_segments(saves, None) == []
    assert pc.buffered_saves_segments(saves, {"pending": 0, "oldest_age_seconds": None}) == []
    # A stub that predates the buffered kinds is fine too.
    assert pc.buffered_saves_segments({"prompt": 0, "trace": 0, "answer": 0}) == []


def test_segments_name_buffered_writes_and_the_backlog(pc):
    saves = {"prompt": 1, "trace": 0, "answer": 0, "trace_buffered": 6, "answer_buffered": 1}
    backlog = {"pending": 7, "oldest_age_seconds": 20 * 86400}
    assert pc.buffered_saves_segments(saves, backlog) == [
        "buffered last turn 6 trace / 1 answer (not saved yet)",
        "7 awaiting replay, oldest 20d",
    ]
    # Either half stands on its own.
    assert pc.buffered_saves_segments(saves, None) == [
        "buffered last turn 6 trace / 1 answer (not saved yet)"
    ]
    assert pc.buffered_saves_segments({"trace_buffered": 0}, {"pending": 2}) == [
        "2 awaiting replay"
    ]


def test_outage_header_names_the_failure_and_uses_the_hosts_joiner(pc):
    saves = {"prompt": 1, "trace": 0, "answer": 0, "trace_buffered": 6, "answer_buffered": 1}
    backlog = {"pending": 7, "oldest_age_seconds": 20 * 86400}
    assert pc.outage_header("unreachable", saves, backlog, "; ") == (
        "Cognee memory: recall skipped (server unreachable)"
        "; saved last turn 1 prompt / 0 trace / 0 answer"
        "; buffered last turn 6 trace / 1 answer (not saved yet)"
        "; 7 awaiting replay, oldest 20d"
    )
    assert pc.outage_header("server_error", {}, None, " · ") == (
        "Cognee memory: recall skipped (server error)"
        " · saved last turn 0 prompt / 0 trace / 0 answer"
    )
    assert [pc.describe_connection_failure(s) for s in pc.DEFINITIVE_FAILURE_STATES] == [
        "auth failed",
        "server unreachable",
        "server error",
    ]
    assert pc.describe_connection_failure("not_responding") == "server not responding"
    assert pc.describe_connection_failure("") == "server unknown"


# ── the headers, per host ───────────────────────────────────────────────────

_OUTAGE_SAVES = {
    "prompt": 1,
    "trace": 0,
    "answer": 0,
    "trace_buffered": 6,
    "answer_buffered": 1,
}
_BACKLOG = {"pending": 7, "oldest_age_seconds": 20 * 86400}


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    module = load_lookup(suite, hook_module, monkeypatch)
    monkeypatch.setattr(module, "warmup_backlog", lambda: dict(_BACKLOG))
    return module


def _joiner(suite) -> str:
    return "; " if suite.name == "claude-code" else " · "


def _message(suite, output: dict) -> str:
    """The hook's terminal message, wherever this host carries it."""
    if suite.name == "claude-code":
        return output["hookSpecificOutput"]["systemMessage"]
    return output["systemMessage"]


def _header(suite, output: dict) -> str:
    """The ``Cognee memory: …`` line of the terminal message."""
    message = _message(suite, output)
    return next(line for line in message.splitlines() if line.startswith("Cognee memory:"))


def test_header_shows_buffered_writes_and_the_backlog(suite, lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, recall=HIT, saves_last_turn=_OUTAGE_SAVES)
    header = _header(suite, run.output)
    j = _joiner(suite)
    assert header.endswith(
        f"{j}saved last turn 1 prompt / 0 trace / 0 answer"
        f"{j}buffered last turn 6 trace / 1 answer (not saved yet)"
        f"{j}7 awaiting replay, oldest 20d"
    )
    # The buffered writes are NOT folded into the saved count.
    assert "6 trace / 1 answer" not in header.split("buffered")[0]


def test_header_reads_as_before_when_nothing_was_buffered(suite, lookup, monkeypatch):
    monkeypatch.setattr(
        lookup, "warmup_backlog", lambda: {"pending": 0, "oldest_age_seconds": None}
    )
    run = drive_recall(lookup, monkeypatch, recall=HIT)
    header = _header(suite, run.output)
    assert header.endswith("saved last turn 0 prompt / 0 trace / 0 answer")
    assert "buffered" not in header and "awaiting replay" not in header


def test_skipped_recall_still_reports_the_outage(suite, lookup, monkeypatch):
    """A known-bad server used to mean no header at all — and an unread counter."""
    monkeypatch.setattr(lookup, "authed_liveness", lambda url, timeout=1.0: "unreachable")
    monkeypatch.setattr(lookup, "probe_health", lambda url, timeout=1.0: "down")
    run = drive_recall(
        lookup,
        monkeypatch,
        recall=HIT,
        prior_state={"state": "unreachable", "base_url": URL},
        saves_last_turn=_OUTAGE_SAVES,
    )
    assert run.fired("recall_skipped_not_ready")
    assert run.calls == [], "no scope was requested against a known-bad server"

    j = _joiner(suite)
    assert _header(suite, run.output) == (
        f"Cognee memory: recall skipped (server unreachable)"
        f"{j}saved last turn 1 prompt / 0 trace / 0 answer"
        f"{j}buffered last turn 6 trace / 1 answer (not saved yet)"
        f"{j}7 awaiting replay, oldest 20d"
    )
    # The model sees exactly what the terminal shows.
    assert run.output["hookSpecificOutput"]["additionalContext"] == _message(suite, run.output)
    assert run.output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    # Codex-derived hosts keep their plain status line on top.
    if suite.name != "claude-code":
        assert run.output["systemMessage"].startswith("cognee: ds · local\n")


def test_inconclusive_probe_names_the_recorded_failure(suite, lookup, monkeypatch):
    monkeypatch.setattr(lookup, "authed_liveness", lambda url, timeout=1.0: "unknown")
    monkeypatch.setattr(lookup, "probe_health", lambda url, timeout=1.0: "slow")
    run = drive_recall(
        lookup,
        monkeypatch,
        recall=HIT,
        prior_state={"state": "server_error", "base_url": URL},
    )
    assert _header(suite, run.output).startswith("Cognee memory: recall skipped (server error)")
