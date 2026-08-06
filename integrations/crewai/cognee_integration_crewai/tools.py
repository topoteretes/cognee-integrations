import asyncio
import concurrent.futures
import functools
import logging
import threading
from typing import List, Optional

import cognee
from crewai.tools import tool

logger = logging.getLogger(__name__)

# Create a dedicated background event loop
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


def _run_async(coro):
    """Run coroutine safely on a background event loop."""
    _start_background_loop()
    fut = asyncio.run_coroutine_threadsafe(coro, _loop)
    try:
        # Add timeout and better error handling
        result = fut.result(timeout=120)  # 120 second timeout
        logger.info("Async operation completed successfully")
        return result
    except concurrent.futures.TimeoutError:
        logger.error("Async operation timed out after 120 seconds")
        raise Exception("Operation timed out - check for deadlocks")
    except Exception as e:
        logger.error(f"Async operation failed with exception: {e}")
        import traceback

        traceback.print_exc()
        raise


_add_lock = asyncio.Lock()
_add_queue = asyncio.Queue()


async def _enqueue_add(*args, **kwargs):
    global _add_lock
    if _add_lock.locked():
        await _add_queue.put((args, kwargs))
        return
    async with _add_lock:
        await _add_queue.put((args, kwargs))
        while True:
            try:
                next_args, next_kwargs = await asyncio.wait_for(_add_queue.get(), timeout=2)
                _add_queue.task_done()
            except asyncio.TimeoutError:
                break
            await cognee.add(*next_args, **next_kwargs)
        await cognee.cognify()


@tool
def add_tool(data: str, node_set: Optional[List[str]] = None):
    """
    Store information in the knowledge base for later retrieval.

    Use this tool whenever you need to remember, store, or save information
    that the user provides. This is essential for building up a knowledge base
    that can be searched later. Always use this tool when the user says things
    like "remember", "store", "save", or gives you information to keep track of.

    Args:
        data (str): The text or information you want to store and remember.
        node_set (Optional[List[str]]): Additional node set identifiers.

    Returns:
        str: A confirmation message indicating that the item was added.
    """
    logger.info(f"Adding data to cognee: {data}")

    # Use lock to prevent race conditions during database initialization
    _run_async(_enqueue_add(data, node_set=node_set))
    return "Item added to cognee and processed"


@tool
def search_tool(query_text: str):
    """
    Search and retrieve previously stored information from the knowledge base.

    Use this tool to find and recall information that was previously stored.
    Always use this tool when you need to answer questions about information
    that should be in the knowledge base, or when the user asks questions
    about previously discussed topics.

    Args:
        query_text (str): What you're looking for, written as a natural language search query.

    Returns:
        list: A list of search results matching the query.
    """
    logger.info(f"Searching cognee for: {query_text}")
    _run_async(_add_queue.join())
    result = _run_async(cognee.search(query_text, top_k=100))
    logger.info(f"Search results: {result}")
    return result


def sessionised_tool(user_id: str):
    """
    Decorator factory that creates a decorator to add user_id to tool calls.

    Args:
        user_id (str): The user session ID to bind to the tool

    Returns:
        A decorator that modifies tools to use the specific user's session
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            logger.info(f"Using tool {func.__name__} with user_id: {user_id}")
            # Inject user_id for tools that support it
            if func.__name__ == "add_tool":
                kwargs["node_set"] = [user_id]
            return func(*args, **kwargs)

        return wrapper

    return decorator


def get_sessionized_cognee_tools(session_id: Optional[str] = None) -> list:
    """
    Returns a list of cognee tools sessionized for a specific user.

    Args:
        session_id (str): The session ID to bind to all tools

    Returns:
        list: List of sessionized cognee tools
    """
    if session_id is None:
        import uuid

        uid = str(uuid.uuid4())
        session_id = f"cognee-test-user-{uid}"

    session_decorator = sessionised_tool(session_id)

    sessionized_add_tool = tool(session_decorator(add_tool.func))
    sessionized_search_tool = tool(session_decorator(search_tool.func))

    logger.info(f"Initialized session with session_id = {session_id}")

    return [
        sessionized_add_tool,
        sessionized_search_tool,
    ]
