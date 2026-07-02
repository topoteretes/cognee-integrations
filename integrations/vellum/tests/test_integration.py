"""Deterministic integration tests for the Vellum nodes and Agent Node tools.

cognee's ``remember`` / ``recall`` are mocked, so these run in CI with no LLM,
no API key and no live database — they prove the nodes map Vellum inputs to the
right cognee call and cognee's result back to typed node outputs (including
citations), and that remember is synchronous by default.
"""

import cognee
import pytest
from cognee_integration_vellum import (
    CogneeRecallNode,
    CogneeRememberNode,
    cognee_recall,
    cognee_remember,
)
from vellum.workflows.state.base import BaseState

# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class _FakeRememberResult:
    status = "completed"
    pipeline_run_id = "run-123"
    error = None
    dataset_name = "support"


class _FakeGraphEntry:
    """Mirrors a cognee recall() 'graph' entry (SearchResultItem shape)."""

    source = "graph"
    text = "Einstein was born in Ulm."
    content = None
    dataset_name = "support"
    dataset_id = "ds-1"
    score = 0.91
    metadata = {"chunk_id": "c1", "doc_id": "d1"}

    def model_dump(self):
        return {
            "source": self.source,
            "text": self.text,
            "dataset_name": self.dataset_name,
            "metadata": self.metadata,
        }


class _FakeQAEntry:
    """Mirrors a cognee recall() 'session' QA entry, whose answer lives in
    ``.answer`` (not ``.content``/``.text``)."""

    source = "session"
    answer = "The capital of France is Paris."
    question = "What is the capital of France?"
    qa_id = "qa-1"
    content = None
    text = None

    def model_dump(self):
        return {"source": self.source, "answer": self.answer, "qa_id": self.qa_id}


@pytest.fixture
def capture_remember(monkeypatch):
    calls = {}

    async def _fake(data, **kwargs):
        calls["data"] = data
        calls["kwargs"] = kwargs
        return _FakeRememberResult()

    monkeypatch.setattr(cognee, "remember", _fake)
    return calls


@pytest.fixture
def capture_recall(monkeypatch):
    calls = {}

    async def _fake(query_text, **kwargs):
        calls["query_text"] = query_text
        calls["kwargs"] = kwargs
        return [_FakeGraphEntry()]

    monkeypatch.setattr(cognee, "recall", _fake)
    return calls


# --------------------------------------------------------------------------- #
# CogneeRememberNode
# --------------------------------------------------------------------------- #


def test_remember_node_is_sync_by_default_and_surfaces_status(capture_remember):
    class Node(CogneeRememberNode):
        data = "Einstein was born in Ulm."
        dataset_name = "support"
        user_id = "alice"

    out = Node(state=BaseState()).run()

    # sync by default
    assert capture_remember["kwargs"]["run_in_background"] is False
    # per-user scope maps user_id -> node_set
    assert capture_remember["kwargs"]["node_set"] == ["alice"]
    assert capture_remember["kwargs"]["dataset_name"] == "support"
    # cognee's terminal status/ids surface on typed outputs
    assert out.status == "completed"
    assert out.pipeline_run_id == "run-123"
    assert out.error == ""


def test_remember_node_background_opt_in(capture_remember):
    class Node(CogneeRememberNode):
        data = "some large batch"
        run_in_background = True

    Node(state=BaseState()).run()
    assert capture_remember["kwargs"]["run_in_background"] is True


# --------------------------------------------------------------------------- #
# CogneeRecallNode
# --------------------------------------------------------------------------- #


def test_recall_node_returns_answer_and_typed_citations(capture_recall):
    class Node(CogneeRecallNode):
        query = "Where was Einstein born?"
        dataset_name = "support"
        user_id = "alice"

    out = Node(state=BaseState()).run()

    # references are requested so citations carry source lineage
    assert capture_recall["kwargs"]["include_references"] is True
    assert capture_recall["kwargs"]["node_name"] == ["alice"]
    assert capture_recall["kwargs"]["datasets"] == ["support"]

    assert "Einstein was born in Ulm." in out.answer
    assert len(out.citations) == 1
    citation = out.citations[0]
    assert citation["source"] == "graph"
    assert citation["dataset_name"] == "support"
    assert citation["metadata"]["chunk_id"] == "c1"
    assert citation["metadata"]["doc_id"] == "d1"


def test_recall_captures_session_qa_answer():
    """A session QA entry stores its answer in ``.answer`` (not content/text) —
    it must still surface in the recalled answer and carry its qa_id citation."""
    from cognee_integration_vellum.client import extract_answer_and_citations

    answer, citations = extract_answer_and_citations([_FakeQAEntry()])

    assert "The capital of France is Paris." in answer
    assert citations[0]["source"] == "session"
    assert citations[0]["qa_id"] == "qa-1"


# --------------------------------------------------------------------------- #
# Agent Node tools
# --------------------------------------------------------------------------- #


def test_tool_cognee_remember(capture_remember):
    result = cognee_remember("hello", dataset_name="support", user_id="bob")
    assert result["status"] == "completed"
    assert capture_remember["kwargs"]["node_set"] == ["bob"]


def test_tool_cognee_recall(capture_recall):
    answer = cognee_recall("Where was Einstein born?", dataset_name="support")
    assert "Einstein was born in Ulm." in answer
