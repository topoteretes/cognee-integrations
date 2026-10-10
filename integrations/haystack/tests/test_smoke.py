import os
from types import SimpleNamespace
from unittest.mock import patch


def test_imports():
    from cognee_integration_haystack import (
        CogneeRetriever,
        CogneeWriter,
        recall,
        remember,
        render_results,
        run_cognee_task,
    )

    assert CogneeRetriever is not None
    assert CogneeWriter is not None
    assert remember is not None
    assert recall is not None
    assert render_results is not None
    assert run_cognee_task is not None


def test_components_are_registered_with_haystack():
    from cognee_integration_haystack import CogneeRetriever, CogneeWriter
    from haystack import Pipeline

    pipeline = Pipeline()
    pipeline.add_component("retriever", CogneeRetriever())
    pipeline.add_component("writer", CogneeWriter())
    assert "retriever" in pipeline.graph.nodes
    assert "writer" in pipeline.graph.nodes


def test_retriever_connects_to_a_downstream_component():
    from cognee_integration_haystack import CogneeRetriever
    from haystack import Pipeline
    from haystack.components.builders import PromptBuilder

    pipeline = Pipeline()
    pipeline.add_component("retriever", CogneeRetriever())
    pipeline.add_component(
        "prompt",
        PromptBuilder(template="{{ documents }}", required_variables=["documents"]),
    )
    # Raises if the documents socket type doesn't match: this is the whole
    # point of being a real component instead of a plain function.
    pipeline.connect("retriever.documents", "prompt.documents")


def test_default_dataset_reads_cognee_project_node_set_env_var():
    from cognee_integration_haystack.components import CogneeRetriever, CogneeWriter

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeRetriever()._datasets == ["my-project"]
        assert CogneeWriter()._dataset_name == "my-project"

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        assert CogneeRetriever()._datasets is None
        assert CogneeWriter()._dataset_name is None


def test_explicit_datasets_override_the_env_var():
    from cognee_integration_haystack.components import CogneeRetriever, CogneeWriter

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeRetriever(datasets=["other"])._datasets == ["other"]
        assert CogneeWriter(dataset_name="other")._dataset_name == "other"


def test_retriever_to_dict_from_dict_round_trip():
    from cognee_integration_haystack import CogneeRetriever

    retriever = CogneeRetriever(datasets=["demo"], top_k=3, recall_kwargs={"session_id": "s1"})
    data = retriever.to_dict()
    restored = CogneeRetriever.from_dict(data)
    assert restored._datasets == ["demo"]
    assert restored._top_k == 3
    assert restored._recall_kwargs == {"session_id": "s1"}


def test_writer_to_dict_from_dict_round_trip():
    from cognee_integration_haystack import CogneeWriter

    writer = CogneeWriter(dataset_name="demo", remember_kwargs={"session_id": "s1"})
    data = writer.to_dict()
    restored = CogneeWriter.from_dict(data)
    assert restored._dataset_name == "demo"
    assert restored._remember_kwargs == {"session_id": "s1"}


def test_retriever_run_calls_recall_with_dataset_scope_and_top_k():
    from cognee_integration_haystack.components import CogneeRetriever

    with patch("cognee_integration_haystack.components.recall") as mock_recall:
        mock_recall.return_value = [SimpleNamespace(source="graph", text="hit one")]
        retriever = CogneeRetriever(datasets=["demo"], top_k=7)
        result = retriever.run("what happened?")

    mock_recall.assert_called_once_with("what happened?", datasets=["demo"], top_k=7)
    assert [d.content for d in result["documents"]] == ["hit one"]


def test_retriever_run_top_k_override_wins_over_default():
    from cognee_integration_haystack.components import CogneeRetriever

    with patch("cognee_integration_haystack.components.recall") as mock_recall:
        mock_recall.return_value = []
        CogneeRetriever(top_k=7).run("q", top_k=2)

    mock_recall.assert_called_once_with("q", top_k=2)


def test_writer_run_calls_remember_per_document_and_skips_empty_content():
    from cognee_integration_haystack.components import CogneeWriter
    from haystack import Document

    docs = [Document(content="first"), Document(content=""), Document(content="second")]
    with patch("cognee_integration_haystack.components.remember") as mock_remember:
        writer = CogneeWriter(dataset_name="demo")
        result = writer.run(docs)

    assert result == {"documents_written": 2}
    assert mock_remember.call_count == 2
    mock_remember.assert_any_call("first", dataset_name="demo")
    mock_remember.assert_any_call("second", dataset_name="demo")


def test_render_results_handles_each_source():
    from cognee_integration_haystack import render_results

    results = [
        SimpleNamespace(source="graph", text="graph hit"),
        SimpleNamespace(source="session", answer="ans", question="q"),
        SimpleNamespace(source="graph_context", content="ctx"),
        SimpleNamespace(source="trace", memory_context="trace blob"),
    ]
    assert render_results(results) == ["graph hit", "ans", "ctx", "trace blob"]
    assert render_results(None) == []
    assert render_results([]) == []
