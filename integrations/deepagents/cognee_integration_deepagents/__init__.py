"""Cognee memory for LangChain Deep Agents.

Record every agent step — plans, tool calls, sub-agent work — into cognee's
session cache, recall what cognee knows before each run, and mirror the
agent's ``/memories/`` files into permanent graph memory.

    from deepagents import create_deep_agent
    from cognee_integration_deepagents import CogneeTraceMiddleware

    agent = create_deep_agent(model=model, tools=tools, middleware=[CogneeTraceMiddleware()])
"""

from .backend import CogneeBackend
from .tracing import CogneeTraceMiddleware, persist_thread, with_cognee_tracing

__all__ = [
    "CogneeBackend",
    "CogneeTraceMiddleware",
    "persist_thread",
    "with_cognee_tracing",
]
