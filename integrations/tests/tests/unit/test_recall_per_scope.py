"""Per-scope recall instrumentation and the shared time budget.

Recall fans out over four scopes (session / trace / session_context / graph) on
every single prompt, so it is the plugin's most latency-sensitive path. Two things
have to hold: the record must show what each scope did — including scopes that
found nothing or never ran — and the fan-out must respect one overall budget
rather than letting each scope spend a full timeout past the deadline.

Contract:
  * the event carries a ``{hits, elapsed_ms}`` record for all four scopes, in
    dispatch order, without disturbing the aggregate ``counts``;
  * per-scope hits are raw attribution — ``graph`` is not folded here, while
    ``counts`` buckets it into ``graph_context``;
  * an open breaker runs nothing yet still reports all four as skipped;
  * a scope's timeout is clamped to the budget remaining after earlier scopes,
    and once too little is left the rest are skipped, not dispatched;
  * the synchronous prompt hook never drains the warmup buffer.

Both suites carry this machinery identically (``per_scope``,
``MIN_SCOPE_TIMEOUT``, ``recall_budget_exceeded``), so both are exercised.

Migrated from claude-code/tests/test_per_scope_timing.py, which ran in no CI job
on any platform.
"""

from __future__ import annotations

import time

import pytest
from utils.recall import SCOPES, assert_valid_per_scope, drive_recall


@pytest.fixture
def lookup(suite, hook_module):
    return hook_module(suite, "session-context-lookup.py")


def test_a_hit_reports_every_scope(lookup, monkeypatch):
    """One hit per scope pair, and the aggregate counters still line up."""
    run = drive_recall(
        lookup,
        monkeypatch,
        recall={
            "session": [{"question": "q1", "answer": "a1"}],
            "trace": [],
            "graph": [{"source": "graph", "content": "gg"}],
            "session_context": [],
        },
    )

    detail = run.detail("context_lookup_hit")
    assert detail is not None, f"expected a context_lookup_hit: {run.events}"
    assert "counts" in detail, "the aggregate counts must survive alongside per_scope"

    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope)
    assert per_scope["session"]["hits"] == 1
    assert per_scope["trace"]["hits"] == 0
    assert per_scope["graph"]["hits"] == 1
    assert per_scope["session_context"]["hits"] == 0

    # Raw attribution above; bucketed here — graph folds into graph_context.
    assert detail["counts"]["graph_context"] == 1


def test_a_total_miss_still_reports_every_scope(lookup, monkeypatch):
    """Nothing found is not nothing to report: four scopes ran and each says so."""
    run = drive_recall(lookup, monkeypatch, recall={scope: [] for scope in SCOPES})

    detail = run.detail("context_lookup_empty")
    assert detail is not None, f"expected a context_lookup_empty: {run.events}"

    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope)
    assert all(record["hits"] == 0 for record in per_scope.values())
    assert not any(record.get("skipped") for record in per_scope.values()), (
        f"every scope ran, so none may be marked skipped: {per_scope}"
    )


def test_an_open_breaker_skips_every_scope_but_still_reports(lookup, monkeypatch):
    """Breaker open means no requests — and a record that says exactly that."""
    run = drive_recall(
        lookup,
        monkeypatch,
        recall={scope: [] for scope in SCOPES},
        breaker_open=(True, 30),
    )

    detail = run.detail("context_lookup_empty")
    assert detail is not None, f"expected a context_lookup_empty: {run.events}"

    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope)
    assert all(record.get("skipped") for record in per_scope.values()), per_scope
    assert all(
        record["hits"] == 0 and record["elapsed_ms"] == 0 for record in per_scope.values()
    ), f"a skipped scope cannot have spent time or found anything: {per_scope}"
    assert run.calls == [], f"breaker open must dispatch nothing, got {run.calls}"


def test_scope_timeouts_are_clamped_to_the_remaining_budget(lookup, monkeypatch):
    """The budget is shared, not per scope.

    With a 0.8s budget and a 0.5s per-call timeout, a slow ``session`` (0.45s)
    leaves ~0.35s, so ``trace`` must be dispatched with less than the full 0.5s.
    ``trace`` then burns 0.3s, dropping the remainder below MIN_SCOPE_TIMEOUT, so
    the last two scopes are skipped outright. Without the clamp each scope could
    run a full timeout past the deadline — four scopes overrunning a 0.8s budget
    is a visible stall on every prompt.
    """
    sleeps = {"session": 0.45, "trace": 0.3}

    def slow_recall(_prompt, **kw):
        time.sleep(sleeps.get(kw["scope"][0], 0))
        return []

    monkeypatch.setenv("COGNEE_RECALL_TIMEOUT", "0.5")
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.8")
    run = drive_recall(lookup, monkeypatch, recall=slow_recall)

    # The first scope owns the whole budget, so it gets the full per-call timeout.
    assert run.timeouts["session"] == 0.5, run.timeouts
    # The second is clamped to what is left, never the full timeout again.
    assert 0.2 <= run.timeouts["trace"] < 0.45, f"expected a clamped timeout: {run.timeouts}"
    # And the rest are never dispatched at all.
    assert set(run.timeouts) == {"session", "trace"}, run.timeouts
    assert run.fired("recall_budget_exceeded"), f"budget overrun not logged: {run.events}"

    per_scope = run.detail("context_lookup_empty")["per_scope"]
    assert_valid_per_scope(per_scope)
    for scope in ("session_context", "graph"):
        assert per_scope[scope].get("skipped"), f"{scope} should be marked skipped: {per_scope}"


def test_the_prompt_hook_does_not_drain_the_warmup_buffer(lookup):
    """#298: draining here would stall the prompt for 10-30s.

    The drain belongs to the asynchronous sibling (store-user-prompt). Pinned by
    absence — the synchronous hook must not even carry the function.
    """
    assert not hasattr(lookup, "drain_warmup_entries")
