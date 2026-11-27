"""A deep agent whose every step lands in cognee memory.

Set your cognee `.env` (LLM + embedding provider) first, then:

    python examples/example.py
"""

import asyncio

from deepagents import create_deep_agent
from cognee_integration_deepagents import (
    CogneeTraceMiddleware,
    persist_thread,
    with_cognee_tracing,
)


def read_docs(query: str) -> str:
    """Stand-in for a real tool the agent calls."""
    return f"Docs for {query}: ingestion adds data, recall asks questions."


async def main() -> None:
    subagents = [
        {
            "name": "researcher",
            "description": "Reads documentation and reports back",
            "tools": [read_docs],
        }
    ]
    agent = create_deep_agent(
        # model="openai:gpt-4.1",  # any LangChain chat model
        tools=[],
        subagents=with_cognee_tracing(subagents),
        middleware=[CogneeTraceMiddleware(dataset_name="research_agent")],
    )

    thread_id = "demo-thread"
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Use the docs to explain cognee recall."}]},
        config={"configurable": {"thread_id": thread_id}},
    )
    print(result["messages"][-1].content)

    # Bridge the thread's trace into the permanent graph (traces → nodes,
    # distilled lessons) so the next run recalls what this one did.
    await persist_thread(thread_id, dataset_name="research_agent")


if __name__ == "__main__":
    asyncio.run(main())
