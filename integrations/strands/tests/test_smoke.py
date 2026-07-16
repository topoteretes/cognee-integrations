def test_imports():
    from cognee_integration_strands import (
        cognee_tools,
        recall,
        remember,
        render_results,
        run_cognee_task,
    )

    assert remember is not None
    assert recall is not None
    assert render_results is not None
    assert cognee_tools is not None
    assert run_cognee_task is not None


def test_cognee_tools_returns_remember_and_recall():
    from cognee_integration_strands import cognee_tools

    tools = cognee_tools()
    assert len(tools) == 2

    sessioned = cognee_tools("test-session")
    assert len(sessioned) == 2


def test_render_results_handles_each_source():
    from types import SimpleNamespace

    from cognee_integration_strands import render_results

    results = [
        SimpleNamespace(source="graph", text="graph hit"),
        SimpleNamespace(source="session", answer="ans", question="q"),
        SimpleNamespace(source="graph_context", content="ctx"),
        SimpleNamespace(source="trace", memory_context="trace blob"),
    ]
    assert render_results(results) == ["graph hit", "ans", "ctx", "trace blob"]
    assert render_results(None) == []
    assert render_results([]) == []
