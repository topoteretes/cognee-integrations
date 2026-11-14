import asyncio
import logging
import os
import weakref
from typing import Annotated, Any, Dict, List, Optional

import cognee
from semantic_kernel.functions import kernel_function

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Semantic Kernel runs async kernel functions on its own event loop, so cognee
# is awaited directly here (no background-loop runner like the sync strands /
# smolagents tools need). cognee isn't safe to initialise concurrently, and SK
# auto function calling can invoke several tool calls at once, so writes are
# serialised. One lock per running loop: an asyncio.Lock bound to one loop
# can't be used from another (e.g. across separate asyncio.run calls).
_write_locks: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock]" = (
    weakref.WeakKeyDictionary()
)


def _write_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _write_locks.get(loop)
    if lock is None:
        lock = _write_locks[loop] = asyncio.Lock()
    return lock


def default_dataset() -> Optional[str]:
    """``COGNEE_PROJECT_NODE_SET`` is the project-scoping env var the other
    cognee integrations use. ``remember``/``recall``/``forget`` don't accept a
    ``node_set`` kwarg though (that's a parameter on the older
    ``add``/``cognify``/``search`` API); their equivalent is a dataset name.
    Same env var, mapped onto the parameter this API generation actually has.
    """
    tag = os.environ.get("COGNEE_PROJECT_NODE_SET", "").strip()
    return tag or None


def _render(item: Any) -> Optional[str]:
    # cognee.recall returns a discriminated union keyed on `source`; pull the
    # text field each source type carries. Same shape the strands integration
    # flattens, since both sit on the same cognee.recall response.
    source = getattr(item, "source", None)
    if source is None:
        return str(item) if item is not None else None
    if source == "graph":
        return item.text
    if source == "session":
        return item.answer or item.question or None
    if source == "graph_context":
        return item.content
    if source == "trace":
        return getattr(item, "memory_context", None)
    return str(item)


def render_results(results: Any) -> List[str]:
    """Flatten a ``cognee.recall`` result list into plain strings."""
    return [text for item in (results or []) if (text := _render(item))]


class CogneeMemoryPlugin:
    """A Semantic Kernel plugin exposing cognee memory as kernel functions.

    Register with ``kernel.add_plugin(CogneeMemoryPlugin(), plugin_name="cognee")``;
    ``remember``, ``search`` and ``forget`` are then callable like any other SK
    plugin function, and available to auto function calling / planners.
    Failures are returned as text rather than raised, so a model calling these
    functions sees what went wrong and can recover.
    """

    def __init__(
        self,
        dataset: Optional[str] = None,
        top_k: int = 15,
        remember_kwargs: Optional[Dict[str, Any]] = None,
        recall_kwargs: Optional[Dict[str, Any]] = None,
        forget_kwargs: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        :param dataset: Cognee dataset to read and write. Defaults to
            ``COGNEE_PROJECT_NODE_SET`` when set, else cognee's own default.
            ``forget`` always resolves to a concrete dataset (``"main_dataset"``
            when nothing is configured), never an unscoped wipe.
        :param top_k: Result count for ``search``, forwarded to ``cognee.recall``.
        :param remember_kwargs: Extra kwargs for every ``cognee.remember`` call.
        :param recall_kwargs: Extra kwargs for every ``cognee.recall`` call.
        :param forget_kwargs: Extra kwargs for every ``cognee.forget`` call.
        """
        self._dataset = dataset if dataset is not None else default_dataset()
        self._top_k = top_k
        self._remember_kwargs = dict(remember_kwargs or {})
        self._recall_kwargs = dict(recall_kwargs or {})
        self._forget_kwargs = dict(forget_kwargs or {})

    @kernel_function(
        name="remember",
        description=(
            "Store information in long-term memory for later retrieval. "
            "Use whenever you learn something worth remembering across sessions."
        ),
    )
    async def remember(
        self, data: Annotated[str, "The text or information to store."]
    ) -> Annotated[str, "Whether the information was stored."]:
        kwargs = dict(self._remember_kwargs)
        if self._dataset:
            kwargs.setdefault("dataset_name", self._dataset)
        try:
            async with _write_lock():
                await cognee.remember(data, **kwargs)
        except Exception as exc:  # reported as text, not raised, so the model can recover
            logger.warning(f"cognee remember failed: {exc}")
            return f"Could not store memory: {exc}"
        return "Stored in memory."

    @kernel_function(
        name="search",
        description=(
            "Search long-term memory for information relevant to a query. "
            "Use to recall anything previously stored with remember."
        ),
    )
    async def search(
        self, query: Annotated[str, "A natural-language search query."]
    ) -> Annotated[str, "The relevant stored information, one item per line."]:
        kwargs = dict(self._recall_kwargs)
        if self._dataset:
            kwargs.setdefault("datasets", [self._dataset])
        kwargs.setdefault("top_k", self._top_k)
        try:
            results = await cognee.recall(query, **kwargs)
        except Exception as exc:  # reported as text, not raised, so the model can recover
            logger.warning(f"cognee search failed: {exc}")
            return f"Could not search memory: {exc}"
        texts = render_results(results)
        if not texts:
            return "No memories found."
        return "\n".join(texts)

    @kernel_function(
        name="forget",
        description=(
            "Clear everything stored in this agent's long-term memory. "
            "Use with care, this cannot be undone."
        ),
    )
    async def forget(self) -> Annotated[str, "Whether the memory was cleared."]:
        kwargs = dict(self._forget_kwargs)
        dataset = self._dataset or "main_dataset"
        kwargs.setdefault("dataset", dataset)
        try:
            async with _write_lock():
                await cognee.forget(**kwargs)
        except Exception as exc:  # reported as text, not raised, so the model can recover
            logger.warning(f"cognee forget failed: {exc}")
            return f"Could not clear memory: {exc}"
        return f"Cleared memory for dataset '{kwargs['dataset']}'."
