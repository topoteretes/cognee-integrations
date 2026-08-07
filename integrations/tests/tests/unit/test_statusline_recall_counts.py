"""Tests for `_recall_segment` (cognee_statusline_render.py) — the faint
`· recall 4s/5t/0g/1a · saved 2p/41t/2a` diagnostics at the end of the bar.

Contract:
  * zeros still render (0g means the graph WAS searched and returned nothing);
  * saves are omitted when the marker has none; unparseable counts read as 0;
  * counts belong to the session that wrote them — a concurrent terminal's
    numbers must never show — while an unattributed marker is still rendered
    (lagging counts beat no counts);
  * the per-session copy wins over the shared file, which holds whoever
    prompted last;
  * a session id arrives from stdin JSON, so it never builds a path unchecked;
  * `COGNEE_STATUSLINE_COUNTS=false` hides the segment.

claude-code only: codex's renderer has no `_recall_segment` (its bar is a short
plain-text string for the model's context, not a terminal diagnostic strip).
The bar-level placement is covered in e2e/test_statusline_bar.py.

Migrated from claude-code/tests/test_statusline_recall_counts.py.
"""

from __future__ import annotations

import json

import pytest
from utils.statusline import write_json

_SESSION = "fde122ae-07db-431d-b5af-acba353e4e3e"
_HITS = {"session": 4, "trace": 5, "graph_context": 0, "session_context": 1}
_SAVES = {"prompt": 2, "trace": 41, "answer": 2}
_FULL = " \033[2m· recall 4s/5t/0g/1a · saved 2p/41t/2a\033[0m"
_COUNTS_ONLY = " \033[2m· recall 4s/5t/0g/1a\033[0m"


@pytest.fixture
def sl(suite, statusline):
    if not hasattr(statusline, "_recall_segment"):
        pytest.skip(f"{suite.name}: no recall-counts segment (the bar is plain and short)")
    return statusline


def _marker(sl, payload=None, per_session=None):
    """Write the shared last_recall.json and/or per-session recall/<key>.json."""
    if payload is not None:
        sl._RECALL_PATH.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(payload, str):
            sl._RECALL_PATH.write_text(payload, encoding="utf-8")
        else:
            write_json(sl._RECALL_PATH, payload)
    for key, entry in (per_session or {}).items():
        write_json(sl._RECALL_DIR / f"{key}.json", entry)


# ── nothing to show ────────────────────────────────────────────────────────


def test_no_marker_file_renders_nothing(sl):
    assert sl._recall_segment(_SESSION) == ""


def test_malformed_marker_renders_nothing(sl):
    _marker(sl, "not json{{{")
    assert sl._recall_segment(_SESSION) == ""


def test_marker_without_hits_renders_nothing(sl):
    _marker(sl, {"session_key": _SESSION, "ts": "2026-07-28T10:04:17+00:00"})
    assert sl._recall_segment(_SESSION) == ""


def test_non_dict_hits_renders_nothing(sl):
    _marker(sl, {"session_key": _SESSION, "hits": "lots"})
    assert sl._recall_segment(_SESSION) == ""


# ── the rendering contract ─────────────────────────────────────────────────


def test_counts_render_with_recall_and_saves(sl):
    _marker(sl, {"session_key": _SESSION, "hits": _HITS, "saves_last_turn": _SAVES})
    assert sl._recall_segment(_SESSION) == _FULL


def test_zero_counts_still_render(sl):
    """0g is information — graph was searched and returned nothing."""
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, {"session_key": _SESSION, "hits": zeros})
    assert sl._recall_segment(_SESSION) == " \033[2m· recall 0s/0t/0g/0a\033[0m"


def test_saves_omitted_when_absent(sl):
    _marker(sl, {"session_key": _SESSION, "hits": _HITS})
    assert sl._recall_segment(_SESSION) == _COUNTS_ONLY


def test_missing_and_unparseable_counts_read_as_zero(sl):
    _marker(sl, {"session_key": _SESSION, "hits": {"session": "x", "trace": 5}})
    assert sl._recall_segment(_SESSION) == " \033[2m· recall 0s/5t/0g/0a\033[0m"


def test_colour_resets_at_the_end(sl):
    _marker(sl, {"session_key": _SESSION, "hits": _HITS})
    assert sl._recall_segment(_SESSION).endswith("\033[0m")


# ── attribution: never show another session's counts ──────────────────────


def test_other_sessions_counts_are_hidden(sl):
    _marker(sl, {"session_key": "some-other-session", "hits": _HITS})
    assert sl._recall_segment(_SESSION) == ""


def test_unattributed_marker_still_renders(sl):
    """Older marker with no session_key — lagging counts beat no counts."""
    _marker(sl, {"hits": _HITS})
    assert sl._recall_segment(_SESSION) == _COUNTS_ONLY


def test_host_context_without_session_id_still_renders(sl):
    _marker(sl, {"session_key": "some-other-session", "hits": _HITS})
    assert sl._recall_segment("") == _COUNTS_ONLY


# ── per-session copy: every terminal shows its OWN numbers ────────────────


def test_per_session_file_is_preferred(sl):
    """Concurrent terminals: the shared file holds whoever prompted last."""
    mine = {"session": 9, "trace": 8, "graph_context": 7, "session_context": 6}
    _marker(
        sl,
        payload={"session_key": "noisy-neighbour", "hits": _HITS, "saves_last_turn": _SAVES},
        per_session={_SESSION: {"hits": mine}},
    )
    assert sl._recall_segment(_SESSION) == " \033[2m· recall 9s/8t/7g/6a\033[0m"


def test_falls_back_to_the_shared_file_when_no_per_session_copy(sl):
    _marker(sl, payload={"session_key": _SESSION, "hits": _HITS})
    assert sl._recall_segment(_SESSION) == _COUNTS_ONLY


def test_another_sessions_per_session_copy_is_never_read(sl):
    _marker(sl, per_session={"someone-else": {"hits": _HITS}})
    assert sl._recall_segment(_SESSION) == ""


def test_path_unsafe_session_id_does_not_escape_the_recall_dir(sl):
    """The id arrives from stdin JSON; never build a path from it unchecked."""
    _marker(sl, payload={"hits": _HITS})
    assert sl._recall_segment("../../etc/passwd") == _COUNTS_ONLY


# ── opt-out ───────────────────────────────────────────────────────────────


def test_env_opt_out_suppresses_the_segment(sl, monkeypatch):
    _marker(sl, {"session_key": _SESSION, "hits": _HITS})
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "false")
    assert sl._recall_segment(_SESSION) == ""


def test_marker_on_disk_is_plain_data(sl):
    """Sanity: the counts are data; only the render step formats them."""
    _marker(sl, {"session_key": _SESSION, "hits": _HITS})
    assert json.loads(sl._RECALL_PATH.read_text(encoding="utf-8"))["hits"] == _HITS
