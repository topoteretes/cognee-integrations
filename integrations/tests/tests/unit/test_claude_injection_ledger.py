"""Ledger behavior across repeated recall and host context resets."""

from __future__ import annotations

import json

import pytest


@pytest.fixture
def ledger(suite, isolated_modules):
    if suite.name not in {"claude-code", "codex"}:
        pytest.skip("Claude Code cloud ledger")
    return isolated_modules(suite, "_injection_ledger")


def test_repeat_then_compact_reinjects(ledger):
    sections = [
        {"entries": ["Project uses Postgres.", "Release requires approval."], "priority": 40}
    ]
    first, before, _ = ledger.render("session-a", "Cognee memory", sections, 9000)
    second, after, _ = ledger.render("session-a", "Cognee memory", sections, 9000)
    assert before["entries_kept"] == 2
    assert second == "Cognee memory; 0 new, 2 already in context"
    assert len(second) < len(first)
    assert after["entries_deduped"] == 2
    ledger.reset("session-a")
    assert ledger.render("session-a", "Cognee memory", sections, 9000)[0] == first


def test_guidance_injected_only_when_changed_including_reversion(ledger):
    def render(value):
        return ledger.render("session-a", "header", [{"entries": [value], "priority": 30}], 9000)[1]

    assert render("Use SQLite")["entries_kept"] == 1
    assert render("Use SQLite")["entries_kept"] == 0
    assert render("Use Postgres")["entries_kept"] == 1
    assert render("Use SQLite")["entries_kept"] == 1


def test_only_fitted_entries_are_recorded(ledger):
    sections = [{"entries": ["tiny", "x" * 10000], "priority": 40}]
    ledger.render("session-a", "header", sections, 100)
    data = json.loads((ledger.session_dir("session-a") / "injected.json").read_text())
    assert data["entry_hashes"] == [ledger.entry_hash("tiny")]
    _, stats, _ = ledger.render("session-a", "header", sections, 20000)
    assert stats["entries_kept"] == 1
    assert stats["entries_deduped"] == 1


def test_isolation_normalization_and_corrupt_file(ledger):
    assert ledger.entry_hash("a \n b") == ledger.entry_hash("a b")
    sections = [{"entries": ["fact"], "priority": 40}]
    ledger.render("session-a", "header", sections, 9000)
    assert ledger.render("session-b", "header", sections, 9000)[1]["entries_kept"] == 1
    (ledger.session_dir("session-a") / "injected.json").write_text("{broken")
    assert ledger.render("session-a", "header", sections, 9000)[1]["entries_kept"] == 1
    with pytest.raises(ValueError):
        ledger.reset("../outside")


def test_digest_seed(ledger):
    ledger.seed("session-a", [ledger.entry_hash("fact")])
    _, stats, _ = ledger.render("session-a", "header", [{"entries": ["fact"]}], 9000)
    assert stats["entries_kept"] == 0


def test_deduped_hits_are_not_reported_as_new_injections(suite, hook_module, monkeypatch):
    if suite.name not in {"claude-code", "codex"}:
        pytest.skip("Claude Code cloud ledger")
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "review-dedup")
    recall = {"graph": [{"source": "graph", "text": "One useful fact. " * 40}]}
    first = drive_recall(lookup, monkeypatch, recall=recall)
    second = drive_recall(lookup, monkeypatch, recall=recall)
    assert "recall 1 memory" in first.output["hookSpecificOutput"]["additionalContext"]
    context = second.output["hookSpecificOutput"]["additionalContext"]
    assert "recall 0 memory" in context and "0 new, 1 already in context" in context
    assert second.detail("context_lookup_empty")["retrieved_hits"] == 1
    from pathlib import Path

    marker = json.loads(
        (Path.home() / f".cognee-plugin/{suite.state_subdir}/last_recall.json").read_text()
    )
    assert marker["session_totals"] == {"turns": 2, "turns_with_hits": 1}
    stats = json.loads(
        (
            Path.home() / f".cognee-plugin/{suite.state_subdir}/sessions/review-dedup/stats.json"
        ).read_text()
    )
    assert stats["dedup_measurements"] == 2
    assert stats["dedup_chars_saved"] > 0


def test_savings_compare_bounded_context_not_raw_duplicates(ledger):
    sections = [{"entries": ["useful fact " * 50], "priority": 40}]
    _, first, _ = ledger.render("savings", "header", sections, 9000)
    actual, second, _ = ledger.render("savings", "header", sections, 9000)
    baseline, _ = ledger.fit_sections([{"entries": ["header"], "required": True}] + sections, 9000)
    assert first["dedup_chars_saved"] == 0
    assert second["dedup_chars_saved"] == len(baseline) - len(actual) > 0
    ledger.reset("savings")
    assert ledger.render("savings", "header", sections, 9000)[1]["dedup_chars_saved"] == 0
    huge = "x" * 10000
    ledger.seed("savings", [ledger.entry_hash(huge)])
    assert (
        ledger.render("savings", "header", [{"entries": [huge]}], 100)[1]["dedup_chars_saved"] == 0
    )


def test_savings_account_for_replacement_context(ledger):
    duplicate, fresh = "old " * 50, "fresh " * 40
    ledger.seed("replacement", [ledger.entry_hash(duplicate)])
    sections = [{"entries": [duplicate, fresh], "priority": 40}]
    actual, stats, _ = ledger.render("replacement", "header", sections, 300)
    baseline, _ = ledger.fit_sections([{"entries": ["header"], "required": True}] + sections, 300)
    assert stats["dedup_chars_saved"] == max(0, len(baseline) - len(actual))
    assert stats["dedup_chars_saved"] < len(duplicate)
