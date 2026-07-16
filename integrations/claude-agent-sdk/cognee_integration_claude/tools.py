import asyncio
import logging
from typing import Any, List, Optional

import cognee
from claude_agent_sdk import tool

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

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


async def remember(data: Any, **kwargs: Any) -> Any:
    """Passthrough to ``cognee.remember`` (kwargs forwarded)"""
    logger.info(f"cognee.remember(kwargs={list(kwargs)}): {data}")
    async with _write_lock:
        return await cognee.remember(data, **kwargs)


async def recall(query_text: str, **kwargs: Any) -> Any:
    """Passthrough to ``cognee.recall`` (kwargs forwarded).

    Returns cognee's native ``RecallResponse`` list — use :func:`render_results`
    for plain strings.
    """
    logger.info(f"cognee.recall(kwargs={list(kwargs)}): {query_text}")
    return await cognee.recall(query_text, **kwargs)


def cognee_tools(
    session_id: Optional[str] = None,
    *,
    remember_kwargs: Optional[dict] = None,
    recall_kwargs: Optional[dict] = None,
) -> list:
    """Build the ``remember``/``recall`` MCP tools for ``create_sdk_mcp_server``.

    With ``session_id`` writes go to cognee's session cache (reads stay
    session-aware) until ``cognee.improve(session_ids=[session_id])`` persists
    them to the permanent graph; without it, writes go straight to the graph.
    ``remember_kwargs``/``recall_kwargs`` bind extra cognee params per call
    (e.g. ``remember_kwargs={"self_improvement": False}`` keeps session writes
    cache-only).
    """
    base = {"session_id": session_id} if session_id is not None else {}
    rem_kwargs = {**base, **(remember_kwargs or {})}
    rec_kwargs = {**base, **(recall_kwargs or {})}

    @tool("remember", "Store information in memory for later retrieval", {"data": str})
    async def remember_tool(args):
        data = args["data"]
        logger.info(f'remember tool called (session_id={session_id}): "{data}"')
        await remember(data, **rem_kwargs)
        return {"content": [{"type": "text", "text": "Item stored in cognee memory"}]}

    @tool("recall", "Recall previously stored information from memory", {"query_text": str})
    async def recall_tool(args):
        query_text = args["query_text"]
        logger.info(f'recall tool called (session_id={session_id}): "{query_text}"')
        results = await recall(query_text, **rec_kwargs)
        return {"content": [{"type": "text", "text": f"Result: {render_results(results)}"}]}

    return [remember_tool, recall_tool]
