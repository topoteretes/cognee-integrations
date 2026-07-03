"""Import smoke test — this is what the monorepo CI runs for TS/Py integrations.

Confirms the package installs and its public surface imports cleanly, and that
the nodes are genuine Vellum Workflows SDK nodes.
"""


def test_public_surface_imports():
    from cognee_integration_vellum import (
        CogneeRecallNode,
        CogneeRememberNode,
        cognee_recall,
        cognee_remember,
    )

    assert CogneeRememberNode is not None
    assert CogneeRecallNode is not None
    assert callable(cognee_remember)
    assert callable(cognee_recall)


def test_nodes_are_vellum_base_nodes():
    from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode
    from vellum.workflows.nodes import BaseNode

    assert issubclass(CogneeRememberNode, BaseNode)
    assert issubclass(CogneeRecallNode, BaseNode)


def test_nodes_declare_typed_outputs():
    from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode

    remember_outputs = CogneeRememberNode.Outputs.__annotations__
    assert "status" in remember_outputs
    assert "error" in remember_outputs
    assert "pipeline_run_id" in remember_outputs

    recall_outputs = CogneeRecallNode.Outputs.__annotations__
    assert "answer" in recall_outputs
    assert "citations" in recall_outputs


def test_tools_are_valid_agent_node_tools():
    """A Vellum Agent Node (ToolCallingNode) turns a plain callable into a tool
    via compile_function_definition, inferring the schema from the signature.
    Assert our tools compile cleanly into Vellum FunctionDefinitions with the
    expected name and parameters."""
    from cognee_integration_vellum import cognee_recall, cognee_remember
    from vellum.workflows.utils.functions import compile_function_definition

    remember_def = compile_function_definition(cognee_remember)
    assert remember_def.name == "cognee_remember"
    assert "data" in (remember_def.parameters or {}).get("properties", {})

    recall_def = compile_function_definition(cognee_recall)
    assert recall_def.name == "cognee_recall"
    assert "query" in (recall_def.parameters or {}).get("properties", {})


def test_example_workflow_imports_and_wires_cognee_nodes():
    """The shipped example is a valid Vellum workflow that wires both cognee
    nodes — importable with no cognee/LLM call, so it runs in CI."""
    from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode
    from examples.support_assistant.workflow import (
        AnswerFromMemory,
        RememberConversation,
        SupportAssistantWorkflow,
    )
    from vellum.workflows import BaseWorkflow

    assert issubclass(SupportAssistantWorkflow, BaseWorkflow)
    assert issubclass(RememberConversation, CogneeRememberNode)
    assert issubclass(AnswerFromMemory, CogneeRecallNode)
