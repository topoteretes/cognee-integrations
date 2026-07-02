"""Thin functions for Vellum's Agent Node.

Registered as custom tools, these let a Vellum agent decide *when* to read or
write cognee memory. They go through the same cognee surface as the nodes
(``client.py``), so there is one place that touches cognee, not two.
"""

from . import client


def cognee_remember(
    data: str,
    dataset_name: str = client.DEFAULT_DATASET_NAME,
    user_id: str = "",
) -> dict:
    """Store text in cognee memory. Returns the ingestion status.

    Args:
        data: The text to remember.
        dataset_name: Target cognee dataset (defaults to the deployment dataset).
        user_id: Optional per-end-user scope.
    """
    result = client.run_sync(
        client.remember(data, dataset_name=dataset_name, user_id=user_id or None)
    )
    return {
        "status": getattr(result, "status", "") or "",
        "error": getattr(result, "error", "") or "",
        "pipeline_run_id": getattr(result, "pipeline_run_id", "") or "",
    }


def cognee_recall(
    query: str,
    dataset_name: str = client.DEFAULT_DATASET_NAME,
    user_id: str = "",
) -> str:
    """Answer a question from cognee memory. Returns the recalled text.

    Args:
        query: The natural-language question.
        dataset_name: Cognee dataset to search (defaults to the deployment dataset).
        user_id: Optional per-end-user scope.
    """
    responses = client.run_sync(
        client.recall(query, dataset_name=dataset_name, user_id=user_id or None)
    )
    answer, _ = client.extract_answer_and_citations(responses)
    return answer
