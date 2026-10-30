"""Tests for `_activity_segment` (cognee_statusline_render.py) — the live
`· recalling…` / `· 3 memories used` segment at the end of the bar.

Contract:
  * the segment reads this session's ``phase/<session_id>.json`` marker, written
    by the hooks (`_plugin_common.phase_begin` / `phase_end`); no network;
  * an active phase renders in the accent colour, present tense (`recalling…`);
    a stale active phase (a hook that died) is ignored;
  * otherwise the most recent finished result renders in past tense with its
    number (`3 memories used`, `✓ memory saved`); zero recalled is phrased
    (`nothing relevant`), never counted;
  * a result a minute old renders faint, so "recent" and "left over" differ;
  * only this session's marker is ever read, and a session id from stdin never
    builds a path unchecked;
  * `COGNEE_STATUSLINE_ACTIVITY=false` hides the segment; `--json` exposes the
    same activity as data for the Claude Code mod.

claude-code only: codex's renderer is a short plain-text string for the model's
context. The bar-level placement is covered in e2e/test_statusline_bar.py.
"""

from __future__ import annotations

import time

import pytest
from utils.statusline import write_json

_SESSION = "fde122ae-07db-431d-b5af-acba353e4e3e"
_RESET = "\033[0m"
_DIM = "\033[2m"


@pytest.fixture
def sl(suite, statusline):
    if not hasattr(statusline, "_activity_segment"):
        pytest.skip(f"{suite.name}: no activity segment (the bar is plain and short)")
    return statusline


def _active(phase: str, detail: str = "", age: float = 0.0) -> dict:
    now = time.time() - age
    return {"phase": phase, "detail": detail, "started_at": now, "updated_at": now}


def _result(count=None, age: float = 0.0, ok: bool = True) -> dict:
    entry = {"at": time.time() - age, "ok": ok}
    if count is not None:
        entry["count"] = count
    return entry


def _marker(sl, session=_SESSION, active=None, results=None):
    write_json(
        sl._PHASE_DIR / f"{session}.json",
        {"session_key": session, "active": active, "results": results or {}},
    )


def test_a_result_older_than_five_minutes_leaves_the_line(sl):
    _marker(sl, results={"recalled": _result(count=3, age=sl._RESULT_HIDE_AFTER_SECONDS + 1)})
    assert sl._activity_segment(_SESSION) == ""
    # Just under the threshold it is still there, faint.
    _marker(sl, results={"recalled": _result(count=3, age=sl._RESULT_HIDE_AFTER_SECONDS - 5)})
    assert "3 memories used" in sl._activity_segment(_SESSION)


def _accent(sl, text: str) -> str:
    return f" · {sl._ACTIVE_STYLE}{text}{_RESET}"


# ── nothing to show ────────────────────────────────────────────────────────


def test_no_marker_renders_nothing(sl):
    assert sl._activity_segment(_SESSION) == ""


def test_malformed_marker_renders_nothing(sl):
    sl._PHASE_DIR.mkdir(parents=True, exist_ok=True)
    (sl._PHASE_DIR / f"{_SESSION}.json").write_text("not json{{{", encoding="utf-8")
    assert sl._activity_segment(_SESSION) == ""


def test_marker_with_no_activity_renders_nothing(sl):
    _marker(sl, active=None, results={})
    assert sl._activity_segment(_SESSION) == ""


def test_no_session_id_renders_nothing(sl):
    _marker(sl, results={"recalled": _result(3)})
    assert sl._activity_segment("") == ""


# ── active phases ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phase, text",
    [
        ("connecting", "connecting…"),
        ("recalling", "recalling…"),
        ("saving", "saving…"),
        ("consolidating", "learning from this session…"),
        ("indexing", "indexing repository…"),
        ("anchoring", "preserving context…"),
        ("syncing", "saving session…"),
    ],
)
def test_active_phase_renders_present_tense_in_accent(sl, phase, text):
    _marker(sl, active=_active(phase))
    assert sl._activity_segment(_SESSION) == _accent(sl, text)


def test_setting_up_carries_its_stage(sl):
    _marker(sl, active=_active("setting_up", "starting memory server"))
    assert sl._activity_segment(_SESSION) == _accent(
        sl, "setting up memory · starting memory server…"
    )


def test_active_phase_wins_over_a_fresh_result(sl):
    _marker(sl, active=_active("recalling"), results={"saved": _result(5, age=2)})
    assert sl._activity_segment(_SESSION) == _accent(sl, "recalling…")


def test_stale_active_phase_is_ignored(sl):
    """A hook that died mid-flight must not leave the bar 'recalling…' forever."""
    _marker(sl, active=_active("recalling", age=sl._PHASE_STALE_SECONDS + 1))
    assert sl._activity_segment(_SESSION) == ""


def test_stale_active_phase_falls_back_to_the_last_result(sl):
    _marker(
        sl,
        active=_active("recalling", age=sl._PHASE_STALE_SECONDS + 1),
        results={"saved": _result(2, age=5)},
    )
    assert sl._activity_segment(_SESSION) == " · ✓ memory saved"


def test_unknown_phase_renders_nothing(sl):
    _marker(sl, active=_active("defragmenting"))
    assert sl._activity_segment(_SESSION) == ""


# ── finished results ───────────────────────────────────────────────────────


def test_recalled_count_renders_past_tense(sl):
    _marker(sl, results={"recalled": _result(3)})
    assert sl._activity_segment(_SESSION) == " · 3 memories used"


def test_single_memory_is_singular(sl):
    _marker(sl, results={"saved": _result(1)})
    assert sl._activity_segment(_SESSION) == " · ✓ memory saved"


def test_zero_recalled_is_phrased_not_counted(sl):
    _marker(sl, results={"recalled": _result(0)})
    assert sl._activity_segment(_SESSION) == " · nothing relevant"


def test_failed_recall_says_so(sl):
    _marker(sl, results={"recalled": _result(0, ok=False)})
    assert sl._activity_segment(_SESSION) == " · recall failed"


@pytest.mark.parametrize(
    "kind, text",
    [
        ("ready", "memory ready"),
        ("consolidated", "✓ learned"),
        ("indexed", "✓ repository indexed"),
        ("anchored", "✓ context kept"),
        ("synced", "✓ session saved"),
    ],
)
def test_other_results_have_plain_labels(sl, kind, text):
    _marker(sl, results={kind: _result()})
    assert sl._activity_segment(_SESSION) == f" · {text}"


def test_failed_ready_says_unavailable(sl):
    _marker(sl, results={"ready": _result(ok=False)})
    assert sl._activity_segment(_SESSION) == " · memory unavailable"


def _connected(sl, monkeypatch, url="http://127.0.0.1:8000"):
    """This session's own connection record says the server is up."""
    monkeypatch.setenv("COGNEE_BASE_URL", url)
    now = time.time()
    write_json(
        sl._CONN_STATE_DIR / f"{_SESSION}.json",
        {
            "state": "ready",
            "base_url": url,
            "checked_at": now,
            "ready_at": now,
            "session_key": _SESSION,
        },
    )


def test_failed_ready_is_dropped_once_the_connection_is_confirmed(sl, monkeypatch):
    """The hook gave up on a boot that then finished: the marker that confirms
    the server beats the older verdict, so no "memory unavailable" next to ●."""
    _connected(sl, monkeypatch)
    _marker(sl, results={"ready": _result(ok=False)})
    assert sl._activity_segment(_SESSION) == ""
    # The next most recent result stands in for it.
    _marker(sl, results={"ready": _result(ok=False, age=1), "recalled": _result(count=2, age=10)})
    assert sl._activity_segment(_SESSION) == " · 2 memories used"
    # A confirmed-ready result is shown as before.
    _marker(sl, results={"ready": _result(ok=True)})
    assert sl._activity_segment(_SESSION) == " · memory ready"


@pytest.mark.parametrize(
    "kind, text",
    [
        ("anchored", "context not kept"),
        ("synced", "session sync failed"),
        ("saved", "save failed"),
    ],
)
def test_failed_work_is_never_ticked(sl, kind, text):
    """``ok`` False is honoured for every kind: a sync that raised, a
    compaction anchor that failed or a dropped write must not read as done."""
    _marker(sl, results={kind: _result(0 if kind == "saved" else None, ok=False)})
    assert sl._activity_segment(_SESSION) == f" · {text}"


def test_a_buffered_write_is_queued_not_failed(sl):
    entry = _result(0, ok=False)
    entry["buffered"] = True
    _marker(sl, results={"saved": entry})
    assert sl._activity_segment(_SESSION) == " · save queued"


def test_a_failed_write_after_a_saved_one_still_ticks_the_turn(sl):
    """The count is the turn's total; one landed write keeps the tick."""
    _marker(sl, results={"saved": _result(1, ok=False)})
    assert sl._activity_segment(_SESSION) == " · ✓ memory saved"


def test_most_recent_result_wins(sl):
    _marker(sl, results={"recalled": _result(3, age=20), "saved": _result(5, age=2)})
    assert sl._activity_segment(_SESSION) == " · ✓ memory saved"


def test_old_result_renders_faint(sl):
    _marker(sl, results={"recalled": _result(3, age=sl._RESULT_DIM_AFTER_SECONDS + 1)})
    assert sl._activity_segment(_SESSION) == f" {_DIM}· 3 memories used{_RESET}"


def test_unparseable_count_reads_as_none(sl):
    _marker(sl, results={"recalled": {"at": time.time(), "count": "lots"}})
    assert sl._activity_segment(_SESSION) == " · nothing relevant"


def test_colour_resets_at_the_end(sl):
    _marker(sl, active=_active("saving"))
    assert sl._activity_segment(_SESSION).endswith(_RESET)


# ── attribution ────────────────────────────────────────────────────────────


def test_another_sessions_marker_is_never_read(sl):
    _marker(sl, session="someone-else", results={"recalled": _result(3)})
    assert sl._activity_segment(_SESSION) == ""


def test_path_unsafe_session_id_does_not_escape_the_phase_dir(sl):
    _marker(sl, results={"recalled": _result(3)})
    assert sl._activity_segment("../../etc/passwd") == ""


# ── opt-out, icon, json ────────────────────────────────────────────────────


def test_env_opt_out_suppresses_the_segment(sl, monkeypatch):
    _marker(sl, active=_active("recalling"))
    monkeypatch.setenv("COGNEE_STATUSLINE_ACTIVITY", "false")
    assert sl._activity_segment(_SESSION) == ""


def test_icon_is_off_unless_asked_for(sl, monkeypatch):
    monkeypatch.delenv("COGNEE_STATUSLINE_ICON", raising=False)
    assert sl._icon() == ""
    monkeypatch.setenv("COGNEE_STATUSLINE_ICON", "brain")
    assert sl._icon() == "🧠 "


def test_json_snapshot_exposes_the_activity_as_data(sl):
    _marker(sl, active=_active("recalling"))
    snap = sl._snapshot(_SESSION, enabled=True)
    assert snap["activity"] == {
        "kind": "active",
        "phase": "recalling",
        "text": "recalling…",
        "age": pytest.approx(0.0, abs=5.0),
    }
    assert snap["enabled"] is True
    assert snap["text"].endswith(" · recalling…")
    assert "\033" not in snap["text"]


def test_json_snapshot_carries_a_finished_result(sl):
    _marker(sl, results={"recalled": _result(3)})
    snap = sl._snapshot(_SESSION, enabled=True)
    assert snap["activity"]["kind"] == "result"
    assert snap["activity"]["text"] == "3 memories used"
    assert snap["activity"]["ok"] is True
