"""Per-scope recall instrumentation and the shared time budget.

Recall runs on every single prompt, so it is the plugin's most latency-sensitive
path. Since the only_context contract of cognee 1.6.0 (SDK-741) it is ONE
graph-scope request — the item that comes back already carries the conversation
history, the retrieved context and the session guidance — plus the code lane on
prompts that arm it (see test_recall_code_lane.py). Two things have to hold: the
record must show what each dispatched scope did — including a scope that found
nothing or never ran — and the fan-out must respect one overall budget rather
than letting any request run a full timeout past the deadline.

Contract:
  * the event carries a ``{hits, elapsed_ms}`` record for every dispatched
    scope, in canonical order (``graph``, then ``code`` when armed), without
    disturbing the aggregate ``counts``;
  * per-scope hits are raw attribution — ``graph`` is not folded here, while
    ``counts`` buckets it into ``graph_context``;
  * an open breaker runs nothing yet still reports the scope as skipped;
  * the scopes are dispatched concurrently, every one with the same deadline —
    the whole budget — so the prompt waits for the slowest request, not the
    sum, and no request waits behind another; the budget is the only knob, so
    ``COGNEE_RECALL_TIMEOUT`` is ignored here;
  * a budget too small for any honest attempt dispatches nothing at all;
  * the synchronous prompt hook never drains the warmup buffer.

The properties that need two requests in flight (overlap, fold order) arm the
code lane, the only other request the hook can make.

All registered suites carry this machinery identically (``per_scope``,
``MIN_SCOPE_TIMEOUT``, ``recall_budget_exceeded``), so all are exercised.

Migrated from claude-code/tests/test_per_scope_timing.py, which ran in no CI job
on any platform.
"""

from __future__ import annotations

import time

import pytest
from utils.recall import (
    CODE_SCOPES,
    SCOPES,
    arm_code_lane,
    assert_valid_per_scope,
    drive_recall,
    load_lookup,
)


@pytest.fixture
def lookup(suite, hook_module):
    return hook_module(suite, "session-context-lookup.py")


def test_a_hit_reports_the_scope(lookup, monkeypatch):
    """One memory item, and the aggregate counters still line up."""
    run = drive_recall(
        lookup,
        monkeypatch,
        recall={"graph": [{"source": "graph", "text": "The question is: `q1`\n\nContext:\n`gg`"}]},
    )

    detail = run.detail("context_lookup_hit")
    assert detail is not None, f"expected a context_lookup_hit: {run.events}"
    assert "counts" in detail, "the aggregate counts must survive alongside per_scope"

    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope)
    assert per_scope["graph"]["hits"] == 1

    # Raw attribution above; bucketed here — graph folds into graph_context.
    assert detail["counts"]["graph_context"] == 1


def test_an_armed_code_lane_reports_alongside_memory(lookup, monkeypatch):
    """Both requests report, memory first, whatever each one found."""
    arm_code_lane(monkeypatch)
    run = drive_recall(
        lookup,
        monkeypatch,
        recall={
            "graph": [{"source": "graph", "text": "The question is: `q1`\n\nContext:\n`gg`"}],
            "code": [],
        },
    )

    detail = run.detail("context_lookup_hit")
    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope, CODE_SCOPES)
    assert per_scope["graph"]["hits"] == 1
    assert per_scope["code"]["hits"] == 0
    assert detail["counts"]["graph_context"] == 1 and detail["counts"]["code"] == 0


def test_a_total_miss_still_reports_the_scope(lookup, monkeypatch):
    """Nothing found is not nothing to report: the request ran and says so."""
    run = drive_recall(lookup, monkeypatch, recall={scope: [] for scope in SCOPES})

    detail = run.detail("context_lookup_empty")
    assert detail is not None, f"expected a context_lookup_empty: {run.events}"

    per_scope = detail["per_scope"]
    assert_valid_per_scope(per_scope)
    assert all(record["hits"] == 0 for record in per_scope.values())
    assert not any(record.get("skipped") for record in per_scope.values()), (
        f"the scope ran, so it may not be marked skipped: {per_scope}"
    )


def test_an_open_breaker_skips_the_scope_but_still_reports(lookup, monkeypatch):
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


def test_every_scope_gets_the_whole_budget_as_its_deadline(lookup, monkeypatch):
    """One deadline for the whole fan-out: the budget, and only the budget.

    Nobody is handed the budget "remaining after earlier scopes", because
    nothing runs earlier — the memory request and the code lane are in flight
    together. And with the scopes concurrent, a per-scope timeout would bound
    the very same interval, so the hook no longer reads
    ``COGNEE_RECALL_TIMEOUT``: set it to anything and the deadline stays the
    budget.
    """
    arm_code_lane(monkeypatch)
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.8")
    monkeypatch.setenv("COGNEE_RECALL_TIMEOUT", "0.1")
    run = drive_recall(lookup, monkeypatch, recall={scope: [] for scope in CODE_SCOPES})
    assert set(run.timeouts) == set(CODE_SCOPES), run.timeouts
    assert all(0.7 <= t <= 0.8 for t in run.timeouts.values()), (
        f"expected every scope to get the budget as its deadline: {run.timeouts}"
    )
    assert not run.fired("recall_budget_exceeded"), run.events


def test_scopes_run_concurrently_so_the_prompt_waits_for_the_slowest(
    suite, hook_module, monkeypatch
):
    """Two slow requests (0.45s memory + 0.3s code) must overlap, not run back to back.

    Sequential dispatch made every extra request a full round trip on top of
    the graph search. Concurrent dispatch is the point of the fan-out, so it is
    pinned by the requests' own clocks: the second sleep starts before the
    first ends. Wall time is not the yardstick — on a loaded CI runner (Windows
    most of all) thread start-up and the hook's own bookkeeping around the
    fan-out add hundreds of milliseconds and made a wall-clock bound flaky. The
    codex core also renders its status line inside ``_run``; ``load_lookup``
    holds that inert so only the fan-out is under test.
    """
    lookup = load_lookup(suite, hook_module, monkeypatch)
    arm_code_lane(monkeypatch)
    sleeps = {"graph": 0.45, "code": 0.3}
    windows: dict[str, tuple[float, float]] = {}

    def slow_recall(_prompt, **kw):
        scope = kw["scope"][0]
        started = time.monotonic()
        time.sleep(sleeps.get(scope, 0))
        windows[scope] = (started, time.monotonic())
        return []

    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "5")
    run = drive_recall(lookup, monkeypatch, recall=slow_recall)

    assert set(run.calls) == set(CODE_SCOPES), run.calls
    graph_start, graph_end = windows["graph"]
    code_start, code_end = windows["code"]
    assert max(graph_start, code_start) < min(graph_end, code_end), (
        f"scopes ran back to back: graph {windows['graph']}, code {windows['code']}"
    )

    per_scope = run.detail("context_lookup_empty")["per_scope"]
    assert_valid_per_scope(per_scope, CODE_SCOPES)
    assert not any(record.get("skipped") for record in per_scope.values()), per_scope
    assert per_scope["graph"]["elapsed_ms"] >= 400, per_scope
    assert per_scope["code"]["elapsed_ms"] >= 250, per_scope


def test_a_budget_below_the_floor_dispatches_nothing(lookup, monkeypatch):
    """Less than MIN_SCOPE_TIMEOUT of budget cannot return anything useful.

    Firing a request with a doomed deadline only loads the server; the hook
    logs ``recall_budget_exceeded`` and reports the scope as skipped.
    """
    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.05")
    run = drive_recall(lookup, monkeypatch, recall={scope: [] for scope in SCOPES})

    assert run.calls == [], f"nothing may be dispatched below the floor: {run.calls}"
    assert run.fired("recall_budget_exceeded"), f"budget overrun not logged: {run.events}"
    per_scope = run.detail("context_lookup_empty")["per_scope"]
    assert_valid_per_scope(per_scope)
    assert all(record.get("skipped") for record in per_scope.values()), per_scope


def test_the_injected_context_is_identical_whatever_order_the_scopes_answer_in(lookup, monkeypatch):
    """Golden parity: staggered arrivals produce the byte-identical injection.

    The sections are folded in canonical order after the fan-out, so a run
    where the code lane answers first and memory last must render exactly what
    an all-instant run renders — code facts first, then memory. The header
    line is stripped before comparing: it carries per-session running totals
    that legitimately differ between two consecutive runs on one host.
    """
    arm_code_lane(monkeypatch)
    hits = {
        "graph": [{"source": "graph", "text": "The question is: `q1`\n\nContext:\ngraph fact"}],
        "code": [{"source": "code", "text": "process_payment (function) — billing/pay.py:42"}],
    }
    delays = {"graph": 0.3, "code": 0.0}

    def staggered(_prompt, **kw):
        scope = kw["scope"][0]
        time.sleep(delays[scope])
        return list(hits[scope])

    def body(run) -> str:
        text = run.output["hookSpecificOutput"]["additionalContext"]
        return text.split("\n", 1)[1]

    instant = drive_recall(lookup, monkeypatch, recall=hits)
    shuffled = drive_recall(lookup, monkeypatch, recall=staggered)

    assert body(shuffled) == body(instant)
    context = body(shuffled)
    positions = [
        context.index("=== Code graph facts ==="),
        context.index("=== Cognee memory ==="),
    ]
    assert positions == sorted(positions), context
    for needle in ("billing/pay.py:42", "graph fact"):
        assert needle in context, context


def test_the_prompt_hook_does_not_drain_the_warmup_buffer(lookup):
    """#298: draining here would stall the prompt for 10-30s.

    The drain belongs to the asynchronous sibling (store-user-prompt). Pinned by
    absence — the synchronous hook must not even carry the function.
    """
    assert not hasattr(lookup, "drain_warmup_entries")
