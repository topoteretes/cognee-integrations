import asyncio
import os

import cognee
from cognee_integration_crewai import add_tool, search_tool
from crewai import Agent
from dotenv import load_dotenv

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
    # A fresh agent instance, unaware of what is in the memory
    fresh_agent = Agent(
        role="Research Analyst",
        goal="Find and analyze contracts in the healthcare industry using the knowledge base",
        backstory=(
            "You are an expert research analyst with access to a comprehensive "
            "knowledge base about company contracts and partnerships."
        ),
        tools=[add_tool, search_tool],
        verbose=True,
    )

    response = fresh_agent.kickoff(
        "I need to research our contract portfolio. Can you search for any contracts "
        "we have with companies in the healthcare industry? Please use the search "
        "functionality to find this information."
    )
    print("\n=== AGENT RESPONSE ===")
    print(response.raw)


if __name__ == "__main__":
    asyncio.run(main())
