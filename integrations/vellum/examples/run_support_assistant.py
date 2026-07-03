"""Run the support-assistant workflow locally (no Vellum account needed).

From ``integrations/vellum``::

    uv run python -m examples.run_support_assistant

Requires a configured cognee (Cognee Cloud, or local cognee with an LLM +
embedding provider set via env). Prints the recalled answer plus the citations
that point back to the conversation stored earlier in the same run.
"""

from examples.support_assistant.workflow import Inputs, SupportAssistantWorkflow


def _outputs_or_raise(event):
    """workflow.run() returns a terminal event; raise on rejection so failures
    are loud instead of an AttributeError on a missing ``.outputs``."""
    if event.name != "workflow.execution.fulfilled":
        raise RuntimeError(f"workflow {event.name}: {getattr(event, 'error', None)}")
    return event.outputs


if __name__ == "__main__":
    result = _outputs_or_raise(
        SupportAssistantWorkflow().run(
            inputs=Inputs(
                user_id="user-42",
                conversation=(
                    "Customer couldn't reset their password; fixed by clearing the auth cache."
                ),
                question="How do I fix a stuck password reset?",
            )
        )
    )
    print("answer:", result.answer)
    print("citations:", result.citations)
