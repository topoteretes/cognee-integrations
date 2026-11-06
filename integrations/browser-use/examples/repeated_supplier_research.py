"""Repeated supplier research with Cognee memory.

Run it twice (two processes). The first run researches the supplier and leaves a
compact, source-linked outcome in Cognee. The second run starts with that
evidence in its system prompt and can call ``cognee_recall`` for more, while the
instructions tell it to re-check facts that change on the live page.

    export COGNEE_BASE_URL=http://localhost:8000 COGNEE_API_KEY=...
    export OPENAI_API_KEY=...
    uv run python examples/repeated_supplier_research.py "https://supplier.example/catalog"

Scope comes from *your* application (here a fixed tenant/user pair); the model
never chooses a dataset.
"""

from __future__ import annotations

import asyncio
import json
import sys

from browser_use import Agent, ChatOpenAI, Tools
from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope


async def main(url: str) -> None:
    scope = MemoryScope(tenant_id="acme", user_id="procurement")
    task = f"Open {url} and report the lead time, minimum order quantity and contact email."

    async with CogneeBrowserMemory(receipts_path="./data/browser-use-receipts.sqlite") as memory:
        run = memory.new_run(task=task, scope=scope, query_hints=["supplier lead time"])

        tools = Tools()
        run.register_tools(tools)  # cognee_recall (+ cognee_remember when enabled)

        context = await run.recall_context()  # bounded, degrades to empty on failure
        print(f"recall: {context.status} ({len(context.hits)} hits)")

        agent = Agent(
            task=task,
            llm=ChatOpenAI(model="gpt-4.1-mini"),
            tools=tools,
            extend_system_message=run.instructions(context),
        )
        history = await run.execute(agent, max_steps=25)

        print("result:", history.final_result())
        print("memory:", json.dumps(run.status.to_dict(), indent=2, default=str))


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "https://example.com"))
