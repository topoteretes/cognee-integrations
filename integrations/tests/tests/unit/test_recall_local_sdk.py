"""The recall dispatch on the in-process local-SDK branch.

``session-context-lookup.py`` makes one memory request per prompt (graph scope,
``HYBRID_COMPLETION``, ``only_context``), plus the code lane when a prompt arms
it. In HTTP mode each request is a blocking call pushed to a worker thread; on
the local-SDK branch each is ``cognee.recall`` awaited directly, so the two
interleave as coroutines on the hook's own event loop and each one is bounded by
``asyncio.wait_for``. Same dispatch, different mechanism — and until now the only
driver ran HTTP mode, so this branch shipped on inspection alone.

Contract, mirroring the HTTP tests:
  * the requests are awaited together — the prompt costs the slowest one;
  * one request raising drops that request only; the other is still injected;
  * a request past the shared deadline is cut there (``recall_error`` with a
    ``slow`` verdict) while the other still lands;
  * ``per_scope`` reports every dispatched scope, in canonical order;
  * the memory request carries scope, query type, only_context and the session id.

Only suites that declare ``has_local_sdk_recall`` carry the branch; the others
skip rather than pretend.
"""

from __future__ import annotations

import asyncio
import time

import pytest
from utils.recall import (
    CODE_LANE,
    CODE_SCOPES,
    SCOPES,
    arm_code_lane,
    assert_valid_per_scope,
    drive_recall,
)

MEMORY = {"source": "graph", "text": "The question is: `q1`\n\nContext:\n`graph fact`"}
CODE_FACT = {"source": "code", "text": "process_payment -> validate_card"}


@pytest.fixture
def lookup(suite, hook_module):
    if not suite.has_local_sdk_recall:
        pytest.skip(f"{suite.name}: no in-process local-SDK recall branch")
    return hook_module(suite, "session-context-lookup.py")


def _context(run) -> str:
    return run.output["hookSpecificOutput"]["additionalContext"]


def test_the_requests_are_awaited_together(lookup, monkeypatch):
    """Memory and code sleeping 0.3s each must cost ~0.3s, not 0.6s."""
    arm_code_lane(monkeypatch)

    async def slow(_prompt, **_kw):
        await asyncio.sleep(0.3)
        return []

    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "5")
    started = time.monotonic()
    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall=slow)
    wall = time.monotonic() - started

    assert sorted(run.calls) == sorted(CODE_SCOPES), run.calls
    assert wall < 0.9, f"requests ran back to back: {wall:.2f}s for 2 x 0.3s"
    per_scope = run.detail("context_lookup_empty")["per_scope"]
    assert_valid_per_scope(per_scope, CODE_SCOPES)
    assert not any(record.get("skipped") for record in per_scope.values()), per_scope
    assert all(record["elapsed_ms"] >= 250 for record in per_scope.values()), per_scope


def test_the_sdk_call_carries_the_scope_and_query_type(lookup, monkeypatch):
    """The wire the SDK branch speaks: one graph-scope HYBRID_COMPLETION request
    with only_context and the session id, and nothing from the retired scopes."""
    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall={})

    assert run.calls == ["graph"], run.calls
    memory = run.kwargs["graph"]
    assert memory["scope"] == ["graph"]
    assert memory["query_type"] == "HYBRID_COMPLETION"
    assert memory["only_context"] is True
    assert memory["session_id"] == "sid"
    assert "context_profile" not in memory, memory


def test_the_code_lane_carries_its_dataset_and_query_on_the_sdk_wire(lookup, monkeypatch):
    lane = arm_code_lane(monkeypatch)
    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall={})

    assert sorted(run.calls) == sorted(CODE_SCOPES), run.calls
    code = run.kwargs["code"]
    assert code["datasets"] == [lane["dataset"]]
    assert code["code_query"] == CODE_LANE["code_query"]
    assert code["query_type"] is None
    memory = run.kwargs["graph"]
    assert "datasets" not in memory and "code_query" not in memory, memory


def test_one_raising_request_does_not_drop_the_other(lookup, monkeypatch):
    arm_code_lane(monkeypatch)

    async def flaky(_prompt, **kw):
        scope = kw["scope"][0]
        if scope == "code":
            raise RuntimeError("code graph exploded")
        return [MEMORY]

    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall=flaky)

    detail = run.detail("context_lookup_hit")
    assert detail is not None, run.events
    assert detail["counts"]["graph_context"] == 1
    assert detail["counts"]["code"] == 0
    errors = [d for e, d in run.events if e == "recall_error"]
    assert [d["scope"] for d in errors] == [["code"]], errors
    assert "graph fact" in _context(run)


def test_a_request_past_the_deadline_is_cut_while_the_other_lands(lookup, monkeypatch):
    """The shared deadline bounds the slowest coroutine; nothing waits on it."""
    arm_code_lane(monkeypatch)

    async def one_hangs(_prompt, **kw):
        if kw["scope"][0] == "graph":
            await asyncio.sleep(5)
            return [{"source": "graph", "text": "too late"}]
        return [CODE_FACT]

    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "0.4")
    started = time.monotonic()
    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall=one_hangs)
    wall = time.monotonic() - started

    assert wall < 3.0, f"the hung request held the prompt: {wall:.2f}s"
    errors = [d for e, d in run.events if e == "recall_error"]
    assert len(errors) == 1 and errors[0]["scope"] == ["graph"], errors
    assert errors[0]["verdict"] == "slow", errors
    per_scope = run.detail("context_lookup_hit")["per_scope"]
    assert 350 <= per_scope["graph"]["elapsed_ms"] < 2500, per_scope
    context = _context(run)
    assert "validate_card" in context
    assert "too late" not in context


def test_results_are_folded_in_canonical_order_whatever_finishes_first(lookup, monkeypatch):
    """Memory answers first here and code last; code facts still lead."""
    arm_code_lane(monkeypatch)
    delays = {"graph": 0.0, "code": 0.2}

    async def staggered(_prompt, **kw):
        scope = kw["scope"][0]
        await asyncio.sleep(delays[scope])
        return {"graph": [MEMORY], "code": [CODE_FACT]}[scope]

    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall=staggered)
    context = _context(run)
    assert context.index("=== Code graph facts ===") < context.index("=== Cognee memory ==="), (
        context
    )
    per_scope = run.detail("context_lookup_hit")["per_scope"]
    assert list(per_scope) == list(CODE_SCOPES), per_scope


def test_a_plain_prompt_makes_exactly_one_sdk_call(lookup, monkeypatch):
    run = drive_recall(lookup, monkeypatch, mode="local_sdk", sdk_recall={"graph": [MEMORY]})
    assert run.calls == list(SCOPES), run.calls
    per_scope = run.detail("context_lookup_hit")["per_scope"]
    assert_valid_per_scope(per_scope, SCOPES)
    assert "graph fact" in _context(run)
