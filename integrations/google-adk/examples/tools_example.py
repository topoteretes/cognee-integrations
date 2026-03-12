import asyncio
import os

import cognee
from cognee_integration_google_adk import add_tool, search_tool
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner

load_dotenv()


async def main():
    from cognee.api.v1.config import config

    config.data_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/data_storage"))

    config.system_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/system"))

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    # """
    #     # Step 1. open file and read the content + add to cognee
    # """
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    for filename in os.listdir(data_dir):
        if filename.endswith(".txt"):
            file_path = os.path.join(data_dir, filename)
            with open(file_path, "r") as f:
                content = f.read()
                await cognee.add(content)
    await cognee.cognify()

    """
        Do a research on the following topic: "What contracts are in the healthcare industy?"
    """
    root_agent = Agent(
        model="gemini-2.5-flash",
        name="root_agent",
        description="A helpful assistant",
        instruction="You are a helpful assistant",
        tools=[add_tool, search_tool],
    )

    runner = InMemoryRunner(agent=root_agent)
    events = await runner.run_debug(
        "I need to research our contract portfolio."
        " Can you search for any contracts we have with companies in the healthcare industry?"
        " Please use the search functionality to find this information."
    )

    print("\n=== AGENT RESPONSE ===")
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)


if __name__ == "__main__":
    asyncio.run(main())
