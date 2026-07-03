"""Real unit tests for the answer/citation extraction in ``client.py``.

No mocks: these call the actual ``extract_answer_and_citations`` with objects
shaped like cognee's ``recall()`` response entries and assert the answer text
and typed citations it produces. It is pure data-transformation logic — no
cognee, no LLM, no network — so it runs deterministically in CI. The real
remember→recall round trip is covered by the opt-in end-to-end tests in
``test_e2e.py``.
"""

from cognee_integration_vellum.client import extract_answer_and_citations


class _GraphEntry:
    """Shaped like a cognee ``recall()`` graph entry (SearchResultItem):
    answer text in ``.text``, source lineage in ``dataset_*`` / ``metadata``."""

    source = "graph"
    text = "Einstein was born in Ulm."
    content = None
    dataset_name = "support"
    dataset_id = "ds-1"
    score = 0.91
    metadata = {"chunk_id": "c1", "doc_id": "d1"}


class _QAEntry:
    """Shaped like a cognee ``recall()`` session QA entry, whose answer lives in
    ``.answer`` (not ``.content`` / ``.text``)."""

    source = "session"
    answer = "The capital of France is Paris."
    question = "What is the capital of France?"
    qa_id = "qa-1"
    content = None
    text = None


def test_graph_entry_answer_and_typed_citation():
    answer, citations = extract_answer_and_citations([_GraphEntry()])

    assert "Einstein was born in Ulm." in answer
    assert len(citations) == 1
    citation = citations[0]
    assert citation["source"] == "graph"
    assert citation["dataset_name"] == "support"
    # document / chunk lineage is carried under metadata
    assert citation["metadata"]["chunk_id"] == "c1"
    assert citation["metadata"]["doc_id"] == "d1"


def test_session_qa_answer_is_captured_with_qa_id():
    """A session QA entry stores its answer in ``.answer`` (not content/text) —
    it must still surface in the answer and carry its ``qa_id`` citation."""
    answer, citations = extract_answer_and_citations([_QAEntry()])

    assert "The capital of France is Paris." in answer
    assert citations[0]["source"] == "session"
    assert citations[0]["qa_id"] == "qa-1"


def test_mixed_entries_are_joined_with_a_citation_each():
    answer, citations = extract_answer_and_citations([_GraphEntry(), _QAEntry()])

    assert "Einstein was born in Ulm." in answer
    assert "The capital of France is Paris." in answer
    assert len(citations) == 2
