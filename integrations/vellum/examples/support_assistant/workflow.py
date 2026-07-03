"""Support-assistant workflow with cognee memory (Vellum Workflows SDK).

A single graph that first *remembers* a resolved support conversation and then
*answers* a new question from that memory, returning citations to the source
data. The two cognee nodes render as blocks in the Vellum visual editor once
pushed. From ``integrations/vellum`` (with ``VELLUM_API_KEY`` set)::

    vellum workflows push examples.support_assistant

A live run additionally needs a configured cognee — Cognee Cloud
(``COGNEE_BASE_URL`` + ``COGNEE_API_KEY``) or a local cognee with an LLM +
embedding provider set via env. See ``examples/run_support_assistant.py``.

Vellum requires exactly one ``BaseWorkflow`` per ``<module>/workflow.py``, so
this file defines a single ``SupportAssistantWorkflow`` (the nodes it wires are
imported from the installed package, not defined here).
"""

from cognee_integration_vellum import CogneeRecallNode, CogneeRememberNode
from vellum.workflows import BaseWorkflow
from vellum.workflows.inputs import BaseInputs
from vellum.workflows.state.base import BaseState

DATASET = "support"


class Inputs(BaseInputs):
    user_id: str = ""
    conversation: str = ""
    question: str = ""


class RememberConversation(CogneeRememberNode):
    data = Inputs.conversation
    dataset_name = DATASET
    user_id = Inputs.user_id


class AnswerFromMemory(CogneeRecallNode):
    query = Inputs.question
    dataset_name = DATASET
    user_id = Inputs.user_id


class SupportAssistantWorkflow(BaseWorkflow[Inputs, BaseState]):
    """Remember a support conversation, then answer a new question from memory.

    ``CogneeRememberNode`` is synchronous by default, so the graph finishes
    ingesting before ``CogneeRecallNode`` runs — the answer reflects the
    conversation just stored, with ``citations`` pointing back to it.
    """

    graph = RememberConversation >> AnswerFromMemory

    class Outputs(BaseWorkflow.Outputs):
        answer = AnswerFromMemory.Outputs.answer
        citations = AnswerFromMemory.Outputs.citations
