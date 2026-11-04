"""Evidence remains resolvable through budget fitting, dedup and inspection."""

from __future__ import annotations

import json
from argparse import Namespace

import pytest


@pytest.fixture
def evidence(suite, isolated_modules):
    if suite.name != "claude-code":
        pytest.skip("Claude evidence contract")
    return isolated_modules(suite, "_evidence")


def item(version="a", text="Use bounded retries."):
    ref = {
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "kind": "segment",
        "artifact_id": "00000000-0000-0000-0000-000000000002",
        "version": version * 64,
    }
    return {
        "reference": ref,
        "status": "available",
        "text": text,
        "title": "Retry policy",
        "rank": 1,
        "source": {"document_name": "policy.md"},
        "citation": f"cognee://datasets/{ref['dataset_id']}/evidence/segment/{ref['artifact_id']}@{ref['version']}",
    }


def result(items):
    return {
        "source": "graph",
        "text": "Legacy bundle must not replace evidence",
        "metadata": {"evidence_contract": "recall_evidence_v1", "evidence_items": items},
    }


def test_citation_roundtrip_and_unsafe_input(evidence):
    value = item()
    assert evidence.parse_citation(value["citation"]) == value["reference"]
    with pytest.raises(ValueError):
        evidence.parse_citation("https://example.com/unrelated")


def test_injection_is_whole_versioned_artifact_and_receipt_has_no_body(
    evidence, suite, hook_module, monkeypatch
):
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-session")
    value = item()
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([value])]})
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert value["text"] in context and value["citation"] in context
    assert "Legacy bundle" not in context
    receipt_path = evidence.session_dir("evidence-session") / "injection-receipt.json"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["items"][0]["reason"] == "injected"
    assert value["text"] not in receipt_path.read_text()
    again = drive_recall(lookup, monkeypatch, recall={"graph": [result([value])]})
    assert value["citation"] not in again.output["hookSpecificOutput"]["additionalContext"]
    assert json.loads(receipt_path.read_text())["items"][0]["reason"] == "already_in_context"
    updated = item(version="b")  # same text, changed source version is eligible
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([updated])]})
    assert updated["citation"] in run.output["hookSpecificOutput"]["additionalContext"]


def test_over_budget_and_unavailable_are_explained(evidence, suite, hook_module, monkeypatch):
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-budget")
    oversized = item(text="x" * 20000)
    missing = {**item(version="b"), "status": "unavailable", "text": None}
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([oversized, missing])]})
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert oversized["citation"] not in context and "Legacy bundle" not in context
    receipt = json.loads(
        (evidence.session_dir("evidence-budget") / "injection-receipt.json").read_text()
    )
    assert [i["reason"] for i in receipt["items"]] == ["over_budget", "unavailable"]


def test_legacy_context_works_but_does_not_gain_fake_citations(
    evidence, suite, hook_module, monkeypatch
):
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-old-server")
    run = drive_recall(
        lookup, monkeypatch, recall={"graph": [{"source": "graph", "text": "Legacy fact"}]}
    )
    assert "Legacy fact" in run.output["hookSpecificOutput"]["additionalContext"]
    receipt = json.loads(
        (evidence.session_dir("evidence-old-server") / "injection-receipt.json").read_text()
    )
    assert receipt["items"][0]["addressable"] is False


def test_own_session_evidence_stays_excluded(evidence, suite, hook_module, monkeypatch):
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-self")
    value = item(text="Session ID: sid\nQuestion: own turn\nAnswer: private")
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([value])]})
    assert "own turn" not in run.output["hookSpecificOutput"]["additionalContext"]
    receipt = json.loads(
        (evidence.session_dir("evidence-self") / "injection-receipt.json").read_text()
    )
    assert "own_session" in [i["reason"] for i in receipt["items"]]


def test_all_unavailable_evidence_keeps_legacy_text(evidence, suite, hook_module, monkeypatch):
    # Missing or internal graph nodes come back unavailable; with nothing
    # renderable the plain recall text must still reach Claude.
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-unavailable")
    unavailable = {**item(), "status": "unavailable", "text": None}
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([unavailable])]})
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert "Legacy bundle" in context and "recall 1 memory" in context
    assert run.kwargs["graph"]["evidence_mode"] == "context"
    assert all(kw.get("evidence_mode") is None for s, kw in run.kwargs.items() if s != "graph")


def test_evidence_is_requested_only_when_asked(evidence, suite, isolated_modules, monkeypatch):
    # File context, pre-compact and recap reuse recall_via_http on tight
    # deadlines; they must not pay for evidence lookups they never read.
    common = isolated_modules(suite, "_plugin_common")
    payloads = []
    monkeypatch.setattr(
        common, "_json_http_request", lambda path, payload, **kw: payloads.append(payload) or []
    )
    common.recall_via_http("q", session_id="", top_k=5, scope=["graph"], dataset="d")
    common.recall_via_http(
        "q", session_id="", top_k=5, scope=["graph"], dataset="d", evidence_mode="context"
    )
    assert "evidence_mode" not in payloads[0]
    assert payloads[1]["evidence_mode"] == "context"


def test_batch_details_one_request_and_max_five(evidence, suite, hook_module, monkeypatch):
    cli = hook_module(suite, "cognee-evidence.py")
    calls = []
    monkeypatch.setattr(cli, "_json_http_request", lambda *a, **kw: calls.append((a, kw)) or [])
    args = Namespace(command="details", references=None, citations=[item()["citation"]] * 5)
    assert cli.run(args) == [] and len(calls) == 1
    assert len(calls[0][0][1]["references"]) == 5
    assert calls[0][1]["timeout"] == 45.0
    args.citations *= 2
    with pytest.raises(ValueError):
        cli.run(args)
    assert len(calls) == 1


def test_index_old_server_explicit_unsupported(evidence, suite, hook_module, monkeypatch):
    cli = hook_module(suite, "cognee-evidence.py")
    monkeypatch.setattr(cli, "get_session_key", lambda: "evidence-index")
    monkeypatch.setattr(cli, "set_session_key", lambda key: None)
    monkeypatch.setattr(cli, "resolve_active_dataset_ids", lambda: ("memory", []))
    calls = []
    monkeypatch.setattr(
        cli, "recall_via_http", lambda *a, **kw: calls.append(kw) or [{"text": "old"}]
    )
    args = Namespace(command="index", session_key=None, query="policy", top_k=5, dataset=None)
    assert cli.run(args)["status"] == "unsupported"
    assert calls[0]["evidence_mode"] == "index" and calls[0]["timeout"] == 45.0


def test_unsupported_storage_keeps_legacy_recall(evidence, suite, hook_module, monkeypatch):
    from utils.recall import drive_recall

    lookup = hook_module(suite, "session-context-lookup.py")
    monkeypatch.setattr(lookup, "get_session_key", lambda: "evidence-unsupported")
    unsupported = {**item(), "status": "unsupported", "text": None}
    run = drive_recall(lookup, monkeypatch, recall={"graph": [result([unsupported])]})
    context = run.output["hookSpecificOutput"]["additionalContext"]
    assert "Legacy bundle" in context and unsupported["citation"] not in context


def test_companion_index_includes_primary_within_same_deadline(
    evidence, suite, isolated_modules, monkeypatch
):
    common = isolated_modules(suite, "_plugin_common")
    # Resolve the fresh sibling module loaded by the isolated common helper.
    import _project_memory

    monkeypatch.setattr(_project_memory, "route", lambda *a: {"write": "companion"})
    calls = []

    def request(path, payload, **kwargs):
        calls.append((payload, kwargs))
        value = item(version="a" if len(calls) == 1 else "b")
        return {"items": [value], "omitted": 0, "unaddressable_results": 0}

    monkeypatch.setattr(common, "_json_http_request", request)
    answer = common.recall_via_http(
        "policy",
        session_id="",
        top_k=5,
        scope=["graph"],
        dataset="primary",
        evidence_mode="index",
        timeout=45,
    )
    assert len(answer["items"]) == 2 and not answer["partial"]
    assert [call[0]["datasets"] for call in calls] == [["companion"], ["primary"]]
    assert calls[1][1]["timeout"] <= calls[0][1]["timeout"] == 45


def test_merged_index_bounded_and_partial_is_explicit(evidence):
    first = {"items": [item(version="a")], "omitted": 0, "unaddressable_results": 1}
    assert evidence.merge_indexes(first, []) == {**first, "partial": True}
    second = {"items": [item(version=str(i)) for i in range(25)], "omitted": 2}
    merged = evidence.merge_indexes(first, second)
    assert len(merged["items"]) == 20 and merged["omitted"] == 8
    assert merged["unaddressable_results"] == 1
