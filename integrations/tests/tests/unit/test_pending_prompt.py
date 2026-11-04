"""The pending-prompt buffer leaves nothing behind (SDK-469, part 1).

``remember_pending_prompt`` parks a prompt until the Stop hook pops it. The pop
used to write the emptied dict back as ``{}``, leaving one 2-byte husk per
session forever (80 of 88 files in one pending/ dir). Now the last pop removes
the file, a pop against nothing creates nothing, and the SessionStart sweep
clears husks older versions left — immediately, since an empty buffer has
nothing in flight.
"""

from __future__ import annotations

import pytest


@pytest.fixture
def pc(suite, isolated_modules, monkeypatch):
    module = isolated_modules(suite, "_plugin_common")
    monkeypatch.setattr(module, "hook_log", lambda *a, **k: None)
    monkeypatch.setenv("COGNEE_SESSION_KEY", "host-abc")
    return module


def test_last_pop_removes_the_file(pc):
    pc.remember_pending_prompt("s1", "what did we decide?", turn_id="t1")
    path = pc._pending_file("s1")
    assert path.exists()
    popped = pc.pop_pending_prompt("s1", turn_id="t1")
    assert popped["prompt"] == "what did we decide?"
    assert not path.exists(), "an emptied buffer must not stay behind as {}"


def test_pop_keeps_the_file_while_other_turns_are_pending(pc):
    pc.remember_pending_prompt("s1", "first", turn_id="t1")
    pc.remember_pending_prompt("s1", "second", turn_id="t2")
    path = pc._pending_file("s1")
    assert pc.pop_pending_prompt("s1", turn_id="t1")["prompt"] == "first"
    assert path.exists()
    assert pc.pop_pending_prompt("s1", turn_id="t2")["prompt"] == "second"
    assert not path.exists()


def test_pop_against_nothing_creates_nothing(pc):
    path = pc._pending_file("s1")
    assert pc.pop_pending_prompt("s1", turn_id="t9") == {"prompt": "", "context": ""}
    assert not path.exists()


def test_sweep_removes_husks_regardless_of_age(pc):
    pc._PENDING_DIR.mkdir(parents=True, exist_ok=True)
    husk = pc._PENDING_DIR / "old-session.json"
    husk.write_text("{}", encoding="utf-8")
    live = pc._PENDING_DIR / "live.json"
    live.write_text('{"host:t1": {"prompt": "x"}}', encoding="utf-8")
    counts = pc.sweep_stale_state()
    assert counts.get("pending_husks") == 1
    assert not husk.exists() and live.exists()
