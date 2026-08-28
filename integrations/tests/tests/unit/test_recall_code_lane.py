"""The code lane inside the per-prompt recall fan-out.

This lane runs on the keystroke->answer path, so its whole design is "cost
nothing unless it can pay for itself". These tests pin that bargain end to end
through ``_run``:

  * an ordinary prompt dispatches exactly the four standard scopes — no extra
    request, no extra budget spent, and the visibility header keeps the shape
    its consumers already parse;
  * a prompt naming a symbol inside an INDEXED repo adds a fifth lane, carrying
    the repo's own dataset and a structured ``code_query`` — the semantic
    scopes are untouched, because the lane is additive, never a substitute;
  * code facts are injected under their own heading and counted separately;
  * a failure anywhere in the gate degrades to "no code lane" rather than
    taking the prompt's memory down with it.

The gate's pure logic lives in unit/test_code_graph_gate.py; the wire contract
in integration/test_code_graph.py.
"""

from __future__ import annotations

import pytest
from utils.recall import drive_recall

STANDARD = ["session", "trace", "session_context", "graph"]


def _header(output) -> str:
    """The one-line visibility header, wherever the suite puts it.

    claude-code nests systemMessage inside hookSpecificOutput; codex emits it
    at the top level alongside it. The header text is identical.
    """
    hook_output = output["hookSpecificOutput"]
    return str(hook_output.get("systemMessage") or output.get("systemMessage") or "")


@pytest.fixture
def lookup(suite, hook_module):
    return hook_module(suite, "session-context-lookup.py")


@pytest.fixture
def indexed_repo(suite, isolated_modules, tmp_path):
    """A repo recorded as indexed, so the lane's opt-in requirement is met."""
    cg = isolated_modules(suite, "_code_graph")
    repo = tmp_path / "proj"
    (repo / "src").mkdir(parents=True)
    cg.save_repo_state(
        {
            "spec": str(repo),
            "spec_kind": "path",
            "repo_root": str(repo),
            "dataset": "codebase-proj",
            "fingerprint": "seed",
        }
    )
    return repo


# ── the lane stays off ─────────────────────────────────────────────────────


def test_conversational_prompt_dispatches_only_standard_scopes(lookup, monkeypatch, indexed_repo):
    """Inside an indexed repo, prose still costs nothing extra."""
    run = drive_recall(
        lookup, monkeypatch, prompt="thanks, that looks right", cwd=str(indexed_repo)
    )
    assert run.calls == STANDARD
    assert not run.fired("code_lane_armed")


def test_identifier_outside_an_indexed_repo_does_not_arm(lookup, monkeypatch, tmp_path):
    """No opt-in for this checkout — the lane must not query someone else's graph."""
    run = drive_recall(lookup, monkeypatch, prompt="what calls process_payment?", cwd=str(tmp_path))
    assert run.calls == STANDARD


def test_header_shape_is_unchanged_when_the_lane_is_off(suite, lookup, monkeypatch):
    """The one-line header is parsed downstream; adding a code counter to every
    turn would change a line that most turns have no code content for.

    claude-code's header still lists every scope (``… / N graph / …``); codex's
    reads in plain words (``N memory hits · … turns had hits this session``).
    """
    run = drive_recall(
        lookup,
        monkeypatch,
        prompt="what happened earlier",
        recall={"session": [{"question": "q", "answer": "a"}]},
    )
    header = _header(run.output)
    assert "code" not in header
    if suite.name == "codex":
        assert "memory hit" in header and "turns had hits this session" in header
    else:
        assert "session" in header and "graph" in header


# ── the lane fires ─────────────────────────────────────────────────────────


def test_identifier_in_an_indexed_repo_adds_the_lane(lookup, monkeypatch, indexed_repo):
    run = drive_recall(
        lookup,
        monkeypatch,
        prompt="what calls process_payment?",
        cwd=str(indexed_repo / "src"),
    )
    assert "code" in run.calls
    # Additive: every semantic scope still ran, and the code lane precedes the
    # graph long pole so a warm snapshot answers before the budget is spent.
    assert [c for c in run.calls if c != "code"] == STANDARD
    assert run.calls.index("code") < run.calls.index("graph")

    armed = run.detail("code_lane_armed")
    assert armed["identifier"] == "process_payment"
    assert armed["dataset"] == "codebase-proj"


def test_lane_carries_the_repo_dataset_and_structured_query(lookup, monkeypatch, indexed_repo):
    """The repo's own narrow dataset — not the session dataset — and a
    deterministic code_query the server can execute without an LLM."""
    run = drive_recall(lookup, monkeypatch, prompt="explain UserService", cwd=str(indexed_repo))
    code_kwargs = run.kwargs["code"]
    assert code_kwargs["dataset"] == "codebase-proj"
    assert code_kwargs["code_query"]["operation"] == "query_facts"
    assert code_kwargs["code_query"]["name"] == "UserService"


def test_semantic_scopes_keep_the_session_dataset(lookup, monkeypatch, indexed_repo):
    """The code lane's dataset override must not leak into the other scopes."""
    run = drive_recall(lookup, monkeypatch, prompt="explain UserService", cwd=str(indexed_repo))
    for scope in STANDARD:
        assert run.kwargs[scope].get("dataset") != "codebase-proj"
        assert run.kwargs[scope].get("code_query") is None


def test_code_facts_are_injected_and_counted(lookup, monkeypatch, indexed_repo):
    run = drive_recall(
        lookup,
        monkeypatch,
        prompt="what calls process_payment?",
        cwd=str(indexed_repo),
        recall={
            "code": [{"source": "code", "text": "process_payment (function) — billing/pay.py:42"}]
        },
    )
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert "=== Code graph facts ===" in context
    assert "billing/pay.py:42" in context

    assert run.detail("context_lookup_hit")["counts"]["code"] == 1
    assert "1 code" in _header(run.output)


def test_empty_code_lane_is_not_an_error(lookup, monkeypatch, indexed_repo):
    """A seed the graph cannot resolve returns nothing server-side; the turn
    must look exactly like a normal turn that found no code facts."""
    run = drive_recall(
        lookup,
        monkeypatch,
        prompt="what calls process_payment?",
        cwd=str(indexed_repo),
        recall={"session": [{"question": "q", "answer": "a"}]},
    )
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert "=== Code graph facts ===" not in context
    assert "0 code" in _header(run.output)
    assert not run.fired("recall_error")


# ── failure containment ────────────────────────────────────────────────────


def test_a_broken_gate_never_breaks_the_prompt(lookup, monkeypatch, indexed_repo):
    """The gate is best-effort: if it raises, recall proceeds without the lane."""
    import sys
    import types

    broken = types.ModuleType("_code_graph")

    def _boom(*_a, **_kw):
        raise RuntimeError("gate exploded")

    broken.auto_code_lane = _boom
    monkeypatch.setitem(sys.modules, "_code_graph", broken)

    run = drive_recall(
        lookup,
        monkeypatch,
        prompt="what calls process_payment?",
        cwd=str(indexed_repo),
        recall={"session": [{"question": "q", "answer": "a"}]},
    )
    assert run.calls == STANDARD
    assert run.fired("code_lane_gate_error")
    assert "q" in run.output["hookSpecificOutput"]["additionalContext"]
