import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

PLUGIN_COGNEE = "cognee_integration_semantic_kernel.plugin.cognee"


def _kernel_with_plugin(**plugin_kwargs):
    from cognee_integration_semantic_kernel import CogneeMemoryPlugin
    from semantic_kernel import Kernel

    kernel = Kernel()
    kernel.add_plugin(CogneeMemoryPlugin(**plugin_kwargs), plugin_name="cognee")
    return kernel


def _invoke(kernel, function_name, **arguments):
    result = asyncio.run(
        kernel.invoke(plugin_name="cognee", function_name=function_name, **arguments)
    )
    return result.value


def test_imports():
    from cognee_integration_semantic_kernel import CogneeMemoryPlugin, render_results

    assert CogneeMemoryPlugin is not None
    assert render_results is not None


def test_plugin_registers_remember_search_forget_as_kernel_functions():
    kernel = _kernel_with_plugin()
    plugin = kernel.get_plugin("cognee")

    assert set(plugin.functions) == {"remember", "search", "forget"}
    params = {
        name: [p.name for p in fn.metadata.parameters] for name, fn in plugin.functions.items()
    }
    assert params == {"remember": ["data"], "search": ["query"], "forget": []}
    for fn in plugin.functions.values():
        assert fn.metadata.description


def test_default_dataset_reads_cognee_project_node_set_env_var():
    from cognee_integration_semantic_kernel import CogneeMemoryPlugin

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": "my-project"}):
        assert CogneeMemoryPlugin()._dataset == "my-project"
        assert CogneeMemoryPlugin(dataset="other")._dataset == "other"

    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        assert CogneeMemoryPlugin()._dataset is None


def test_remember_via_kernel_calls_cognee_with_dataset_scope():
    kernel = _kernel_with_plugin(dataset="demo")
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.remember = AsyncMock()
        value = _invoke(kernel, "remember", data="a fact")

    mock_cognee.remember.assert_awaited_once_with("a fact", dataset_name="demo")
    assert value == "Stored in memory."


def test_search_via_kernel_calls_recall_with_dataset_scope_and_top_k():
    kernel = _kernel_with_plugin(dataset="demo", top_k=7)
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.recall = AsyncMock(
            return_value=[
                SimpleNamespace(source="graph", text="hit one"),
                SimpleNamespace(source="graph", text="hit two"),
            ]
        )
        value = _invoke(kernel, "search", query="what happened?")

    mock_cognee.recall.assert_awaited_once_with("what happened?", datasets=["demo"], top_k=7)
    assert value == "hit one\nhit two"


def test_search_reports_no_memories_found():
    kernel = _kernel_with_plugin()
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.recall = AsyncMock(return_value=[])
        value = _invoke(kernel, "search", query="anything")

    assert value == "No memories found."


def test_forget_is_scoped_to_a_concrete_dataset_even_when_unconfigured():
    with patch.dict(os.environ, {"COGNEE_PROJECT_NODE_SET": ""}):
        kernel = _kernel_with_plugin()
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.forget = AsyncMock()
        value = _invoke(kernel, "forget")

    # Never an unscoped wipe: an agent-triggered forget must not reach past
    # its own dataset just because nothing was configured.
    mock_cognee.forget.assert_awaited_once_with(dataset="main_dataset")
    assert "main_dataset" in value


def test_failures_are_returned_as_text_not_raised():
    kernel = _kernel_with_plugin(dataset="demo")
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.remember = AsyncMock(side_effect=RuntimeError("boom"))
        mock_cognee.recall = AsyncMock(side_effect=RuntimeError("boom"))
        mock_cognee.forget = AsyncMock(side_effect=RuntimeError("boom"))
        assert "boom" in _invoke(kernel, "remember", data="x")
        assert "boom" in _invoke(kernel, "search", query="x")
        assert "boom" in _invoke(kernel, "forget")


def test_write_lock_works_across_separate_event_loops():
    # Two asyncio.run calls use two different loops; a single module-level
    # asyncio.Lock would be bound to the first and fail in the second.
    kernel = _kernel_with_plugin()
    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.remember = AsyncMock()
        assert _invoke(kernel, "remember", data="one") == "Stored in memory."
        assert _invoke(kernel, "remember", data="two") == "Stored in memory."
    assert mock_cognee.remember.await_count == 2


def test_concurrent_remembers_are_serialised():
    from cognee_integration_semantic_kernel import CogneeMemoryPlugin

    active = 0
    peak = 0

    async def slow_remember(*args, **kwargs):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1

    plugin = CogneeMemoryPlugin()

    async def run_both():
        await asyncio.gather(plugin.remember("a"), plugin.remember("b"), plugin.remember("c"))

    with patch(PLUGIN_COGNEE) as mock_cognee:
        mock_cognee.remember = slow_remember
        asyncio.run(run_both())

    assert peak == 1


def test_render_results_handles_each_source():
    from cognee_integration_semantic_kernel import render_results

    results = [
        SimpleNamespace(source="graph", text="graph hit"),
        SimpleNamespace(source="session", answer="ans", question="q"),
        SimpleNamespace(source="graph_context", content="ctx"),
        SimpleNamespace(source="trace", memory_context="trace blob"),
    ]
    assert render_results(results) == ["graph hit", "ans", "ctx", "trace blob"]
    assert render_results(None) == []
    assert render_results([]) == []
