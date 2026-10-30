import asyncio
import logging
import os
import threading
import weakref
from typing import Any, Dict, List, Optional

import cognee
from agno.tools import Toolkit

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Agno runs sync tools under agent.run() and async tools under agent.arun().
# The async methods below are the real implementation; the sync ones run them
# on a dedicated background event loop (same runner the strands integration
# uses), so both paths share one code path.
_loop = None
_loop_thread = None


def _start_background_loop():
    global _loop, _loop_thread
    if _loop is None:
        _loop = asyncio.new_event_loop()

        def run_loop():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=run_loop, daemon=True)
        _loop_thread.start()


def run_cognee_task(coro, timeout=300):
    """Run an async cognee coroutine from sync code and return its result."""
    _start_background_loop()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


# cognee isn't safe to initialise concurrently, and an agent can fire several
# tool calls at once, so writes are serialised. One lock per running loop: the
# sync path runs on the background loop, the async path on the caller's, and
# an asyncio.Lock bound to one loop can't be used from another.
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


class CogneeTools(Toolkit):
    """An Agno toolkit exposing cognee memory: ``cognee_remember``,
    ``cognee_search`` and ``cognee_forget``.

    Use with ``Agent(tools=[CogneeTools()])``. Works under both ``agent.run()``
    and ``agent.arun()``. Failures are returned as text rather than raised, so
    the agent can read what went wrong and keep going.
    """

    def __init__(
        self,
        dataset: Optional[str] = None,
        top_k: int = 15,
        remember_kwargs: Optional[Dict[str, Any]] = None,
        recall_kwargs: Optional[Dict[str, Any]] = None,
        forget_kwargs: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        """
        :param dataset: Cognee dataset to read and write. Defaults to
            ``COGNEE_PROJECT_NODE_SET`` when set, else cognee's own default.
            ``cognee_forget`` always resolves to a concrete dataset
            (``"main_dataset"`` when nothing is configured), never an
            unscoped wipe.
        :param top_k: Result count for ``cognee_search``, forwarded to ``cognee.recall``.
        :param remember_kwargs: Extra kwargs for every ``cognee.remember`` call.
        :param recall_kwargs: Extra kwargs for every ``cognee.recall`` call.
        :param forget_kwargs: Extra kwargs for every ``cognee.forget`` call.
        :param kwargs: Forwarded to ``Toolkit`` (e.g. ``include_tools``,
            ``requires_confirmation_tools=["cognee_forget"]``).
        """
        self._dataset = dataset if dataset is not None else default_dataset()
        self._top_k = top_k
        self._remember_kwargs = dict(remember_kwargs or {})
        self._recall_kwargs = dict(recall_kwargs or {})
        self._forget_kwargs = dict(forget_kwargs or {})
        super().__init__(
            name="cognee_tools",
            tools=[self.cognee_remember, self.cognee_search, self.cognee_forget],
            async_tools=[
                (self.acognee_remember, "cognee_remember"),
                (self.acognee_search, "cognee_search"),
                (self.acognee_forget, "cognee_forget"),
            ],
            **kwargs,
        )

    async def acognee_remember(self, data: str) -> str:
        """Store information in long-term memory for later retrieval.

        Use whenever you learn something worth remembering across sessions.

        Args:
            data (str): The text or information to store.

        Returns:
            str: Whether the information was stored.
        """
        kwargs = dict(self._remember_kwargs)
        if self._dataset:
            kwargs.setdefault("dataset_name", self._dataset)
        try:
            async with _write_lock():
                await cognee.remember(data, **kwargs)
        except Exception as exc:  # reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_remember failed: {exc}")
            return f"Could not store memory: {exc}"
        return "Stored in memory."

    async def acognee_search(self, query: str) -> str:
        """Search long-term memory for information relevant to a query.

        Use to recall anything previously stored with cognee_remember.

        Args:
            query (str): A natural-language search query.

        Returns:
            str: The relevant stored information, one item per line.
        """
        kwargs = dict(self._recall_kwargs)
        if self._dataset:
            kwargs.setdefault("datasets", [self._dataset])
        kwargs.setdefault("top_k", self._top_k)
        try:
            results = await cognee.recall(query, **kwargs)
        except Exception as exc:  # reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_search failed: {exc}")
            return f"Could not search memory: {exc}"
        texts = render_results(results)
        if not texts:
            return "No memories found."
        return "\n".join(texts)

    async def acognee_forget(self) -> str:
        """Clear everything stored in this agent's long-term memory.

        Use with care, this cannot be undone.

        Returns:
            str: Whether the memory was cleared.
        """
        kwargs = dict(self._forget_kwargs)
        kwargs.setdefault("dataset", self._dataset or "main_dataset")
        try:
            async with _write_lock():
                await cognee.forget(**kwargs)
        except Exception as exc:  # reported as text, not raised, so the agent can recover
            logger.warning(f"cognee_forget failed: {exc}")
            return f"Could not clear memory: {exc}"
        return f"Cleared memory for dataset '{kwargs['dataset']}'."

    def cognee_remember(self, data: str) -> str:
        """Store information in long-term memory for later retrieval.

        Use whenever you learn something worth remembering across sessions.

        Args:
            data (str): The text or information to store.

        Returns:
            str: Whether the information was stored.
        """
        return run_cognee_task(self.acognee_remember(data))

    def cognee_search(self, query: str) -> str:
        """Search long-term memory for information relevant to a query.

        Use to recall anything previously stored with cognee_remember.

        Args:
            query (str): A natural-language search query.

        Returns:
            str: The relevant stored information, one item per line.
        """
        return run_cognee_task(self.acognee_search(query))

    def cognee_forget(self) -> str:
        """Clear everything stored in this agent's long-term memory.

        Use with care, this cannot be undone.

        Returns:
            str: Whether the memory was cleared.
        """
        return run_cognee_task(self.acognee_forget())
