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

Both suites keep the total. claude-code writes it to the per-session copy
``recall/<session_key>.json`` (what its bar reads); codex has one shared
``last_recall.json`` and carries the total forward only when that file is
stamped with its own ``session_key``.
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
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: _KEY)
    if suite.name == "codex":
        # codex prefixes the header with the plain status line; keep it inert.
        monkeypatch.setattr(module, "render_status_for_host", lambda key: "cognee: ds · local")
    return module


@pytest.fixture
def state(suite, temp_home) -> Path:
    return temp_home / ".cognee-plugin" / suite.name


@pytest.fixture
def per_session(suite, state):
    """Read the marker this suite's total lives in (see module docstring)."""

    def _read(key: str = _KEY) -> dict:
        path = state / "last_recall.json"
        if suite.name == "claude-code":
            path = state / "recall" / f"{key}.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    return _read


def _shared(state: Path) -> dict:
    return json.loads((state / "last_recall.json").read_text(encoding="utf-8"))


def _seed(state: Path, suite, payload: dict, key: str = _KEY) -> None:
    """Pre-write the marker a previous prompt of session ``key`` would have left."""
    path = state / "last_recall.json"
    if suite.name == "claude-code":
        path = state / "recall" / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def test_first_prompt_starts_the_count(lookup, monkeypatch, per_session):
    drive_recall(lookup, monkeypatch, recall=_HIT)
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_a_miss_counts_the_turn_but_not_a_hit(lookup, monkeypatch, per_session):
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 0}


def test_totals_accumulate_across_prompts(lookup, monkeypatch, per_session):
    for results in (_HIT, _MISS, _HIT, _HIT, _MISS):
        drive_recall(lookup, monkeypatch, recall=results)
    marker = per_session()
    assert marker["session_totals"] == {"turns": 5, "turns_with_hits": 3}
    # The per-turn part still describes only the latest prompt.
    assert sum(marker["hits"].values()) == 0


def test_another_sessions_marker_is_not_the_source_of_the_total(
    lookup, monkeypatch, suite, state, per_session
):
    """Another session prompted last; its total must not seed ours."""
    shared = state / "last_recall.json"
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
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 1}
    # ...and the shared copy now carries ours, stamped with our key.
    assert _shared(state)["session_key"] == _KEY
    assert _shared(state)["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_codex_continues_its_own_shared_marker(lookup, monkeypatch, suite, state, per_session):
    if suite.name != "codex":
        pytest.skip("claude-code keeps a per-session copy; covered by the accumulate test")
    _seed(
        state,
        suite,
        {"session_key": _KEY, "hits": {}, "session_totals": {"turns": 4, "turns_with_hits": 2}},
    )
    drive_recall(lookup, monkeypatch, recall=_HIT)
    assert per_session()["session_totals"] == {"turns": 5, "turns_with_hits": 3}


def test_a_legacy_marker_without_totals_restarts_the_count(
    lookup, monkeypatch, suite, state, per_session
):
    _seed(state, suite, {"session_key": _KEY, "hits": {"session": 2}})
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 0}


def test_a_corrupt_marker_restarts_the_count_instead_of_failing(
    lookup, monkeypatch, suite, state, per_session
):
    _seed(state, suite, "not json{{{")
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_negative_or_garbage_totals_are_clamped(lookup, monkeypatch, suite, state, per_session):
    _seed(
        state,
        suite,
        {
            "session_key": _KEY,
            "hits": {},
            "session_totals": {"turns": -4, "turns_with_hits": "x"},
        },
    )
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    assert per_session()["session_totals"] == {"turns": 1, "turns_with_hits": 1}


def test_path_unsafe_session_key_writes_no_per_session_copy(lookup, monkeypatch, suite, state):
    if suite.name != "claude-code":
        pytest.skip("codex has no per-session copy")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "../escape")
    run = drive_recall(lookup, monkeypatch, recall=_HIT)
    assert run.detail("last_recall_write_failed") is None, run.events
    recall_dir = state / "recall"
    assert not recall_dir.exists() or not any(recall_dir.iterdir())
    assert _shared(state)["hits"]["session"] == 1


# ── from past sessions ─────────────────────────────────────────────────────

_SID = "sid"  # what drive_recall's _load_session_id seam returns


def _graph(*texts):
    return {
        "session": [],
        "trace": [],
        "graph": [{"source": "graph", "text": text} for text in texts],
        "session_context": [],
    }


def test_graph_passages_from_other_sessions_count(lookup, monkeypatch, per_session):
    drive_recall(
        lookup,
        monkeypatch,
        recall=_graph(
            "Session ID: claude_other\n\nQuestion: q\n\nAnswer: a",
            "# Session learning — 2026-08-06 (session claude_older)\n\nUse ruff.",
        ),
    )
    marker = per_session()
    assert marker["hits"]["graph_context"] == 2
    assert marker["cross_session_hits"] == 2


def test_graph_passages_from_this_session_do_not_count(lookup, monkeypatch, per_session):
    drive_recall(
        lookup,
        monkeypatch,
        recall=_graph(
            f"Session ID: {_SID}\n\nQuestion: q\n\nAnswer: a",
            "Session ID: claude_other\n\nQuestion: q2\n\nAnswer: a2",
        ),
    )
    marker = per_session()
    assert marker["hits"]["graph_context"] == 2
    assert marker["cross_session_hits"] == 1


def test_unstamped_graph_passages_count_as_outside_this_session(lookup, monkeypatch, per_session):
    """A remember-ed document has no session header; it is still knowledge this
    conversation never produced."""
    drive_recall(lookup, monkeypatch, recall=_graph("The deploy runs from the release branch."))
    assert per_session()["cross_session_hits"] == 1


def test_session_scoped_hits_never_count_as_cross_session(lookup, monkeypatch, per_session):
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
    marker = per_session()
    assert sum(marker["hits"].values()) == 3
    assert marker["cross_session_hits"] == 0


def test_cross_session_count_is_logged_with_the_hit(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, recall=_graph("Session ID: claude_other\n\nx"))
    assert run.detail("context_lookup_hit")["cross_session_hits"] == 1


# ── codex: the header IS the user surface ─────────────────────────────────


def _codex_header(state: Path) -> str:
    """The `Cognee memory: …` line of the last injected context (from the audit log)."""
    last = (state / "recall-audit.log").read_text(encoding="utf-8").strip().splitlines()[-1]
    context = json.loads(last)["context"]
    return next(line for line in context.splitlines() if line.startswith("Cognee memory:"))


@pytest.fixture
def codex(suite):
    if suite.name != "codex":
        pytest.skip("the plain-words header is codex's surface; claude-code has the bar")


def test_codex_header_reads_in_plain_words(codex, lookup, monkeypatch, state):
    drive_recall(
        lookup,
        monkeypatch,
        recall={
            "session": [{"question": "q", "answer": "a"}],
            "trace": [],
            "graph": [
                {"source": "graph", "text": "Session ID: claude_other\n\nx"},
                {"source": "graph", "text": f"Session ID: {_SID}\n\ny"},
            ],
            "session_context": [],
        },
    )
    assert _codex_header(state) == (
        "Cognee memory: 3 memory hits (1 from a past session) · 1/1 turns had hits this session"
        " · saved last turn 0 prompt / 0 trace / 0 answer"
    )


def test_codex_header_says_warming_up_until_the_first_hit(codex, lookup, monkeypatch, state):
    drive_recall(lookup, monkeypatch, recall=_MISS)
    drive_recall(lookup, monkeypatch, recall=_MISS)
    assert _codex_header(state) == (
        "Cognee memory: 0 memory hits · memory warming up (2 turns)"
        " · saved last turn 0 prompt / 0 trace / 0 answer"
    )


def test_codex_header_omits_past_sessions_at_zero(codex, lookup, monkeypatch, state):
    drive_recall(lookup, monkeypatch, recall=_HIT)
    assert _codex_header(state).startswith("Cognee memory: 1 memory hit · 1/1 turns had hits")
