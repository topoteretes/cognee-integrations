"""Using the lower-level methods when your application owns the run loop.

``recall_context``, ``register_tools``, ``observe_step`` and ``finalize`` can be
called individually. None of them changes an agent that already exists: build
the system-prompt extension *before* constructing the ``Agent``.
"""

from __future__ import annotations

import asyncio

from browser_use import Agent, ChatOpenAI, Tools
from cognee_integration_browser_use import CogneeBrowserMemory, MemoryScope


async def main() -> None:
    task = "Find the return policy on https://example.com and summarize it in one sentence."
    async with CogneeBrowserMemory(remember_tool_enabled=True) as memory:
        run = memory.new_run(task=task, scope=MemoryScope(project_id="returns-audit"))

        tools = Tools()
        run.register_tools(tools)
        context = await run.recall_context("return policy example.com")

        agent = Agent(
            task=task,
            llm=ChatOpenAI(model="gpt-4.1-mini"),
            tools=tools,
            extend_system_message=run.instructions(context),
        )

        async def on_step_end(current_agent: Agent) -> None:
            await run.observe_step(current_agent)  # cheap metadata only, no network

        history = None
        try:
            history = await agent.run(max_steps=15, on_step_end=on_step_end)
        finally:
            # A validator can promote the record to application_validated.
            receipt = await run.finalize(
                history,
                validator=lambda h: bool(h and h.is_successful()),
            )
            print("receipt:", receipt.to_dict() if receipt else None)
            if receipt:
                print("status:", (await memory.record_status(receipt.record_id)).status.value)


if __name__ == "__main__":
    asyncio.run(main())
