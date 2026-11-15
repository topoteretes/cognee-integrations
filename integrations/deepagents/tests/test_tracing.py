"""CogneeTraceMiddleware: every step recorded, nothing breaks the agent."""

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from cognee_integration_deepagents.tracing import (
    CogneeTraceMiddleware,
    persist_thread,
    with_cognee_tracing,
)

from cognee.memory.entries import QAEntry, TraceEntry


@pytest.fixture()
def cognee_stub(monkeypatch):
    """A cognee whose remember/improve/recall calls are captured, never run."""
    import cognee

    calls = {"remember": [], "improve": [], "recall": []}

    async def fake_remember(entry, dataset_name="main_dataset", **kwargs):
        calls["remember"].append({"entry": entry, "dataset": dataset_name, **kwargs})
        return SimpleNamespace(items=[])

    async def fake_recall(query, *args, **kwargs):
        calls["recall"].append({"query": query, **kwargs})
        return [SimpleNamespace(text="search_docs times out past 200 chars")]

    async def fake_improve(dataset, **kwargs):
        calls["improve"].append({"dataset": dataset, **kwargs})
        return SimpleNamespace()

    monkeypatch.setattr(cognee, "remember", fake_remember)
    monkeypatch.setattr(cognee, "recall", fake_recall)
    monkeypatch.setattr(cognee, "improve", fake_improve)
    return calls


@pytest.fixture()
def thread_id(monkeypatch):
    """Pin the LangGraph thread the middleware derives its session from."""
    import langgraph.config

    monkeypatch.setattr(
        langgraph.config,
        "get_config",
        lambda: {"configurable": {"thread_id": "thread-1"}},
        raising=False,
    )
    return "thread-1"


def _tool_request(name="search_docs", args=None):
    return SimpleNamespace(call={"name": name, "args": args or {"query": "cognee"}})


async def _handler(result):
    async def call(_request):
        return result

    return call


async def test_tool_call_is_recorded_as_a_trace(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware(dataset_name="research_agent")
    result = SimpleNamespace(content="found 3 documents", status="success")

    returned = await middleware.awrap_tool_call(
        _tool_request(), await _handler(result)
    )

    assert returned is result
    assert len(cognee_stub["remember"]) == 1
    call = cognee_stub["remember"][0]
    assert isinstance(call["entry"], TraceEntry)
    assert call["entry"].origin_function == "search_docs"
    assert call["entry"].status == "success"
    assert call["entry"].method_return_value == "found 3 documents"
    assert call["dataset"] == "research_agent"
    assert call["session_id"] == "thread-1"


async def test_failed_tool_records_the_error(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware()
    result = SimpleNamespace(content="connection refused", status="error")

    await middleware.awrap_tool_call(_tool_request(), await _handler(result))

    entry = cognee_stub["remember"][0]["entry"]
    assert entry.status == "error"
    assert entry.error_message == "connection refused"
    assert entry.method_return_value is None


async def test_excluded_tools_are_not_recorded(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware(exclude_tools=("secret_tool",))
    result = SimpleNamespace(content="hidden", status="success")

    await middleware.awrap_tool_call(
        _tool_request(name="secret_tool"), await _handler(result)
    )

    assert cognee_stub["remember"] == []


async def test_redact_hook_runs_before_storage(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware(redact=lambda text: text.replace("sk-", "[redacted]"))
    result = SimpleNamespace(content="key sk-123", status="success")

    await middleware.awrap_tool_call(_tool_request(), await _handler(result))

    assert cognee_stub["remember"][0]["entry"].method_return_value == "key [redacted]123"


async def test_long_results_are_truncated(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware(max_result_chars=10)
    result = SimpleNamespace(content="x" * 100, status="success")

    await middleware.awrap_tool_call(_tool_request(), await _handler(result))

    stored = cognee_stub["remember"][0]["entry"].method_return_value
    assert stored.startswith("x" * 10)
    assert "chars dropped" in stored


async def test_recall_before_the_run_injects_a_system_message(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware()
    state = {"messages": [HumanMessage("Summarize the docs")]}

    update = await middleware.abefore_agent(state, None)

    assert cognee_stub["recall"][0]["query"] == "Summarize the docs"
    assert cognee_stub["recall"][0]["session_id"] == "thread-1"
    message = update["messages"][0]
    assert isinstance(message, SystemMessage)
    assert "times out past 200 chars" in message.content


async def test_recall_failure_never_breaks_the_agent(cognee_stub, thread_id, monkeypatch):
    import cognee

    async def failing_recall(*args, **kwargs):
        raise ConnectionError("server down")

    monkeypatch.setattr(cognee, "recall", failing_recall)
    middleware = CogneeTraceMiddleware()
    state = {"messages": [HumanMessage("Summarize the docs")]}

    assert await middleware.abefore_agent(state, None) is None


async def test_final_turn_is_stored_as_a_qa_entry(cognee_stub, thread_id):
    middleware = CogneeTraceMiddleware()
    state = {
        "messages": [
            HumanMessage("Summarize the docs"),
            AIMessage("The docs cover ingestion and recall."),
        ]
    }

    await middleware.aafter_agent(state, None)

    call = cognee_stub["remember"][0]
    assert isinstance(call["entry"], QAEntry)
    assert call["entry"].question == "Summarize the docs"
    assert call["entry"].answer == "The docs cover ingestion and recall."


def test_with_cognee_tracing_copies_specs_and_never_mutates(cognee_stub):
    spec: dict = {"name": "researcher", "description": "d", "middleware": []}
    original = list(spec["middleware"])

    traced = with_cognee_tracing([spec])

    assert spec["middleware"] == original
    assert len(traced[0]["middleware"]) == 1
    assert isinstance(traced[0]["middleware"][0], CogneeTraceMiddleware)
    assert traced[0]["name"] == "researcher"


async def test_persist_thread_runs_improve_for_the_session(cognee_stub):
    await persist_thread("thread-1", dataset_name="research_agent")

    assert cognee_stub["improve"] == [{"dataset": "research_agent", "session_ids": ["thread-1"]}]
