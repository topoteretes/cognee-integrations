"""Live end-to-end round trip through the nodes against a real cognee.

Skipped by default (so CI stays keyless/deterministic). Run it with a configured
cognee — Cognee Cloud (``COGNEE_BASE_URL`` + ``COGNEE_API_KEY``) or local cognee
with an ``LLM_API_KEY`` — by setting ``COGNEE_VELLUM_E2E=1``:

    COGNEE_VELLUM_E2E=1 uv run pytest tests/test_e2e.py -v

It stores a fact with ``CogneeRememberNode`` and then retrieves it with
``CogneeRecallNode``, asserting the fact comes back — a genuine remember→recall
round trip through the Vellum node layer.
"""

import os

import pytest

RUN_E2E = os.getenv("COGNEE_VELLUM_E2E") == "1"


@pytest.mark.skipif(
    not RUN_E2E,
    reason="set COGNEE_VELLUM_E2E=1 (with a configured cognee) to run the live round trip",
)
def test_remember_then_recall_round_trip():
    from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode
    from vellum.workflows.state.base import BaseState

    dataset = "vellum_e2e_test"

    class Remember(CogneeRememberNode):
        data = "The Eiffel Tower is located in Paris, France."
        dataset_name = dataset

    remembered = Remember(state=BaseState()).run()
    assert remembered.status in ("completed", "session_stored")
    assert remembered.error == ""

    class Recall(CogneeRecallNode):
        query = "Where is the Eiffel Tower?"
        dataset_name = dataset

    recalled = Recall(state=BaseState()).run()
    assert "Paris" in recalled.answer


@pytest.mark.skipif(
    not RUN_E2E,
    reason="set COGNEE_VELLUM_E2E=1 (with a configured cognee) to run the live round trip",
)
def test_support_assistant_workflow_end_to_end():
    """Run the shipped example workflow (RememberConversation >> AnswerFromMemory)
    against a real cognee: it stores a support conversation and then answers a
    new question from that memory, with citations. This is the real end-to-end
    path — the same one shown in the demo."""
    from examples.support_assistant.workflow import Inputs, SupportAssistantWorkflow

    event = SupportAssistantWorkflow().run(
        inputs=Inputs(
            user_id="user-e2e",
            conversation=(
                "Customer couldn't reset their password; fixed by clearing the auth cache."
            ),
            question="How do I fix a stuck password reset?",
        )
    )

    assert event.name == "workflow.execution.fulfilled", event
    outputs = event.outputs
    assert outputs.answer, "expected a non-empty answer from memory"
    assert "cache" in outputs.answer.lower()
    assert len(outputs.citations) >= 1
