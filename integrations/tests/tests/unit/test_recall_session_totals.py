"""The per-session running total and the cross-session hit count the prompt hook
keeps for the status line.

``session-context-lookup`` writes ``recall/<session_key>.json`` on every prompt;
alongside the per-turn ``hits`` it carries ``session_totals`` — how many prompts
this session has seen and on how many of them memory injected something. That is
the ``12/40 turns had hits this session`` number in the bar (see
test_statusline_recall_counts.py for the rendering side).

Contract:
  * every prompt increments ``turns``; only a prompt with at least one injected
    result increments ``turns_with_hits``;
  * the total is carried forward from this session's own per-session file, never
    from the shared ``last_recall.json`` (which holds whoever prompted last);
  * a corrupt or legacy (total-less) per-session file restarts the count rather
    than breaking the write;
  * the per-session copy is only written for a path-safe session key;
  * ``cross_session_hits`` counts the graph passages not stamped with this
    session's id — session/trace/guidance hits are this session's by
    construction and never count.

claude-code only: codex's bar has no counts segment, so its hook keeps no total.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from utils.recall import SCOPES, drive_recall

_KEY = "fde122ae-07db-431d-b5af-acba353e4e3e"
_HIT = {
    "session": [{"question": "q1", "answer": "a1"}],
    "trace": [],
    "graph": [],
    "session_context": [],
}
_MISS = {scope: [] for scope in SCOPES}


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip(f"{suite.name}: the bar has no counts segment, so no session total")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: _KEY)
    return module


def _per_session(temp_home: Path, key: str = _KEY) -> dict:
    path = temp_home / ".cognee-plugin" / "claude-code" / "recall" / f"{key}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def _shared(temp_home: Path) -> dict:
    path = temp_home / ".cognee-plugin" / "claude-code" / "last_recall.json"
    return json.loads(path.read_text(encoding="utf-8"))


def test_first_prompt_starts_the_count(lookup, monkeypatch, temp_home):
    drive_recall(lookup, monkeypatch, recall=_HIT)
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_a_miss_counts_the_turn_but_not_a_hit(lookup, monkeypatch, temp_home):
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 0}


def test_totals_accumulate_across_prompts(lookup, monkeypatch, temp_home):
    for results in (_HIT, _MISS, _HIT, _HIT, _MISS):
        drive_recall(lookup, monkeypatch, recall=results)
    marker = _per_session(temp_home)
    assert marker["session_totals"] == {"turns": 5, "turns_with_hits": 3}
    # The per-turn part still describes only the latest prompt.
    assert sum(marker["hits"].values()) == 0


def test_the_shared_file_is_not_the_source_of_the_total(lookup, monkeypatch, temp_home):
    """Another session prompted last; its total must not seed ours."""
    shared = temp_home / ".cognee-plugin" / "claude-code" / "last_recall.json"
    shared.parent.mkdir(parents=True, exist_ok=True)
    shared.write_text(
        json.dumps(
            {
                "session_key": "noisy-neighbour",
                "hits": {"session": 3},
                "session_totals": {"turns": 99, "turns_with_hits": 98},
            }
        ),
        encoding="utf-8",
    )
    drive_recall(lookup, monkeypatch, recall=_HIT)
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 1}
    # ...and the shared copy now carries ours, stamped with our key.
    assert _shared(temp_home)["session_key"] == _KEY
    assert _shared(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_a_legacy_marker_without_totals_restarts_the_count(lookup, monkeypatch, temp_home):
    per = temp_home / ".cognee-plugin" / "claude-code" / "recall" / f"{_KEY}.json"
    per.parent.mkdir(parents=True, exist_ok=True)
    per.write_text(json.dumps({"session_key": _KEY, "hits": {"session": 2}}), encoding="utf-8")
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 0}


def test_a_corrupt_marker_restarts_the_count_instead_of_failing(lookup, monkeypatch, temp_home):
    per = temp_home / ".cognee-plugin" / "claude-code" / "recall" / f"{_KEY}.json"
    per.parent.mkdir(parents=True, exist_ok=True)
    per.write_text("not json{{{", encoding="utf-8")
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_negative_or_garbage_totals_are_clamped(lookup, monkeypatch, temp_home):
    per = temp_home / ".cognee-plugin" / "claude-code" / "recall" / f"{_KEY}.json"
    per.parent.mkdir(parents=True, exist_ok=True)
    per.write_text(
        json.dumps({"hits": {}, "session_totals": {"turns": -4, "turns_with_hits": "x"}}),
        encoding="utf-8",
    )
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    assert _per_session(temp_home)["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_path_unsafe_session_key_writes_no_per_session_copy(lookup, monkeypatch, temp_home):
    monkeypatch.setattr(lookup, "get_session_key", lambda: "../escape")
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    recall_dir = temp_home / ".cognee-plugin" / "claude-code" / "recall"
    assert not recall_dir.exists() or not any(recall_dir.iterdir())
    assert _shared(temp_home)["hits"]["session"] == 1


# ── from past sessions ─────────────────────────────────────────────────────

_SID = "sid"  # what drive_recall's _load_session_id seam returns


def _graph(*texts):
    return {
        "session": [],
        "trace": [],
        "graph": [{"source": "graph", "text": text} for text in texts],
        "session_context": [],
    }


def test_graph_passages_from_other_sessions_count(lookup, monkeypatch, temp_home):
    drive_recall(
        lookup,
        monkeypatch,
        recall=_graph(
            "Session ID: claude_other\n\nQuestion: q\n\nAnswer: a",
            "# Session learning — 2026-08-06 (session claude_older)\n\nUse ruff.",
        ),
    )
    marker = _per_session(temp_home)
    assert marker["hits"]["graph_context"] == 2
    assert marker["cross_session_hits"] == 2


def test_graph_passages_from_this_session_do_not_count(lookup, monkeypatch, temp_home):
    drive_recall(
        lookup,
        monkeypatch,
        recall=_graph(
            f"Session ID: {_SID}\n\nQuestion: q\n\nAnswer: a",
            "Session ID: claude_other\n\nQuestion: q2\n\nAnswer: a2",
        ),
    )
    marker = _per_session(temp_home)
    assert marker["hits"]["graph_context"] == 2
    assert marker["cross_session_hits"] == 1


def test_unstamped_graph_passages_count_as_outside_this_session(lookup, monkeypatch, temp_home):
    """A remember-ed document has no session header; it is still knowledge this
    conversation never produced."""
    drive_recall(lookup, monkeypatch, recall=_graph("The deploy runs from the release branch."))
    assert _per_session(temp_home)["cross_session_hits"] == 1


def test_session_scoped_hits_never_count_as_cross_session(lookup, monkeypatch, temp_home):
    drive_recall(
        lookup,
        monkeypatch,
        recall={
            "session": [{"question": "q", "answer": "a"}],
            "trace": [{"source": "trace", "origin_function": "Bash", "status": "success"}],
            "graph": [],
            "session_context": [{"source": "session_context", "content": "guidance"}],
        },
    )
    marker = _per_session(temp_home)
    assert sum(marker["hits"].values()) == 3
    assert marker["cross_session_hits"] == 0


def test_cross_session_count_is_logged_with_the_hit(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, recall=_graph("Session ID: claude_other\n\nx"))
    assert run.detail("context_lookup_hit")["cross_session_hits"] == 1
