"""``elapsed_ms`` on the recall events (#3676).

Latency is the plugin's main user-visible cost: it sits on every prompt, and the
only record of how long it took is what the hooks log. These pin the timing
fields so a latency regression is diagnosable from hook.log alone, and — just as
important — that a miss carries a timing too.

Contract:
  * ``elapsed_ms`` is monotonic-based, whole-integer, never negative;
  * ``context_lookup_hit`` and ``context_lookup_empty`` both carry it, without
    dropping the fields they already had.

(The legacy document bridge's ``http_bridge_poll`` / failed-submit timings used
to be pinned here too; that bridge is gone.)

Gated by capability rather than by probe, and the two halves differ:

* the **helper** half runs on both suites — codex gained ``elapsed_ms`` in the
  port that landed in main;
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
