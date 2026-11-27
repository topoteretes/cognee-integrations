import os
from types import SimpleNamespace
from unittest.mock import patch


def test_imports():
    from cognee_integration_smolagents import (
        CogneeForgetTool,
        CogneeRememberTool,
        CogneeSearchTool,
        cognee_tools,
        recall,
        remember,
        render_results,
        run_cognee_task,
    )

    assert CogneeRememberTool is not None
    assert CogneeSearchTool is not None
    assert CogneeForgetTool is not None
    assert cognee_tools is not None
    assert remember is not None
    assert recall is not None
    assert render_results is not None
    assert run_cognee_task is not None


def test_tools_are_valid_smolagents_tools():
    from cognee_integration_smolagents import CogneeForgetTool, CogneeRememberTool, CogneeSearchTool
    from smolagents import Tool

    for cls in (CogneeRememberTool, CogneeSearchTool, CogneeForgetTool):
        tool = cls()
        assert isinstance(tool, Tool)
        assert isinstance(tool.name, str) and tool.name
        assert isinstance(tool.description, str) and tool.description
        assert isinstance(tool.inputs, dict)
        assert tool.output_type == "string"


def test_cognee_tools_returns_three_tools_usable_by_an_agent():
    from cognee_integration_smolagents import cognee_tools
    from smolagents import CodeAgent, OpenAIModel

    tools = cognee_tools(dataset="demo")
    assert [t.name for t in tools] == ["cognee_remember", "cognee_search", "cognee_forget"]

    # Raises if a tool isn't actually agent-usable: this is the whole point
    # of being real Tool subclasses instead of plain functions.
    agent = CodeAgent(
        tools=cognee_tools(), model=OpenAIModel(model_id="gpt-4o-mini", api_key="dummy")
    )
    assert {t.name for t in tools} <= {t.name for t in agent.tools.values()}


def test_default_dataset_reads_cognee_project_node_set_env_var():
    from cognee_integration_smolagents.tools import (
        CogneeForgetTool,
        CogneeRememberTool,
        CogneeSearchTool,
    )

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeRememberTool()._dataset == "my-project"
        assert CogneeSearchTool()._dataset == "my-project"
        assert CogneeForgetTool()._dataset == "my-project"

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        assert CogneeRememberTool()._dataset is None
        assert CogneeSearchTool()._dataset is None
        # forget never resolves to an unscoped None: an agent-triggered wipe
        # must always target a concrete dataset, never "whatever's default".
        assert CogneeForgetTool()._dataset == "main_dataset"


def test_explicit_dataset_overrides_the_env_var():
    from cognee_integration_smolagents.tools import CogneeRememberTool

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeRememberTool(dataset="other")._dataset == "other"


def test_remember_tool_calls_remember_with_dataset_scope():
    from cognee_integration_smolagents.tools import CogneeRememberTool

    with patch("cognee_integration_smolagents.tools.remember") as mock_remember:
        result = CogneeRememberTool(dataset="demo").forward("a fact")

    mock_remember.assert_called_once_with("a fact", dataset_name="demo")
    assert result == "Stored in memory."


def test_remember_tool_reports_errors_as_text_not_raised():
    from cognee_integration_smolagents.tools import CogneeRememberTool

    with patch("cognee_integration_smolagents.tools.remember", side_effect=RuntimeError("boom")):
        result = CogneeRememberTool().forward("a fact")

    assert "boom" in result


def test_search_tool_calls_recall_with_dataset_scope_and_top_k():
    from cognee_integration_smolagents.tools import CogneeSearchTool

    with patch("cognee_integration_smolagents.tools.recall") as mock_recall:
        mock_recall.return_value = [SimpleNamespace(source="graph", text="hit one")]
        result = CogneeSearchTool(dataset="demo", top_k=7).forward("what happened?")

    mock_recall.assert_called_once_with("what happened?", datasets=["demo"], top_k=7)
    assert result == "hit one"


def test_search_tool_reports_no_memories_found():
    from cognee_integration_smolagents.tools import CogneeSearchTool

    with patch("cognee_integration_smolagents.tools.recall", return_value=[]):
        result = CogneeSearchTool().forward("anything")

    assert result == "No memories found."


def test_search_tool_reports_errors_as_text_not_raised():
    from cognee_integration_smolagents.tools import CogneeSearchTool

    with patch("cognee_integration_smolagents.tools.recall", side_effect=RuntimeError("boom")):
        result = CogneeSearchTool().forward("anything")

    assert "boom" in result


def test_forget_tool_scopes_to_its_dataset_and_reports_errors_as_text():
    from cognee_integration_smolagents.tools import CogneeForgetTool

    with patch("cognee_integration_smolagents.tools.forget") as mock_forget:
        result = CogneeForgetTool(dataset="demo").forward()

    mock_forget.assert_called_once_with(dataset="demo")
    assert "demo" in result

    with patch("cognee_integration_smolagents.tools.forget", side_effect=RuntimeError("boom")):
        result = CogneeForgetTool(dataset="demo").forward()
    assert "boom" in result


def test_render_results_handles_each_source():
    from cognee_integration_smolagents import render_results

    results = [
        SimpleNamespace(source="graph", text="graph hit"),
        SimpleNamespace(source="session", answer="ans", question="q"),
        SimpleNamespace(source="graph_context", content="ctx"),
        SimpleNamespace(source="trace", memory_context="trace blob"),
    ]
    assert render_results(results) == ["graph hit", "ans", "ctx", "trace blob"]
    assert render_results(None) == []
    assert render_results([]) == []
