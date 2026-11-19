"""Per-prompt recall receipts (SDK-1098).

Every prompt that reaches recall appends one body-free receipt to the
session's ``recall-receipts.jsonl``: an id, the prompt's hash, the server, what
each scope did, and a recorded decision for every candidate entry. These tests
drive the real hook and read the file it writes.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

KEY = "receipt-session"
PROMPT = "how does the dataset switch work?"


@pytest.fixture
def lookup(suite, hook_module, monkeypatch):
    if suite.name != "claude-code":
        pytest.skip("Claude Code recall receipts")
    module = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(module, "get_session_key", lambda: KEY)
    return module


@pytest.fixture
def evidence(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude Code recall receipts")
    return isolated_modules(suite, "_evidence")


def passages(count: int, size: int = 400) -> dict:
    body = "## Relevant passages\n" + "\n---\n".join(
        f"passage {i}: " + chr(ord("a") + i % 26) * size for i in range(count)
    )
    return {"graph": [{"source": "graph", "text": body}]}


def audit_lines() -> list:
    path = Path.home() / ".cognee-plugin" / "claude-code" / "recall-audit.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_one_prompt_appends_one_body_free_receipt(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    drive_recall(lookup, monkeypatch, recall=passages(3), prompt=PROMPT)
    path = evidence.receipts_path(KEY)
    raw = path.read_text(encoding="utf-8")
    assert len(raw.splitlines()) == 1
    receipt = json.loads(raw)
    assert len(receipt["receipt_id"]) == 32
    assert receipt["prompt_hash"] == evidence.prompt_hash(PROMPT)
    assert receipt["server_url"] == "https://cloud.example"
    assert receipt["session_id"] == "sid"
    graph = receipt["per_scope"]["graph"]
    assert (graph["status"], graph["retrieved"]) == ("ok", 1) and graph["elapsed_ms"] >= 0
    assert set(receipt["per_scope"]) == {"graph", "session_context"}
    items = receipt["items"]
    assert items and all(item["reason"] == "injected" for item in items)
    for item in items:
        assert {"hash", "source", "section", "chars", "reason"} <= set(item)
        assert "text" not in item and "citation" not in item
        assert item["section"] == "passages" and item["chars"] > 0
    # Neither the prompt nor any recalled text is stored.
    assert PROMPT not in raw and "passage 0" not in raw
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_every_prompt_appends_and_repeats_are_already_in_context(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    drive_recall(lookup, monkeypatch, recall=passages(2), prompt=PROMPT)
    stats = sys.modules["_session_stats"]  # imported by the hook on its first recall
    injected = stats.read(KEY)["entries_injected"]
    drive_recall(lookup, monkeypatch, recall=passages(2), prompt="and the session sync?")
    first, second = evidence.read_receipts(KEY)
    assert first["receipt_id"] != second["receipt_id"]
    assert first["prompt_hash"] != second["prompt_hash"]
    assert [i["hash"] for i in second["items"]] == [i["hash"] for i in first["items"]]
    assert {i["reason"] for i in second["items"]} == {"already_in_context"}
    assert stats.read(KEY)["entries_injected"] == injected


def test_over_budget_entries_are_recorded_and_match_the_audit_line(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    monkeypatch.setenv("COGNEE_INJECT_MAX_CHARS", "1024")
    drive_recall(lookup, monkeypatch, recall=passages(6), prompt=PROMPT)
    receipt = evidence.last_receipt(KEY)
    reasons = [item["reason"] for item in receipt["items"]]
    assert "over_budget" in reasons and "unaccounted" not in reasons
    audit = audit_lines()[-1]
    assert audit["receipt_id"] == receipt["receipt_id"]
    assert reasons.count("over_budget") == audit["entries_dropped"]
    assert "dropped_entries" not in audit
    counted = sys.modules["_session_stats"].read(KEY)["entries_over_budget"]
    assert counted == audit["entries_dropped"]


def test_a_timed_out_scope_is_named_and_the_output_is_unchanged(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    def recall(_prompt, **kw):
        if kw["scope"] == ["session_context"]:
            raise TimeoutError("The read operation timed out")
        return passages(1)["graph"]

    timed_out = drive_recall(lookup, monkeypatch, recall=recall, prompt=PROMPT)
    scopes = evidence.last_receipt(KEY)["per_scope"]
    assert scopes["session_context"]["status"] == "timeout"
    assert scopes["graph"]["status"] == "ok"
    context = timed_out.output["hookSpecificOutput"]["additionalContext"]
    assert "passage 0" in context and "memory recall timed out" in context
    event = next(d for e, d in timed_out.events if e == "context_lookup_hit")
    assert event["receipt_id"] == evidence.last_receipt(KEY)["receipt_id"]
    assert "dropped_entries" not in event
    # The receipt changes nothing the model sees: a fresh session with no
    # receipt writer gets the same output.
    monkeypatch.setattr(lookup, "get_session_key", lambda: "receipt-session-2")
    monkeypatch.setattr(lookup, "append_receipt", lambda *a, **k: "")
    without = drive_recall(lookup, monkeypatch, recall=recall, prompt=PROMPT)
    assert without.output == timed_out.output


def test_a_receipt_write_failure_never_breaks_recall(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(lookup, "append_receipt", broken)
    run = drive_recall(lookup, monkeypatch, recall=passages(1), prompt=PROMPT)
    assert "passage 0" in run.output["hookSpecificOutput"]["additionalContext"]
    assert any(e == "injection_receipt_failed" for e, _ in run.events)


def test_a_skipped_recall_still_writes_a_receipt(lookup, evidence, monkeypatch):
    from utils.recall import URL, drive_recall

    monkeypatch.setattr(lookup, "cloud_enabled", lambda *_: True)
    drive_recall(
        lookup,
        monkeypatch,
        recall=lambda *a, **kw: pytest.fail("recall must be skipped"),
        prior_state={"state": "unreachable", "base_url": URL},
        prompt=PROMPT,
    )
    receipt = evidence.last_receipt(KEY)
    assert receipt["skipped"] == "skipped_outage"
    assert receipt["items"] == []
    assert {s["status"] for s in receipt["per_scope"].values()} == {"skipped_outage"}


def test_an_open_breaker_is_recorded_per_scope(lookup, evidence, monkeypatch):
    from utils.recall import drive_recall

    drive_recall(lookup, monkeypatch, recall=passages(1), breaker_open=(True, 30), prompt=PROMPT)
    receipt = evidence.last_receipt(KEY)
    assert receipt["skipped"] == "skipped_breaker"
    assert {s["status"] for s in receipt["per_scope"].values()} == {"skipped_breaker"}


def test_server_origin_drops_credentials_query_and_fragment(evidence):
    assert (
        evidence.server_origin("https://user:pw@tenant-1.aws.cognee.ai:8443/api/?key=x#y")
        == "https://tenant-1.aws.cognee.ai:8443/api"
    )
    assert evidence.server_origin("http://localhost:8000") == "http://localhost:8000"
    assert evidence.server_origin("not a url") == ""


def test_citation_for_is_the_inverse_of_parse_citation(evidence):
    reference = {
        "dataset_id": "d1",
        "kind": "edge",
        "artifact_id": "a1",
        "version": "v1",
        "source_node_id": "s",
        "target_node_id": "t",
        "relationship_name": "uses",
    }
    assert evidence.parse_citation(evidence.citation_for(reference)) == reference
    assert evidence.citation_for({"dataset_id": "d1"}) == ""


def test_bad_lines_are_skipped_when_reading(evidence):
    path = evidence.receipts_path(KEY)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"receipt_id": "a"}\nnot json\n{"receipt_id": "b"}\n', encoding="utf-8")
    assert [r["receipt_id"] for r in evidence.read_receipts(KEY)] == ["a", "b"]
    assert evidence.last_receipt(KEY)["receipt_id"] == "b"
