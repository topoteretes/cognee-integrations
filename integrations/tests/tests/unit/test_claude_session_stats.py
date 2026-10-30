"""Persistent session value counters and trustworthy summaries."""

from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def stats(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude Code stats")
    return isolated_modules(suite, "_session_stats")


def test_concurrent_writers_do_not_lose_saves(stats, monkeypatch):
    # Fifty read-modify-write cycles, each with an fsync, queue up behind one
    # lock; on a slow Windows runner that is longer than the production wait
    # (which caps how long a store hook may block), and the writer that gave up
    # reported a lost save that was only a late one. The test is about the
    # count, so the wait is lifted out of the way.
    monkeypatch.setattr(stats, "_LOCK_TIMEOUT_SECONDS", 120.0)
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert all(pool.map(lambda _: stats.update("one", saves=1), range(50)))
    assert stats.read("one")["saves"] == 50
    assert stats.read("two") == {}


def test_last_completed_session_same_project_and_tenant(stats):
    stats.update(
        "old",
        saves=3,
        recalls=4,
        hits=2,
        improves_landed=1,
        metadata={"dataset": "project", "server_url": "tenant", "ended_at": 1},
    )
    stats.update(
        "other", saves=999, metadata={"dataset": "other", "server_url": "tenant", "ended_at": 2}
    )
    stats.update("active", saves=999, metadata={"dataset": "project", "server_url": "tenant"})
    previous = stats.previous("new", "project", "tenant")
    assert previous["host_session_id"] == "old"
    text = stats.summary(previous)
    assert "saved 3" in text and "2/4 (50%)" in text and "1 improves landed" in text
    assert "unmeasured" in stats.summary({"saves": 0})


def test_report_preserves_counter_meanings(stats):
    stats.update(
        "one", recalls=2, hits=1, entries_injected=3, entries_deduped=2, chars_injected=150
    )
    report = stats.report("one")
    assert report["current_session"]["entries_injected"] == 3
    assert report["totals"]["entries_deduped"] == 2
    assert report["totals"]["saves"] == 0
    assert "not proven use" in report["note"]


def test_bad_state_fails_without_breaking_hooks(stats):
    assert not stats.update("../escape", recalls=1)
    assert not stats.update("", recalls=1)


def test_savings_accumulate_without_losing_precision(stats):
    stats.update("one", dedup_chars_saved=7, dedup_measurements=1)
    stats.update("one", saves=1)
    stats.update("one", dedup_chars_saved=7, dedup_measurements=1)
    result = stats.read("one")
    assert result["dedup_chars_saved"] == 14
    assert result["dedup_measurements"] == 2
    assert result["dedup_chars_saved"] // 4 == 3
    assert stats.read("two").get("dedup_measurements", 0) == 0
