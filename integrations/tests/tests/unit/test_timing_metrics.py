"""``elapsed_ms`` on the recall and bridge events (#3676).

Latency is the plugin's main user-visible cost: it sits on every prompt, and the
only record of how long it took is what the hooks log. These pin the timing
fields so a latency regression is diagnosable from hook.log alone, and — just as
important — that the *failure* paths carry timings too. A submit that fails slowly
is exactly what one wants to find in a latency log, and it is the case most
easily left out.

Contract:
  * ``elapsed_ms`` is monotonic-based, whole-integer, never negative;
  * the bridge's poll event carries it, and so does the failed-submit event;
  * ``context_lookup_hit`` and ``context_lookup_empty`` both carry it, without
    dropping the fields they already had.

Gated by capability rather than by probe, and the two halves now differ:

* the **helper** and the **bridge** halves run on both suites — codex gained
  ``elapsed_ms`` and the cognify poll in the port that landed in main, and its
  ``http_bridge_poll`` times the submit and the confirm together;
* the **recall** half stays claude-code only: codex logs no aggregate per-prompt
  total, timing each scope inline instead. That per-scope breakdown is asserted
  for both suites in test_recall_per_scope.py.

Migrated from claude-code/tests/test_hook_timing.py, which ran in no CI job on any
platform.
"""

from __future__ import annotations

import time

import pytest
from utils.recall import drive_recall


@pytest.fixture
def pc(suite, isolated_modules):
    if not suite.has_elapsed_ms_helper:
        pytest.skip(f"{suite.name}: no elapsed_ms helper (scopes are timed inline instead)")
    return isolated_modules(suite, "_plugin_common")


@pytest.fixture
def lookup(suite, hook_module):
    if not suite.has_recall_latency_metric:
        pytest.skip(f"{suite.name}: context_lookup events carry no aggregate elapsed_ms")
    return hook_module(suite, "session-context-lookup.py")


# ── the helper ────────────────────────────────────────────────────────────────


def test_elapsed_ms_is_a_non_negative_whole_number(pc):
    """Ints keep the hook.log fields compact and greppable."""
    value = pc.elapsed_ms(time.monotonic())
    assert isinstance(value, int)
    assert value >= 0


def test_elapsed_ms_measures_the_delta_in_milliseconds(pc, monkeypatch):
    """Pinned to a constant clock: 100.25 - 100.0 -> 250ms.

    A constant rather than a fixed-length iterator, so the assertion cannot break
    on how many times the implementation happens to read the clock.
    """
    monkeypatch.setattr(pc.time, "monotonic", lambda: 100.25)
    assert pc.elapsed_ms(100.0) == 250


# ── the bridge ────────────────────────────────────────────────────────────────


@pytest.fixture
def run_bridge(pc, suite, tmp_path, monkeypatch):
    """Drive the bridge with its HTTP seams mocked, capturing hook_log events."""
    if not suite.has_background_remember:
        pytest.skip(f"{suite.name}: the bridge submits synchronously and never polls")

    def _run(outcome="completed", *, post_result=None):
        events: list[tuple[str, dict]] = []
        monkeypatch.setattr(pc, "_local_api_url", lambda: "http://x")
        monkeypatch.setattr(pc, "_backend_reachable", lambda url: True)
        monkeypatch.setattr(pc, "_api_key", lambda: "k")
        monkeypatch.setattr(pc, "_format_cached_bridge_document", lambda ds, sid: ("qa text", ""))
        monkeypatch.setattr(pc, "_bridge_file", lambda sid: tmp_path / "bridge.json")
        monkeypatch.setattr(pc, "_load_json_file", lambda p: {})
        monkeypatch.setattr(pc, "_write_json_file", lambda p, data: None)
        monkeypatch.setattr(
            pc,
            "_post_remember_document",
            lambda *a, **k: post_result
            or {"ok": True, "dataset_id": "d1", "pipeline_run_id": "p1"},
        )
        monkeypatch.setattr(pc, "wait_for_cognify", lambda *a, **k: outcome)
        monkeypatch.setattr(
            pc, "hook_log", lambda event, detail=None: events.append((event, detail or {}))
        )

        pc.persist_session_cache_to_graph_via_http("ds", "sid")
        return events

    return _run


def _detail(events, name):
    for event, detail in events:
        if event == name:
            return detail
    return None


def test_the_bridge_poll_carries_its_elapsed_ms(run_bridge):
    detail = _detail(run_bridge("completed"), "http_bridge_poll")
    assert detail is not None, "expected an http_bridge_poll event"
    assert isinstance(detail.get("elapsed_ms"), int), detail
    assert detail["elapsed_ms"] >= 0

    # The timing field is additive — what was already logged must still be there.
    assert detail["outcome"] == "completed"
    assert detail["dataset_id"] == "d1"


def test_a_failed_submit_still_carries_its_elapsed_ms(run_bridge):
    """A slow failure is the most useful thing in a latency log, not the least."""
    events = run_bridge("completed", post_result={"ok": False, "status": 503})
    detail = _detail(events, "http_bridge_post_failed")
    assert detail is not None, f"expected an http_bridge_post_failed event: {events}"
    assert isinstance(detail.get("elapsed_ms"), int), detail
    assert detail["elapsed_ms"] >= 0


# ── the recall ────────────────────────────────────────────────────────────────


def test_a_recall_hit_carries_its_elapsed_ms(lookup, monkeypatch):
    run = drive_recall(
        lookup, monkeypatch, recall={"session": [{"question": "q1", "answer": "a1"}]}
    )

    detail = run.detail("context_lookup_hit")
    assert detail is not None, f"expected a context_lookup_hit: {run.events}"
    assert isinstance(detail.get("elapsed_ms"), int), detail
    assert detail["elapsed_ms"] >= 0

    # Additive, again: the counters the status line reads must be untouched.
    assert "counts" in detail
    assert "saves_last_turn" in detail


def test_a_recall_miss_carries_its_elapsed_ms(lookup, monkeypatch):
    """A miss has a cost too, and it is the one worth watching."""
    run = drive_recall(lookup, monkeypatch, recall={})

    detail = run.detail("context_lookup_empty")
    assert detail is not None, f"expected a context_lookup_empty: {run.events}"
    assert isinstance(detail.get("elapsed_ms"), int), detail
    assert detail["elapsed_ms"] >= 0
