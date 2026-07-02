"""Support-assistant example: give a Vellum workflow cross-execution memory.

Two tiny workflows share one cognee dataset:

- ``IngestConversationWorkflow`` — stores a resolved support conversation into
  cognee memory (run this at the end of each ticket).
- ``AnswerFromMemoryWorkflow`` — answers a new question using
  ``CogneeRecallNode``, returning the answer plus citations to the prior
  conversations it came from.

Push either with ``vellum push examples/support_assistant_workflow.py`` and the
cognee nodes show up as blocks in the Vellum editor. For the hackathon demo, run
a Vellum Evaluation suite against ``AnswerFromMemoryWorkflow`` with the ingest
step on vs. off and report the score delta.
"""

from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode
from vellum.workflows import BaseWorkflow
from vellum.workflows.inputs import BaseInputs
from vellum.workflows.state.base import BaseState

DATASET = "support"


# --------------------------------------------------------------------------- #
# Ingest a resolved conversation
# --------------------------------------------------------------------------- #


class IngestInputs(BaseInputs):
    user_id: str = ""
    conversation: str = ""


class RememberConversation(CogneeRememberNode):
    data = IngestInputs.conversation
    dataset_name = DATASET
    user_id = IngestInputs.user_id


class IngestConversationWorkflow(BaseWorkflow[IngestInputs, BaseState]):
    graph = RememberConversation

    class Outputs(BaseWorkflow.Outputs):
        status = RememberConversation.Outputs.status
        error = RememberConversation.Outputs.error


# --------------------------------------------------------------------------- #
# Answer a new question from memory, with citations
# --------------------------------------------------------------------------- #


class AnswerInputs(BaseInputs):
    user_id: str = ""
    question: str = ""


class RecallForAnswer(CogneeRecallNode):
    query = AnswerInputs.question
    dataset_name = DATASET
    user_id = AnswerInputs.user_id


class AnswerFromMemoryWorkflow(BaseWorkflow[AnswerInputs, BaseState]):
    graph = RecallForAnswer

    class Outputs(BaseWorkflow.Outputs):
        answer = RecallForAnswer.Outputs.answer
        citations = RecallForAnswer.Outputs.citations


def _outputs_or_raise(event):
    """workflow.run() returns a terminal event; raise on rejection so failures
    are loud instead of an AttributeError on a missing ``.outputs``."""
    if event.name != "workflow.execution.fulfilled":
        raise RuntimeError(f"workflow {event.name}: {getattr(event, 'error', None)}")
    return event.outputs


if __name__ == "__main__":
    # Requires a configured cognee (Cognee Cloud, or local cognee with an LLM +
    # embedding provider set via env).
    ingest = IngestConversationWorkflow().run(
        inputs=IngestInputs(
            user_id="user-42",
            conversation=(
                "Customer couldn't reset their password; fixed by clearing the auth cache."
            ),
        )
    )
    print("ingest:", _outputs_or_raise(ingest).status)

    answer = _outputs_or_raise(
        AnswerFromMemoryWorkflow().run(
            inputs=AnswerInputs(user_id="user-42", question="How do I fix a stuck password reset?")
        )
    )
    print("answer:", answer.answer)
    print("citations:", answer.citations)
