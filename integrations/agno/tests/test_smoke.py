import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

TOOLS_COGNEE = "cognee_integration_agno.tools.cognee"


def _call_sync(toolkit, name, **arguments):
    from agno.tools.function import FunctionCall

    call = FunctionCall(function=toolkit.functions[name], arguments=arguments)
    assert call.execute().status == "success"
    return call.result


def _call_async(toolkit, name, **arguments):
    from agno.tools.function import FunctionCall

    call = FunctionCall(function=toolkit.async_functions[name], arguments=arguments)
    assert asyncio.run(call.aexecute()).status == "success"
    return call.result


def test_imports():
    from cognee_integration_agno import CogneeTools, render_results, run_cognee_task

    assert CogneeTools is not None
    assert render_results is not None
    assert run_cognee_task is not None


def test_toolkit_registers_sync_and_async_functions_under_the_same_names():
    from cognee_integration_agno import CogneeTools

    toolkit = CogneeTools()
    expected = ["cognee_remember", "cognee_search", "cognee_forget"]
    assert list(toolkit.functions) == expected
    assert list(toolkit.async_functions) == expected


def test_toolkit_is_usable_by_an_agent_with_docstring_schemas():
    from agno.agent import Agent
    from agno.models.openai import OpenAIChat
    from cognee_integration_agno import CogneeTools

    toolkit = CogneeTools()
    Agent(model=OpenAIChat(id="gpt-4o-mini", api_key="dummy"), tools=[toolkit])

    params = {}
    for name, function in toolkit.functions.items():
        function.process_entrypoint()
        assert function.description
        params[name] = function.parameters["required"]
    assert params == {"cognee_remember": ["data"], "cognee_search": ["query"], "cognee_forget": []}


def test_toolkit_options_pass_through_to_agno():
    from cognee_integration_agno import CogneeTools

    read_only = CogneeTools(include_tools=["cognee_search"])
    assert list(read_only.functions) == ["cognee_search"]
    assert list(read_only.async_functions) == ["cognee_search"]

    guarded = CogneeTools(requires_confirmation_tools=["cognee_forget"])
    for functions in (guarded.functions, guarded.async_functions):
        assert {n: f.requires_confirmation for n, f in functions.items()} == {
            "cognee_remember": False,
            "cognee_search": False,
            "cognee_forget": True,
        }


def test_default_dataset_reads_cognee_project_node_set_env_var():
    from cognee_integration_agno import CogneeTools

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeTools()._dataset == "my-project"
        assert CogneeTools(dataset="other")._dataset == "other"

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        assert CogneeTools()._dataset is None


def test_remember_sync_and_async_call_cognee_with_dataset_scope():
    from cognee_integration_agno import CogneeTools

    toolkit = CogneeTools(dataset="demo")
    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.remember = AsyncMock()
        assert _call_sync(toolkit, "cognee_remember", data="one") == "Stored in memory."
        assert _call_async(toolkit, "cognee_remember", data="two") == "Stored in memory."

    assert mock_cognee.remember.await_args_list == [
        (("one",), {"dataset_name": "demo"}),
        (("two",), {"dataset_name": "demo"}),
    ]


def test_search_sync_and_async_call_recall_with_dataset_scope_and_top_k():
    from cognee_integration_agno import CogneeTools

    toolkit = CogneeTools(dataset="demo", top_k=7)
    hits = [
        SimpleNamespace(source="graph", text="hit one"),
        SimpleNamespace(source="graph", text="hit two"),
    ]
    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.recall = AsyncMock(return_value=hits)
        assert _call_sync(toolkit, "cognee_search", query="q") == "hit one\nhit two"
        assert _call_async(toolkit, "cognee_search", query="q") == "hit one\nhit two"

    for call in mock_cognee.recall.await_args_list:
        assert call == (("q",), {"datasets": ["demo"], "top_k": 7})


def test_search_reports_no_memories_found():
    from cognee_integration_agno import CogneeTools

    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.recall = AsyncMock(return_value=[])
        assert _call_sync(CogneeTools(), "cognee_search", query="q") == "No memories found."


def test_forget_is_scoped_to_a_concrete_dataset_even_when_unconfigured():
    from cognee_integration_agno import CogneeTools

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        toolkit = CogneeTools()
    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.forget = AsyncMock()
        result = _call_sync(toolkit, "cognee_forget")

    # Never an unscoped wipe: an agent-triggered forget must not reach past
    # its own dataset just because nothing was configured.
    mock_cognee.forget.assert_awaited_once_with(dataset="main_dataset")
    assert "main_dataset" in result


def test_failures_are_returned_as_text_not_raised():
    from cognee_integration_agno import CogneeTools

    toolkit = CogneeTools(dataset="demo")
    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.remember = AsyncMock(side_effect=RuntimeError("boom"))
        mock_cognee.recall = AsyncMock(side_effect=RuntimeError("boom"))
        mock_cognee.forget = AsyncMock(side_effect=RuntimeError("boom"))
        assert "boom" in _call_sync(toolkit, "cognee_remember", data="x")
        assert "boom" in _call_async(toolkit, "cognee_search", query="x")
        assert "boom" in _call_sync(toolkit, "cognee_forget")


def test_concurrent_async_remembers_are_serialised():
    from cognee_integration_agno import CogneeTools

    active = 0
    peak = 0

    async def slow_remember(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    toolkit = CogneeTools()

    async def run_three():
        await asyncio.gather(
            toolkit.acognee_remember("a"),
            toolkit.acognee_remember("b"),
            toolkit.acognee_remember("c"),
        )

    with patch(TOOLS_COGNEE) as mock_cognee:
        mock_cognee.remember = slow_remember
        asyncio.run(run_three())

    assert peak == 1


def test_render_results_handles_each_source():
    from cognee_integration_agno import render_results

    results = [
        SimpleNamespace(source="graph", text="graph hit"),
        SimpleNamespace(source="session", answer="ans", question="q"),
        SimpleNamespace(source="graph_context", content="ctx"),
        SimpleNamespace(source="trace", memory_context="trace blob"),
    ]
    assert render_results(results) == ["graph hit", "ans", "ctx", "trace blob"]
    assert render_results(None) == []
    assert render_results([]) == []
