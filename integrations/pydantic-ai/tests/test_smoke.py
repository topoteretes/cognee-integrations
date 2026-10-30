"""Exercise the public toolset through real Pydantic AI agent runs."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cognee_integration_pydantic_ai import cognee_toolset
from cognee_integration_pydantic_ai import toolset as integration
from pydantic_ai import Agent, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel


async def run_tool(toolset, name: str, args: dict) -> str:
    """Have a local model call one tool, then return its result to the test."""

    def model(messages, info):
        assert {tool.name for tool in info.function_tools} == {
            "cognee_remember",
            "cognee_search",
            "cognee_forget",
        }
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(name, args)])
        return ModelResponse(parts=[TextPart("done")])

    result = await Agent(FunctionModel(model), toolsets=[toolset]).run("test")
    return str(result.all_messages()[-2].parts[0].content)


@pytest.mark.asyncio
async def test_tools_route_to_scoped_cognee_calls(monkeypatch):
    remember = AsyncMock(return_value=SimpleNamespace(status="completed"))
    recall = AsyncMock(
        return_value=[SimpleNamespace(source="graph", text="Store 12 prefers email")]
    )
    forget = AsyncMock(return_value={})
    monkeypatch.setattr(integration.cognee, "remember", remember)
    monkeypatch.setattr(integration.cognee, "recall", recall)
    monkeypatch.setattr(integration.cognee, "forget", forget)

    scoped = cognee_toolset(user_id="customer-42", session_id="incident-7")
    assert "Stored" in await run_tool(scoped, "cognee_remember", {"data": "Store 12"})
    assert "Store 12 prefers email" in await run_tool(
        scoped, "cognee_search", {"query": "Store 12"}
    )
    assert "deleted" in await run_tool(scoped, "cognee_forget", {})

    dataset = remember.await_args.kwargs["dataset_name"]
    node_set = remember.await_args.kwargs["node_set"][0]
    assert dataset.startswith("pydantic_ai_")
    assert "customer-42" not in dataset
    assert node_set.startswith("pydantic_ai_")
    recall.assert_awaited_once_with("Store 12", datasets=[dataset], top_k=5)
    forget.assert_awaited_once_with(dataset=dataset)


@pytest.mark.asyncio
async def test_scopes_do_not_share_or_delete_each_others_dataset(monkeypatch):
    forget = AsyncMock(return_value={})
    monkeypatch.setattr(integration.cognee, "forget", forget)

    await run_tool(cognee_toolset(user_id="alice"), "cognee_forget", {})
    alice_dataset = forget.await_args.kwargs["dataset"]
    await run_tool(cognee_toolset(user_id="bob"), "cognee_forget", {})
    assert forget.await_args.kwargs["dataset"] != alice_dataset


@pytest.mark.asyncio
async def test_remote_dict_results_and_reported_errors(monkeypatch):
    recall = AsyncMock(return_value=[{"source": "graph", "text": "remote memory"}])
    remember = AsyncMock(return_value={"status": "errored", "error": "ingest failed"})
    monkeypatch.setattr(integration.cognee, "recall", recall)
    monkeypatch.setattr(integration.cognee, "remember", remember)
    scoped = cognee_toolset()

    assert "remote memory" in await run_tool(scoped, "cognee_search", {"query": "test"})
    assert "nothing was stored" in await run_tool(scoped, "cognee_remember", {"data": "test"})


@pytest.mark.asyncio
async def test_auto_recall_injects_fresh_context_before_each_model_call(monkeypatch):
    recall = AsyncMock(
        side_effect=[
            [SimpleNamespace(source="graph", text="first memory")],
            [SimpleNamespace(source="graph", text="second memory")],
        ]
    )
    monkeypatch.setattr(integration.cognee, "recall", recall)
    seen_instructions = []

    def model(messages, info):
        seen_instructions.append(info.instructions)
        return ModelResponse(parts=[TextPart("ok")])

    agent = Agent(FunctionModel(model), toolsets=[cognee_toolset(auto_recall=True)])
    first = await agent.run("first question")
    await agent.run("second question", message_history=first.all_messages())

    assert "first memory" in seen_instructions[0]
    assert "second memory" in seen_instructions[1]
    assert "first memory" not in seen_instructions[1]
    assert [call.args[0] for call in recall.await_args_list] == [
        "first question",
        "second question",
    ]


@pytest.mark.asyncio
async def test_cognee_failures_do_not_fail_agent_run(monkeypatch):
    failure = AsyncMock(side_effect=RuntimeError("service down"))
    monkeypatch.setattr(integration.cognee, "remember", failure)
    monkeypatch.setattr(integration.cognee, "recall", failure)
    monkeypatch.setattr(integration.cognee, "forget", failure)
    scoped = cognee_toolset(auto_recall=True)

    assert "unavailable" in await run_tool(scoped, "cognee_remember", {"data": "a fact"})
    assert "No matching memory" in await run_tool(scoped, "cognee_search", {"query": "a fact"})
    assert "unavailable" in await run_tool(scoped, "cognee_forget", {})


def test_configuration_from_environment(monkeypatch):
    monkeypatch.setenv("COGNEE_PYDANTIC_AI_DATASET", "retail")
    monkeypatch.setenv("COGNEE_PYDANTIC_AI_USER_ID", "alice")
    assert cognee_toolset() is not None
    with pytest.raises(ValueError, match="top_k"):
        cognee_toolset(top_k=0)


@pytest.mark.asyncio
async def test_missing_scope_is_empty_memory(monkeypatch):
    missing = AsyncMock(side_effect=integration.DatasetNotFoundError("missing"))
    monkeypatch.setattr(integration.cognee, "recall", missing)
    monkeypatch.setattr(integration.cognee, "forget", missing)
    scoped = cognee_toolset(user_id="new-user")

    assert "No matching memory" in await run_tool(scoped, "cognee_search", {"query": "test"})
    assert "No scoped memory" in await run_tool(scoped, "cognee_forget", {})
