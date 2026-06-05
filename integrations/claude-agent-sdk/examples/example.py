import asyncio
import os
import webbrowser

import cognee
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    UserMessage,
    create_sdk_mcp_server,
)
from cognee.api.v1.visualize import visualize_multi_user_graph
from cognee.modules.users.methods import get_default_user
from cognee_integration_claude import cognee_tools


def display_message(msg):
    """Print a message as 'Speaker: text' (system messages ignored)."""
    if isinstance(msg, UserMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"User: {block.text}")
    elif isinstance(msg, AssistantMessage):
        for block in msg.content:
            if isinstance(block, TextBlock):
                print(f"Claude: {block.text}")
    elif isinstance(msg, SystemMessage):
        pass
    elif isinstance(msg, ResultMessage):
        print("Result ended")


async def visualize_graph(open_browser=True):
    destination_file_path = os.path.join(
        os.path.dirname(__file__), "../.cognee", "graph_visualization.html"
    )

    # Plain visualize_graph() can't see per-dataset graphs under cognee's access
    # control, so render every dataset the default user owns.
    user = await get_default_user()
    pairs = [(user, ds) for ds in await cognee.datasets.list_datasets(user=user)]
    await visualize_multi_user_graph(pairs, destination_file_path=destination_file_path)

    if open_browser:
        url = "file://" + os.path.abspath(destination_file_path)
        webbrowser.open(url)


server = create_sdk_mcp_server(name="my-tools", version="1.0.0", tools=cognee_tools())

options = ClaudeAgentOptions(
    mcp_servers={"tools": server},
    allowed_tools=["mcp__tools__remember", "mcp__tools__recall"],
    disallowed_tools=[
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
    ],
)


async def main():
    from cognee.api.v1.config import config

    config.data_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/data_storage"))

    config.system_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/system"))

    await cognee.forget(everything=True)

    async with ClaudeSDKClient(options=options) as client:
        await client.query(
            'We have signed a contract with the following company: "Meditech Solutions". '
            "Company is in the healthcare industry. Start date is Jan 2023 and "
            "end date is Dec 2025. Contract value is £1.2M.\n"
            'We have signed a contract with the following company: "QuantumSoft". '
            "Company is in the technology industry. Start date is Aug 2024 and "
            "end date is Aug 2028. Contract value is £5.5M.\n"
            'We have signed a contract with the following company: "Orion Retail Group". '
            "Company is in the retail industry. Start date is Mar 2024 and "
            "end date is Mar 2026. Contract value is £850K.\n"
        )

        await client.query(
            "I need to research our contract portfolio. Can you search for any contracts we "
            "have with companies in the healthcare industry? Use any tools you have."
        )

        async for msg in client.receive_response():
            display_message(msg)

    await visualize_graph()


if __name__ == "__main__":
    asyncio.run(main())
