# /// script
# requires-python = ">=3.11"
# dependencies = ["band-sdk[anthropic]", "cognee-integration-band"]
# ///
"""Band agent with shared Cognee memory.

Wraps a stock AnthropicAdapter with CogneeMemoryAdapter: every room message
gets relevant context recalled from the shared `agent_sessions` dataset, every
turn is captured as a QA pair, and sessions are bridged into the graph when a
room closes or the agent stops. The `cognee_search` / `cognee_remember` tools
let the model make explicit memory calls too.

Setup (once):
  1. Create an agent at https://app.band.ai/agents and export its credentials:
       export BAND_AGENT_ID="..."
       export BAND_AGENT_API_KEY="..."
  2. Point at your Cognee server in ~/.cognee/.env (shared with the Claude
     Code / Codex plugins):
       COGNEE_BASE_URL="https://your-instance.cognee.ai"
       COGNEE_API_KEY="ck_..."
  3. export ANTHROPIC_API_KEY="sk-ant-..."

Run:
  uv run examples/memory_agent.py
"""

import asyncio
import os

from band import Agent, configure_logging
from band.adapters import AnthropicAdapter
from cognee_band import CogneeClient, CogneeMemoryAdapter, CogneeSettings, cognee_tools

configure_logging()


async def main() -> None:
    settings = CogneeSettings.resolve()
    client = CogneeClient(settings)

    inner = AnthropicAdapter(
        prompt=(
            "You are a helpful assistant with persistent memory. Blocks labeled "
            "'Cognee memory' contain context recalled from past sessions — treat "
            "them as your own memory. Use cognee_search for explicit lookups and "
            "cognee_remember to store facts worth keeping."
        ),
        additional_tools=cognee_tools(client),
    )

    agent = Agent.create(
        adapter=CogneeMemoryAdapter(inner, settings=settings, client=client),
        agent_id=os.environ["BAND_AGENT_ID"],
        api_key=os.environ["BAND_AGENT_API_KEY"],
    )
    await agent.run()


if __name__ == "__main__":
    asyncio.run(main())
