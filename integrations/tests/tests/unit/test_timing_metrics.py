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

Both halves run on every registered suite. The aggregate per-prompt total was
claude-code only until the recall scopes were dispatched concurrently: with the
per-scope timings overlapping instead of adding up, the total stopped being
derivable from ``per_scope`` alone, so codex and antigravity now log it too (the
``has_recall_latency_metric`` flag was retired with that port). Since the
one-request memory contract of cognee 1.6.0 (SDK-741) the only second request
the hook can make is the code lane, so the overlap test arms it. The per-scope
breakdown is asserted for all registered suites in test_recall_per_scope.py.

Migrated from claude-code/tests/test_hook_timing.py, which ran in no CI job on any
platform.
"""

from __future__ import annotations

import time

import pytest
from utils.recall import arm_code_lane, drive_recall


@pytest.fixture
def pc(suite, isolated_modules):
    return isolated_modules(suite, "_plugin_common")


@pytest.fixture
def lookup(suite, hook_module):
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
        lookup,
        monkeypatch,
        recall={"graph": [{"source": "graph", "text": "The question is: `q1`\n\nContext:\n`a1`"}]},
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


def test_the_aggregate_is_the_fan_out_wall_time_not_the_sum_of_scopes(lookup, monkeypatch):
    """Scopes overlap, so the total tracks the slowest scope, not their sum.

    This is the reason every suite now carries the aggregate: two requests of
    0.3s (memory plus the armed code lane) dispatched together cost ~0.3s, and
    only the aggregate can say so — summing ``per_scope`` reads ~0.6s.
    """
    arm_code_lane(monkeypatch)
    sleeps = {"graph": 0.3, "code": 0.3}

    def slow(_prompt, **kw):
        time.sleep(sleeps.get(kw["scope"][0], 0))
        return []

    monkeypatch.setenv("COGNEE_RECALL_BUDGET", "5")
    run = drive_recall(lookup, monkeypatch, recall=slow)

    detail = run.detail("context_lookup_empty")
    summed = sum(r["elapsed_ms"] for r in detail["per_scope"].values())
    assert summed >= 550, detail["per_scope"]
    assert 250 <= detail["elapsed_ms"] < 500, (detail["elapsed_ms"], detail["per_scope"])
