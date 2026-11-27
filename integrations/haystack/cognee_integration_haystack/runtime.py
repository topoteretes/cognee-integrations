import asyncio
import logging
import threading
from typing import Any, List, Optional

import cognee

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

# Haystack components run synchronously but cognee is async, so run cognee
# coroutines on a dedicated background event loop and block for the result.
# Mirrors the strands integration's runner (same problem, same fix).
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
