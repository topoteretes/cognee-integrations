import asyncio
import logging
import threading
from typing import Any, List, Optional

import cognee
from strands import tool

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Strands tools are synchronous but cognee is async, so run cognee coroutines on
# a dedicated background event loop and block for the result.
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


# cognee isn't safe to initialise concurrently, so serialise writes.
_write_lock = asyncio.Lock()


def _render(item: Any) -> Optional[str]:
    # cognee.recall returns a discriminated union keyed on `source`; pull the
    # text field each source type carries.
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


async def _remember_async(data: Any, **kwargs: Any) -> Any:
    async with _write_lock:
        return await cognee.remember(data, **kwargs)


def remember(data: Any, **kwargs: Any) -> Any:
    """Sync passthrough to ``cognee.remember`` (kwargs forwarded; no defaults added)."""
    return run_cognee_task(_remember_async(data, **kwargs))


def recall(query_text: str, **kwargs: Any) -> Any:
    """Sync passthrough to ``cognee.recall``; returns cognee's RecallResponse list.

    Use :func:`render_results` to flatten the result into plain strings.
    """
    return run_cognee_task(cognee.recall(query_text, **kwargs))


def cognee_tools(
    session_id: Optional[str] = None,
    *,
    remember_kwargs: Optional[dict] = None,
    recall_kwargs: Optional[dict] = None,
) -> list:
    """Build the ``remember`` and ``recall`` Strands tools.

    Pass the result to ``Agent(tools=cognee_tools())``. With ``session_id``,
    writes go to cognee's session cache (reads stay session-aware) until
    ``cognee.improve(session_ids=[session_id])`` persists them to the permanent
    graph; without it, writes go straight to the graph. ``remember_kwargs`` /
    ``recall_kwargs`` bind extra cognee params per call (e.g.
    ``remember_kwargs={"self_improvement": False}`` keeps session writes
    cache-only).
    """
    base = {"session_id": session_id} if session_id is not None else {}
    rem_kwargs = {**base, **(remember_kwargs or {})}
    rec_kwargs = {**base, **(recall_kwargs or {})}

    @tool
    def remember(data: str) -> str:
        """Store information in memory for later retrieval.

        Use whenever the user gives you information to remember, store, or save.

        Args:
            data: The text or information to store.
        """
        logger.info(f"remember tool (session_id={session_id}): {data}")
        run_cognee_task(_remember_async(data, **rem_kwargs))
        return "Item stored in cognee memory"

    @tool
    def recall(query_text: str) -> str:
        """Search and retrieve previously stored information from memory.

        Use to answer questions about anything previously stored.

        Args:
            query_text: A natural-language search query.
        """
        logger.info(f"recall tool (session_id={session_id}): {query_text}")
        results = run_cognee_task(cognee.recall(query_text, **rec_kwargs))
        return f"Result: {render_results(results)}"

    return [remember, recall]
