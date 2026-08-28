"""Tests for `_recall_segment` (cognee_statusline_render.py) — the
`· 5 memory hits · 12/40 turns had hits this session` counts at the end of the bar.

Contract:
  * per turn: the sum of every scope's hits, in plain words, singularized at 1
    (`1 memory hit`); zero still renders (memory ran and found nothing);
  * per session: `H/T turns had hits this session`, faint, from the marker's
    `session_totals`; a session with no hit yet says `memory warming up (N turns)`
    instead of a bare `0/N`;
  * the session total is only ever read from this session's own per-session
    copy — the shared file (another session's, or a legacy hook's) never
    provides one, and a neighbour's per-turn counts must never show either;
  * an unattributed shared marker is still rendered (lagging counts beat none);
  * a session id arrives from stdin JSON, so it never builds a path unchecked;
  * `(N from past sessions)` follows the per-turn count when the marker's
    `cross_session_hits` is positive — graph passages from earlier sessions —
    singular at 1, capped at the total, omitted at 0;
  * `COGNEE_STATUSLINE_COUNTS=false` hides the segment; `=full` restores the
    per-scope diagnostic strip `recall 4s/5t/0g/1a · saved 2p/41t/2a`.

claude-code only: codex's renderer has no `_recall_segment` (its bar is a short
plain-text string for the model's context, not a terminal diagnostic strip).
The bar-level placement is covered in e2e/test_statusline_bar.py.
"""

from __future__ import annotations

import json

import pytest
from utils.statusline import write_json

_SESSION = "fde122ae-07db-431d-b5af-acba353e4e3e"
_HITS = {"session": 4, "trace": 5, "graph_context": 0, "session_context": 1}  # 10
_SAVES = {"prompt": 2, "trace": 41, "answer": 2}
_TOTALS = {"turns": 40, "turns_with_hits": 12}

_DIM, _RESET = "\033[2m", "\033[0m"
_PER_TURN = " · 10 memory hits"
_FULL = f"{_PER_TURN} {_DIM}· 12/40 turns had hits this session{_RESET}"
_STRIP_FULL = f" {_DIM}· recall 4s/5t/0g/1a · saved 2p/41t/2a{_RESET}"
_STRIP_COUNTS_ONLY = f" {_DIM}· recall 4s/5t/0g/1a{_RESET}"


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


def _mine(hits=_HITS, totals=_TOTALS, **extra):
    entry = {"session_key": _SESSION, "hits": hits, **extra}
    if totals is not None:
        entry["session_totals"] = totals
    return {_SESSION: entry}


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


# ── per turn ───────────────────────────────────────────────────────────────


def test_per_turn_is_the_sum_over_scopes(sl):
    _marker(sl, per_session=_mine(totals=None))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_per_turn_counts_every_scope_including_code(sl):
    hits = {"session": 1, "trace": 1, "graph_context": 1, "session_context": 1, "code": 1}
    _marker(sl, per_session=_mine(hits=hits, totals=None))
    assert sl._recall_segment(_SESSION) == " · 5 memory hits"


def test_single_hit_is_singular(sl):
    hits = {"session": 1, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=hits, totals=None))
    assert sl._recall_segment(_SESSION) == " · 1 memory hit"


def test_zero_hits_still_render(sl):
    """0 is information — memory ran this turn and found nothing."""
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=zeros, totals=None))
    assert sl._recall_segment(_SESSION) == " · 0 memory hits"


def test_unparseable_counts_read_as_zero(sl):
    _marker(sl, per_session=_mine(hits={"session": "x", "trace": 5}, totals=None))
    assert sl._recall_segment(_SESSION) == " · 5 memory hits"


def test_saves_are_not_shown_by_default(sl):
    _marker(sl, per_session=_mine(totals=None, saves_last_turn=_SAVES))
    assert sl._recall_segment(_SESSION) == _PER_TURN


# ── from past sessions ─────────────────────────────────────────────────────


def test_cross_session_hits_ride_along_with_the_per_turn_count(sl):
    _marker(sl, per_session=_mine(cross_session_hits=3))
    assert sl._recall_segment(_SESSION) == (
        f" · 10 memory hits (3 from past sessions) "
        f"{_DIM}· 12/40 turns had hits this session{_RESET}"
    )


def test_a_single_cross_session_hit_is_singular(sl):
    _marker(sl, per_session=_mine(totals=None, cross_session_hits=1))
    assert sl._recall_segment(_SESSION) == " · 10 memory hits (1 from a past session)"


def test_zero_cross_session_hits_are_omitted(sl):
    _marker(sl, per_session=_mine(totals=None, cross_session_hits=0))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_cross_session_hits_never_exceed_the_total(sl):
    """A stale or inconsistent marker must not claim more than was injected."""
    hits = {"session": 1, "trace": 0, "graph_context": 1, "session_context": 0}
    _marker(sl, per_session=_mine(hits=hits, totals=None, cross_session_hits=7))
    assert sl._recall_segment(_SESSION) == " · 2 memory hits (2 from past sessions)"


def test_garbage_cross_session_count_reads_as_zero(sl):
    _marker(sl, per_session=_mine(totals=None, cross_session_hits="many"))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_full_mode_ignores_cross_session_hits(sl, monkeypatch):
    _marker(sl, per_session=_mine(cross_session_hits=3))
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "full")
    assert sl._recall_segment(_SESSION) == _STRIP_COUNTS_ONLY


# ── per session ────────────────────────────────────────────────────────────


def test_session_total_renders_faint_after_the_per_turn_count(sl):
    _marker(sl, per_session=_mine())
    assert sl._recall_segment(_SESSION) == _FULL


def test_session_without_a_hit_yet_says_warming_up(sl):
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=zeros, totals={"turns": 7, "turns_with_hits": 0}))
    assert (
        sl._recall_segment(_SESSION)
        == f" · 0 memory hits {_DIM}· memory warming up (7 turns){_RESET}"
    )


def test_warming_up_singularizes_one_turn(sl):
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=zeros, totals={"turns": 1, "turns_with_hits": 0}))
    assert (
        sl._recall_segment(_SESSION)
        == f" · 0 memory hits {_DIM}· memory warming up (1 turn){_RESET}"
    )


def test_first_hit_flips_warming_up_to_the_ratio(sl):
    hits = {"session": 1, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=hits, totals={"turns": 8, "turns_with_hits": 1}))
    assert (
        sl._recall_segment(_SESSION)
        == f" · 1 memory hit {_DIM}· 1/8 turns had hits this session{_RESET}"
    )


def test_missing_totals_render_only_the_per_turn_count(sl):
    """A marker written by an older hook has no session_totals."""
    _marker(sl, per_session=_mine(totals=None))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_zero_turns_render_only_the_per_turn_count(sl):
    _marker(sl, per_session=_mine(totals={"turns": 0, "turns_with_hits": 0}))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_non_dict_totals_render_only_the_per_turn_count(sl):
    _marker(sl, per_session=_mine(totals="many"))
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_colour_resets_at_the_end(sl):
    _marker(sl, per_session=_mine())
    assert sl._recall_segment(_SESSION).endswith(_RESET)


# ── attribution: never show another session's counts ──────────────────────


def test_other_sessions_counts_are_hidden(sl):
    _marker(sl, {"session_key": "some-other-session", "hits": _HITS})
    assert sl._recall_segment(_SESSION) == ""


def test_unattributed_marker_still_renders(sl):
    """Older marker with no session_key — lagging counts beat no counts."""
    _marker(sl, {"hits": _HITS})
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_host_context_without_session_id_still_renders(sl):
    _marker(sl, {"session_key": "some-other-session", "hits": _HITS})
    assert sl._recall_segment("") == _PER_TURN


def test_shared_file_never_provides_a_session_total(sl):
    """A cumulative number only means something from the session that counted it."""
    _marker(sl, {"session_key": _SESSION, "hits": _HITS, "session_totals": _TOTALS})
    assert sl._recall_segment(_SESSION) == _PER_TURN


# ── per-session copy: every terminal shows its OWN numbers ────────────────


def test_per_session_file_is_preferred(sl):
    """Concurrent terminals: the shared file holds whoever prompted last."""
    mine = {"session": 9, "trace": 8, "graph_context": 7, "session_context": 6}  # 30
    _marker(
        sl,
        payload={"session_key": "noisy-neighbour", "hits": _HITS, "saves_last_turn": _SAVES},
        per_session={
            _SESSION: {"hits": mine, "session_totals": {"turns": 3, "turns_with_hits": 3}}
        },
    )
    assert (
        sl._recall_segment(_SESSION)
        == f" · 30 memory hits {_DIM}· 3/3 turns had hits this session{_RESET}"
    )


def test_falls_back_to_the_shared_file_when_no_per_session_copy(sl):
    _marker(sl, payload={"session_key": _SESSION, "hits": _HITS})
    assert sl._recall_segment(_SESSION) == _PER_TURN


def test_another_sessions_per_session_copy_is_never_read(sl):
    _marker(sl, per_session={"someone-else": {"hits": _HITS, "session_totals": _TOTALS}})
    assert sl._recall_segment(_SESSION) == ""


def test_path_unsafe_session_id_does_not_escape_the_recall_dir(sl):
    """The id arrives from stdin JSON; never build a path from it unchecked."""
    _marker(sl, payload={"hits": _HITS})
    assert sl._recall_segment("../../etc/passwd") == _PER_TURN


# ── opt-out / diagnostic mode ─────────────────────────────────────────────


def test_env_opt_out_suppresses_the_segment(sl, monkeypatch):
    _marker(sl, per_session=_mine())
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "false")
    assert sl._recall_segment(_SESSION) == ""


def test_full_mode_restores_the_per_scope_strip(sl, monkeypatch):
    _marker(sl, per_session=_mine(saves_last_turn=_SAVES))
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "full")
    assert sl._recall_segment(_SESSION) == _STRIP_FULL


def test_full_mode_omits_saves_when_absent(sl, monkeypatch):
    _marker(sl, per_session=_mine())
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "full")
    assert sl._recall_segment(_SESSION) == _STRIP_COUNTS_ONLY


def test_full_mode_zero_counts_still_render(sl, monkeypatch):
    """0g is information — graph was searched and returned nothing."""
    zeros = {"session": 0, "trace": 0, "graph_context": 0, "session_context": 0}
    _marker(sl, per_session=_mine(hits=zeros))
    monkeypatch.setenv("COGNEE_STATUSLINE_COUNTS", "full")
    assert sl._recall_segment(_SESSION) == f" {_DIM}· recall 0s/0t/0g/0a{_RESET}"


def test_marker_on_disk_is_plain_data(sl):
    """Sanity: the counts are data; only the render step formats them."""
    _marker(sl, {"session_key": _SESSION, "hits": _HITS})
    assert json.loads(sl._RECALL_PATH.read_text(encoding="utf-8"))["hits"] == _HITS
