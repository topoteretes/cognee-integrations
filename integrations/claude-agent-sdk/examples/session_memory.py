"""Session memory vs permanent memory with cognee + the Claude Agent SDK.

Agent A (no session) writes to the permanent graph; Agent B (session_id, with
self_improvement=False) writes to the session cache, which stays invisible to
graph search until cognee.improve() persists it. The numbered prints walk
through that contrast.

Run from the integration directory (needs LLM_API_KEY in .env):

    uv run python examples/session_memory.py
"""

import asyncio
import os

import cognee
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    create_sdk_mcp_server,
)
from cognee.api.v1.config import config
from cognee.api.v1.visualize import visualize_multi_user_graph
from cognee.modules.users.methods import get_default_user
from cognee_integration_claude import cognee_tools
from dotenv import load_dotenv

load_dotenv()

SESSION_ID = "mission-briefing"

# Isolated, gitignored state dir so forget(everything=True) can't wipe real data.
_COGNEE_DIR = os.path.join(os.path.dirname(__file__), ".cognee")

DISALLOWED = [
    "Task",
    "Bash",
    "Glob",
    "Grep",
    "ExitPlanMode",
    "Read",
    "Edit",
    "Write",
    "NotebookEdit",
    "WebFetch",
    "TodoWrite",
    "WebSearch",
    "BashOutput",
    "KillShell",
    "SlashCommand",
]


def _options(server_name: str, tools: list) -> ClaudeAgentOptions:
    server = create_sdk_mcp_server(name=server_name, version="1.0.0", tools=tools)
    return ClaudeAgentOptions(
        mcp_servers={"tools": server},
        allowed_tools=["mcp__tools__remember", "mcp__tools__recall"],
        disallowed_tools=DISALLOWED,
    )


# No session -> permanent graph.
agent_a = _options("agent-a", cognee_tools())

# session_id + self_improvement=False -> writes stay in the cache until improve().
agent_b = _options(
    "agent-b",
    cognee_tools(session_id=SESSION_ID, remember_kwargs={"self_improvement": False}),
)


async def ask(options: ClaudeAgentOptions, prompt: str) -> str:
    """Run one prompt through a fresh agent and return its text reply."""
    reply = ""
    async with ClaudeSDKClient(options=options) as client:
        await client.query(prompt)
        async for msg in client.receive_response():
            if isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        reply += block.text
    return reply.strip()


async def visualize(file_name: str) -> None:
    # Plain visualize_graph() can't see per-dataset graphs under access control,
    # so render every dataset the default user owns.
    user = await get_default_user()
    pairs = [(user, ds) for ds in await cognee.datasets.list_datasets(user=user)]
    path = os.path.join(_COGNEE_DIR, file_name)
    await visualize_multi_user_graph(pairs, destination_file_path=path)
    print(f"   graph -> {path}")


async def main() -> None:
    config.data_root_directory(os.path.join(_COGNEE_DIR, "data"))
    config.system_root_directory(os.path.join(_COGNEE_DIR, "system"))

    await cognee.forget(everything=True)

    print("\n1) Agent A (no session) remembers facts -> permanent graph")
    print(
        await ask(
            agent_a,
            "Use your remember tool to store each of these as separate facts:\n"
            '- "Meditech Solutions" — healthcare industry, contract worth £1.2M.\n'
            '- "QuantumSoft" — technology industry, contract worth £5.5M.\n'
            '- "Orion Retail Group" — retail industry, contract worth £850K.',
        )
    )

    print("\n2) Agent A recalls from permanent memory")
    print(
        await ask(agent_a, "Use your recall tool: which contracts are in the healthcare industry?")
    )

    print("\n3) Visualize the permanent graph")
    await visualize("session_demo_graph_1.html")

    print("\n4) Agent B (session) reads the permanent memory Agent A populated")
    print(
        await ask(agent_b, "Use your recall tool: what technology-industry contracts do we have?")
    )

    print("\n5) Agent B remembers a NEW fact -> session cache only")
    print(
        await ask(
            agent_b,
            "Use your remember tool to store: The Orion Retail Group contract was "
            "renewed for 3 more years at an increased value of £2.0M for 2026.",
        )
    )

    orion_q = (
        "Use your recall tool: what is the renewed 2026 value of the Orion Retail Group contract?"
    )

    print("\n6) Agent A asks about Agent B's fact -> not in the graph yet")
    print(await ask(agent_a, orion_q))

    print(
        f"\n7) Persist the session cache into the permanent graph: "
        f"improve(session_ids=[{SESSION_ID!r}])"
    )
    await cognee.improve(session_ids=[SESSION_ID])
    print("   done")

    print("\n8) Agent A asks again -> now found")
    print(await ask(agent_a, orion_q))

    print("\n9) Visualize the graph again (now includes Agent B's persisted data)")
    await visualize("session_demo_graph_2.html")


if __name__ == "__main__":
    asyncio.run(main())
