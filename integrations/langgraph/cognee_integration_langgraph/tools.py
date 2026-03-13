import asyncio
import functools
import logging
from typing import List, Optional

import cognee
from cognee.memify_pipelines.persist_sessions_in_knowledge_graph import (
    persist_sessions_in_knowledge_graph_pipeline,
)
from cognee.modules.engine.models.node_set import NodeSet
from cognee.modules.search.types import SearchType
from cognee.modules.users.methods import get_default_user
from langchain_core.tools import tool

from . import bootstrap  # noqa: F401

logger = logging.getLogger(__name__)

_add_lock = asyncio.Lock()
_add_queue = asyncio.Queue()


async def _enqueue_add(*args, **kwargs):
    global _add_lock
    user = kwargs.pop("user", None)
    if _add_lock.locked():
        await _add_queue.put((args, kwargs, user))
        return
    async with _add_lock:
        await _add_queue.put((args, kwargs, user))
        while True:
            try:
                next_args, next_kwargs, next_user = await asyncio.wait_for(
                    _add_queue.get(), timeout=2
                )
                _add_queue.task_done()
            except asyncio.TimeoutError:
                break
            add_kwargs = {**next_kwargs}
            if next_user is not None:
                add_kwargs["user"] = next_user
            await cognee.add(*next_args, **add_kwargs)
        cognify_kwargs = {}
        if user is not None:
            cognify_kwargs["user"] = user
        await cognee.cognify(**cognify_kwargs)


@tool
async def add_tool(data: str, node_set: Optional[List[str]] = None, user=None):
    """
    Store information in the knowledge base for later retrieval.

    Use this tool whenever you need to remember, store, or save information
    that the user provides. This is essential for building up a knowledge base
    that can be searched later. Always use this tool when the user says things
    like "remember", "store", "save", or gives you information to keep track of.

    Args:
        data (str): The text or information you want to store and remember.
        node_set (Optional[List[str]]): Additional node set identifiers.
        user: Optional Cognee User object for access control.

    Returns:
        str: A confirmation message indicating that the item was added.
    """
    logger.info(f"Adding data to cognee: {data}")

    # Use lock to prevent race conditions during database initialization
    await _enqueue_add(data, node_set=node_set, user=user)
    return "Item added to cognee and processed"


async def search_tool(
    query_text: str,
    session_id: Optional[str] = None,
    node_set: Optional[List[str]] = None,
    query_type: Optional[str] = None,
    user=None,
):
    """
    Search and retrieve previously stored information from the knowledge base.

    Use this tool to find and recall information that was previously stored.
    Always use this tool when you need to answer questions about information
    that should be in the knowledge base, or when the user asks questions
    about previously discussed topics. The tool maintains conversation context
    within a session, allowing for follow-up questions.

    Args:
        query_text (str): What you're looking for, written as a natural language search query.
        session_id (Optional[str]): Session identifier for maintaining conversation context.
        node_set (Optional[List[str]]): Additional node set identifiers to filter search.
        query_type (Optional[str]): Type of search query (e.g., "GRAPH_COMPLETION", "INSIGHTS").
            Defaults to GRAPH_COMPLETION for conversational context.

    Returns:
        str or list: Search results matching the query.
    """
    logger.info(
        "Searching cognee for: %s with session_id: %s, node_set: %s, query_type: %s",
        query_text,
        session_id,
        node_set,
        query_type,
    )
    await _add_queue.join()

    # Prepare search parameters
    search_kwargs = {
        "query_text": query_text,
    }

    # Add session_id if provided (for conversation context)
    if session_id:
        search_kwargs["session_id"] = session_id

    # Add query_type if provided, default to GRAPH_COMPLETION for better conversational responses
    if query_type:
        try:
            search_kwargs["query_type"] = SearchType[query_type]
        except KeyError:
            logger.warning(f"Invalid query_type: {query_type}, using default")
    else:
        # Use GRAPH_COMPLETION as default for better conversation handling
        search_kwargs["query_type"] = SearchType.GRAPH_COMPLETION

    # Add node_set filtering if provided (for additional filtering beyond sessions)
    if node_set:
        search_kwargs["node_type"] = NodeSet
        search_kwargs["node_name"] = node_set
        search_kwargs["top_k"] = 100
    else:
        search_kwargs["top_k"] = 100

    # Pass user for access control if provided
    if user is not None:
        search_kwargs["user"] = user

    result = await cognee.search(**search_kwargs)

    logger.info(f"Search results: {result}")
    return result


@tool
async def persist_sessions_tool(session_ids: List[str]):
    """
    Persist conversation sessions into the knowledge graph for long-term memory.

    Use this tool to save important conversation sessions into the knowledge graph,
    making them part of the permanent knowledge base. This is useful for preserving
    important conversations, decisions, or information exchanges that should be
    remembered across different sessions.

    Args:
        session_ids (List[str]): List of session IDs to persist into the knowledge graph.

    Returns:
        str: A confirmation message indicating that sessions were persisted.
    """
    logger.info(f"Persisting sessions to knowledge graph: {session_ids}")

    try:
        # Get the default user
        default_user = await get_default_user()

        # Persist the sessions
        await persist_sessions_in_knowledge_graph_pipeline(
            user=default_user,
            session_ids=session_ids,
        )

        return f"Successfully persisted {len(session_ids)} session(s) to the knowledge graph"
    except Exception as e:
        logger.error(f"Error persisting sessions: {e}")
        return f"Error persisting sessions: {str(e)}"


def sessionised_tool(user_id: str, user=None):
    """
    Decorator factory that creates a decorator to add session_id to tool calls.

    Args:
        user_id (str): The user session ID to bind to the tool
        user: Optional Cognee User object for access control.

    Returns:
        A decorator that modifies tools to use the specific user's session
    """

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            logger.info(f"Using tool {func.__name__} with session_id: {user_id}")
            # Inject user for access control
            if user is not None:
                kwargs["user"] = user
            # Inject session_id for search_tool (native session support)
            if func.__name__ == "search_tool":
                kwargs["session_id"] = user_id
            # For add_tool, still use node_set for now (cognee.add doesn't support session_id yet)
            elif func.__name__ == "add_tool":
                kwargs["node_set"] = [user_id]
            return await func(*args, **kwargs)

        return wrapper

    return decorator


def get_sessionized_cognee_tools(
    session_id: Optional[str] = None,
    include_persist_tool: bool = False,
    user=None,
) -> list:
    """
    Returns a list of cognee tools sessionized for a specific user.

    Args:
        session_id (str): The session ID to bind to all tools. If not provided,
            a random session ID will be auto-generated.
        include_persist_tool (bool): Whether to include the persist_sessions_tool
            in the returned list. Default is False.
        user: Optional Cognee User object for access control.

    Returns:
        list: List of sessionized cognee tools
            (add_tool, search_tool, and optionally persist_sessions_tool)
    """
    if session_id is None:
        import uuid

        uid = str(uuid.uuid4())
        session_id = f"cognee-test-user-{uid}"

    session_decorator = sessionised_tool(session_id, user=user)

    add_callable = getattr(add_tool, "coroutine", add_tool)
    search_callable = getattr(search_tool, "coroutine", search_tool)
    sessionized_add_tool = tool(session_decorator(add_callable))
    sessionized_search_tool = tool(session_decorator(search_callable))

    logger.info(f"Initialized session with session_id = {session_id}")

    tools = [
        sessionized_add_tool,
        sessionized_search_tool,
    ]

    # Optionally include persist_sessions_tool (not sessionized, operates on multiple sessions)
    if include_persist_tool:
        tools.append(persist_sessions_tool)

    return tools
