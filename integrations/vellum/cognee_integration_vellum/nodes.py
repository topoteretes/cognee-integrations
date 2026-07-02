"""Vellum Workflows SDK nodes backed by cognee memory.

Pushed to Vellum (``vellum push``), these render as first-class drag-and-drop
blocks in the visual editor, docstring included.
"""

from typing import Any

from vellum.workflows.nodes import BaseNode

from . import client


class CogneeRememberNode(BaseNode):
    """Store data in cognee memory so later workflow executions can recall it.

    Synchronous by default: the node blocks until cognee finishes building the
    graph, so ``status`` / ``error`` are real the moment the node completes and
    downstream nodes can branch on them. Set ``run_in_background=True`` to
    fire-and-return for large batch ingests (the caller then polls status).
    """

    data: str = ""
    dataset_name: str = client.DEFAULT_DATASET_NAME
    user_id: str = ""
    run_in_background: bool = False

    class Outputs(BaseNode.Outputs):
        status: str
        pipeline_run_id: str
        error: str
        dataset_name: str

    def run(self) -> "CogneeRememberNode.Outputs":
        result = client.run_sync(
            client.remember(
                self.data,
                dataset_name=self.dataset_name,
                user_id=self.user_id or None,
                run_in_background=self.run_in_background,
            )
        )
        return self.Outputs(
            status=getattr(result, "status", "") or "",
            pipeline_run_id=getattr(result, "pipeline_run_id", "") or "",
            error=getattr(result, "error", "") or "",
            dataset_name=getattr(result, "dataset_name", self.dataset_name),
        )


class CogneeRecallNode(BaseNode):
    """Answer from cognee memory, with citations to the source data.

    Surfaces the retrieved ``answer`` plus typed ``citations`` (which
    dataset/document/chunk each result came from) and the full ``results`` list
    as node outputs, so a downstream node can render "answered from ...".
    """

    query: str = ""
    dataset_name: str = client.DEFAULT_DATASET_NAME
    user_id: str = ""
    top_k: int = 15

    class Outputs(BaseNode.Outputs):
        answer: str
        citations: list
        results: list

    def run(self) -> "CogneeRecallNode.Outputs":
        responses = client.run_sync(
            client.recall(
                self.query,
                dataset_name=self.dataset_name,
                user_id=self.user_id or None,
                top_k=self.top_k,
                include_references=True,
            )
        )
        answer, citations = client.extract_answer_and_citations(responses)
        results: list[Any] = [r.model_dump() if hasattr(r, "model_dump") else r for r in responses]
        return self.Outputs(answer=answer, citations=citations, results=results)
