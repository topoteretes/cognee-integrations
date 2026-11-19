import logging
from typing import Any, Dict, List, Optional

from smolagents import Tool

from .runtime import default_dataset, forget, recall, remember, render_results

logger = logging.getLogger(__name__)


class CogneeRememberTool(Tool):
    """Store information in cognee's knowledge graph for later retrieval."""

    name = "cognee_remember"
    description = (
        "Store information in long-term memory for later retrieval. "
        "Use whenever you learn something worth remembering across runs."
    )
    inputs = {"data": {"type": "string", "description": "The text or information to store."}}
    output_type = "string"

    def __init__(
        self, dataset: Optional[str] = None, remember_kwargs: Optional[Dict[str, Any]] = None
    ):
        super().__init__()
        self._dataset = dataset if dataset is not None else default_dataset()
        self._remember_kwargs = dict(remember_kwargs or {})

    def forward(self, data: str) -> str:
        kwargs = dict(self._remember_kwargs)
        if self._dataset:
            kwargs.setdefault("dataset_name", self._dataset)
        try:
            remember(data, **kwargs)
        except (
            Exception
        ) as exc:  # tool errors are reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_remember failed: {exc}")
            return f"Could not store memory: {exc}"
        return "Stored in memory."


class CogneeSearchTool(Tool):
    """Search cognee's knowledge graph for information relevant to a query."""

    name = "cognee_search"
    description = (
        "Search long-term memory for information relevant to a query. "
        "Use to recall anything previously stored with cognee_remember."
    )
    inputs = {"query": {"type": "string", "description": "A natural-language search query."}}
    output_type = "string"

    def __init__(
        self,
        dataset: Optional[str] = None,
        top_k: int = 15,
        recall_kwargs: Optional[Dict[str, Any]] = None,
    ):
        super().__init__()
        self._dataset = dataset if dataset is not None else default_dataset()
        self._top_k = top_k
        self._recall_kwargs = dict(recall_kwargs or {})

    def forward(self, query: str) -> str:
        kwargs = dict(self._recall_kwargs)
        if self._dataset:
            kwargs.setdefault("datasets", [self._dataset])
        kwargs.setdefault("top_k", self._top_k)
        try:
            results = recall(query, **kwargs)
        except (
            Exception
        ) as exc:  # tool errors are reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_search failed: {exc}")
            return f"Could not search memory: {exc}"
        texts = render_results(results)
        if not texts:
            return "No memories found."
        return "\n".join(texts)


class CogneeForgetTool(Tool):
    """Clear this agent's stored memories.

    Scoped to the configured dataset only, never the whole cognee account:
    an agent-triggered wipe should never be able to reach past its own
    sandbox, even by mistake.
    """

    name = "cognee_forget"
    description = (
        "Clear everything stored in this agent's long-term memory. "
        "Use with care, this cannot be undone."
    )
    inputs = {}
    output_type = "string"

    def __init__(
        self, dataset: Optional[str] = None, forget_kwargs: Optional[Dict[str, Any]] = None
    ):
        super().__init__()
        self._dataset = dataset if dataset is not None else (default_dataset() or "main_dataset")
        self._forget_kwargs = dict(forget_kwargs or {})

    def forward(self) -> str:
        kwargs = dict(self._forget_kwargs)
        kwargs.setdefault("dataset", self._dataset)
        try:
            forget(**kwargs)
        except (
            Exception
        ) as exc:  # tool errors are reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_forget failed: {exc}")
            return f"Could not clear memory: {exc}"
        return f"Cleared memory for dataset '{self._dataset}'."


def cognee_tools(
    dataset: Optional[str] = None,
    *,
    top_k: int = 15,
    remember_kwargs: Optional[Dict[str, Any]] = None,
    recall_kwargs: Optional[Dict[str, Any]] = None,
    forget_kwargs: Optional[Dict[str, Any]] = None,
) -> List[Tool]:
    """Build the ``cognee_remember``, ``cognee_search`` and ``cognee_forget`` tools.

    Pass the result to ``CodeAgent(tools=cognee_tools())`` or
    ``ToolCallingAgent(tools=cognee_tools())``. Without ``dataset``, each tool
    falls back to ``COGNEE_PROJECT_NODE_SET`` (the same project-scoping env var
    the other cognee integrations use) and then to cognee's own default.
    """
    return [
        CogneeRememberTool(dataset=dataset, remember_kwargs=remember_kwargs),
        CogneeSearchTool(dataset=dataset, top_k=top_k, recall_kwargs=recall_kwargs),
        CogneeForgetTool(dataset=dataset, forget_kwargs=forget_kwargs),
    ]
