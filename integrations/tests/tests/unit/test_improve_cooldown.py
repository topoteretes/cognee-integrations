"""The per-session improve cooldown (``_plugin_common.improve_throttle_reason``).

Background: ``COGNEE_IMPROVE_COOLDOWN`` used to be a variable inside the idle
watcher process. That process exits after every bridge and is respawned on the
next prompt with the variable reset to zero, so the cooldown never gated
anything and an improve ran after essentially every prompt. The state now lives
on disk per session, written by the improve functions on a confirmed success,
and both automatic triggers consult it.

Contract:
  * a session that never improved is never throttled;
  * inside the cooldown the reason is ``cooldown``;
  * past the cooldown but with no new stored entries it is ``no_new_entries``;
  * a new entry (``bump_turn_counter``) clears it;
  * ``COGNEE_AUTO_IMPROVE_EVERY=0`` disables the every-N trigger;
  * ``store-to-session``'s background fire skips a throttled session and never
    reaches the server.
"""

from __future__ import annotations

import asyncio
import json

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    common = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(common, "hook_log", lambda *a, **k: None)
    return common


# ── state file ────────────────────────────────────────────────────────────────


def test_never_improved_session_is_not_throttled(pc):
    assert pc.read_improve_state("fresh") == {}
    assert pc.improve_throttle_reason("fresh") == ""


def test_record_writes_one_json_file_per_session(pc):
    pc.bump_turn_counter("sid")
    pc.bump_turn_counter("sid")
    pc.record_improve_success("sid", "ds", "idle")
    files = list(pc._IMPROVE_STATE_DIR.glob("*.json"))
    assert len(files) == 1
    state = json.loads(files[0].read_text(encoding="utf-8"))
    assert state["session_id"] == "sid"
    assert state["dataset"] == "ds"
    assert state["trigger"] == "idle"
    assert state["turn_count_at_improve"] == 2
    assert state["last_improved_at"] > 0
    assert pc.read_improve_state("sid") == state
    # A second session gets its own file; the first is untouched.
    pc.record_improve_success("other", "ds", "final")
    assert len(list(pc._IMPROVE_STATE_DIR.glob("*.json"))) == 2
    assert pc.read_improve_state("sid") == state


def test_empty_session_id_is_a_no_op(pc):
    pc.record_improve_success("", "ds", "idle")
    assert not pc._IMPROVE_STATE_DIR.exists() or not list(pc._IMPROVE_STATE_DIR.glob("*"))
    assert pc.improve_throttle_reason("") == ""


def test_corrupt_state_file_is_not_a_throttle(pc):
    path = pc._improve_state_path("sid")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    assert pc.read_improve_state("sid") == {}
    assert pc.improve_throttle_reason("sid") == ""


# ── the two gates ─────────────────────────────────────────────────────────────


def test_inside_cooldown_reason_is_cooldown(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    pc.record_improve_success("sid", "ds", "idle")
    pc.bump_turn_counter("sid")  # new work does not shorten the cooldown
    assert pc.improve_throttle_reason("sid") == "cooldown"


def test_past_cooldown_without_new_entries_is_no_new_entries(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "0")
    pc.bump_turn_counter("sid")
    pc.record_improve_success("sid", "ds", "auto")
    assert pc.improve_throttle_reason("sid") == "no_new_entries"


def test_past_cooldown_with_new_entry_is_allowed(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "0")
    pc.record_improve_success("sid", "ds", "auto")
    assert pc.improve_throttle_reason("sid") == "no_new_entries"
    pc.bump_turn_counter("sid")
    assert pc.improve_throttle_reason("sid") == ""


def test_cooldown_expiry_is_measured_from_the_recorded_timestamp(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    pc.record_improve_success("sid", "ds", "idle")
    pc.bump_turn_counter("sid")
    # Age the recorded improve past the window without touching the clock.
    path = pc._improve_state_path("sid")
    state = json.loads(path.read_text(encoding="utf-8"))
    state["last_improved_at"] -= 601
    path.write_text(json.dumps(state), encoding="utf-8")
    assert pc.improve_throttle_reason("sid") == ""


def test_a_new_success_restarts_the_window(pc, monkeypatch):
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "600")
    pc.record_improve_success("sid", "ds", "idle")
    pc.bump_turn_counter("sid")
    pc.record_improve_success("sid", "ds", "final")  # the final sync also counts
    assert pc.read_improve_state("sid")["trigger"] == "final"
    assert pc.improve_throttle_reason("sid") == "cooldown"


# ── knobs ─────────────────────────────────────────────────────────────────────


def test_cooldown_default_and_parsing(pc, monkeypatch):
    monkeypatch.delenv("COGNEE_IMPROVE_COOLDOWN", raising=False)
    assert pc.improve_cooldown_seconds() == 600.0
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "90")
    assert pc.improve_cooldown_seconds() == 90.0
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "-5")
    assert pc.improve_cooldown_seconds() == 0.0
    monkeypatch.setenv("COGNEE_IMPROVE_COOLDOWN", "soon")
    assert pc.improve_cooldown_seconds() == 600.0


def test_auto_improve_every_zero_disables(pc, monkeypatch):
    # The README always said "0 disables"; it used to fall back to 150 instead.
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "0")
    assert pc._auto_improve_threshold() == 0
    for _ in range(5):
        _count, should_improve = pc.bump_turn_counter("sid")
        assert should_improve is False
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "2")
    assert pc._auto_improve_threshold() == 2
    monkeypatch.setenv("COGNEE_AUTO_IMPROVE_EVERY", "lots")
    assert pc._auto_improve_threshold() == pc.AUTO_IMPROVE_EVERY_DEFAULT


# ── the auto trigger honours it ───────────────────────────────────────────────


@pytest.fixture
def store(suite, hook_module, monkeypatch):
    module = hook_module(suite, "store-to-session.py")
    monkeypatch.setattr(module, "notify", lambda *a, **k: None)
    return module


def test_auto_fire_skips_a_throttled_session(store, monkeypatch):
    events, improves = [], []
    monkeypatch.setattr(
        store, "hook_log", lambda ev, detail=None: events.append((ev, detail or {}))
    )
    monkeypatch.setattr(store, "improve_throttle_reason", lambda sid: "cooldown")
    monkeypatch.setattr(store, "http_api_ready", lambda: True)
    monkeypatch.setattr(
        store, "run_session_improve", lambda *a, **k: improves.append((a, k)) or True
    )

    asyncio.run(store._fire_improve_background("ds", "sid", None, reason="turn_150"))

    assert improves == []
    assert events == [
        ("auto_improve_throttled", {"reason": "turn_150", "session": "sid", "why": "cooldown"})
    ]


def test_auto_fire_runs_with_the_auto_trigger_when_not_throttled(store, monkeypatch):
    improves = []
    monkeypatch.setattr(store, "hook_log", lambda *a, **k: None)
    monkeypatch.setattr(store, "improve_throttle_reason", lambda sid: "")
    monkeypatch.setattr(store, "http_api_ready", lambda: True)
    monkeypatch.setattr(
        store, "run_session_improve", lambda *a, **k: improves.append((a, k)) or True
    )

    asyncio.run(store._fire_improve_background("ds", "sid", None, reason="turn_150"))

    assert improves == [(("ds", "sid"), {"trigger": "auto"})]
