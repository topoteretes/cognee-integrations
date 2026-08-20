"""Explicit Cognee tools for Band adapters.

Band adapters that accept ``additional_tools`` (Anthropic, ClaudeSDK,
PydanticAI, CrewAI, ...) take ``CustomToolDef = tuple[type[BaseModel],
Callable]`` pairs. The tool name is derived from the model class name
(``CogneeSearchInput`` → ``cognee_search``) and the description from the model
docstring, so the classes below define the model-visible contract.

Auto-recall already runs on every message; these tools are for *explicit*
memory operations the model decides to make — a targeted search, or durably
storing a fact worth keeping beyond the session.

Pydantic is imported inside :func:`cognee_tools` so importing ``cognee_band``
never requires it (the Band SDK ships it anyway).
"""

import asyncio

from .client import UNREACHABLE, CogneeClient


def cognee_tools(client: CogneeClient) -> list:
    """Build ``additional_tools`` entries backed by *client*.

    Returns ``[(CogneeSearchInput, handler), (CogneeRememberInput, handler)]``.
    """
    from pydantic import BaseModel, Field

    class CogneeSearchInput(BaseModel):
        """Search the shared Cognee memory (knowledge graph built from past
        agent sessions) for information relevant to a query. Use when the user
        refers to earlier work, decisions, people, or facts you don't have in
        the current conversation."""

        query: str = Field(description="What to look for, phrased as a question")

    class CogneeRememberInput(BaseModel):
        """Durably store an important fact, decision, or preference in the
        shared Cognee memory so any connected agent can recall it later. Use
        for information worth keeping beyond this conversation."""

        content: str = Field(description="The fact to remember, self-contained")

    async def search(inp) -> str:
        results = await asyncio.to_thread(client.recall, inp.query)
        if results == UNREACHABLE:
            return "Cognee memory server is unreachable."
        if isinstance(results, dict):
            return f"Cognee search failed: {results.get('error')}"
        if not results:
            return "No relevant memories found."
        from .adapter import render_memory_block

        return render_memory_block(results) or "No relevant memories found."

    async def remember(inp) -> str:
        result = await asyncio.to_thread(client.remember, inp.content)
        if result == UNREACHABLE:
            return "Cognee memory server is unreachable; nothing was stored."
        if isinstance(result, dict) and result.get("error"):
            return f"Cognee remember failed: {result.get('error')}"
        return "Stored in Cognee memory (graph build runs in the background)."

    return [(CogneeSearchInput, search), (CogneeRememberInput, remember)]
