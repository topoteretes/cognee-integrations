import asyncio
import os
import webbrowser

import cognee
from cognee_integration_crewai import get_sessionized_cognee_tools
from crewai import Agent
from dotenv import load_dotenv

load_dotenv()


async def visualize_graph(file_name, open_browser=True):
    current_file_dir = os.path.dirname(os.path.abspath(__file__))
    destination_file_path = os.path.join(current_file_dir, file_name)

    await cognee.visualize_graph(destination_file_path)

    if open_browser:
        url = "file://" + os.path.abspath(destination_file_path)
        webbrowser.open(url)


async def main():
    from cognee.api.v1.config import config

    config.data_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/data_storage"))

    config.system_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/system"))

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    """
        Do a research on the following topic: "What contracts are in the healthcare industy?"
    """

    add_tool, search_tool = get_sessionized_cognee_tools("a-sample-session-id")

    # A fresh agent instance, unaware of what is in the memory
    agent = Agent(
        role="Research Analyst",
        goal="Find and analyze contracts in the healthcare industry using the knowledge base",
        backstory=(
            "You are an expert research analyst with access to a comprehensive "
            "knowledge base about company contracts and partnerships."
        ),
        tools=[add_tool, search_tool],
    )

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

    response = agent.kickoff(
        [
            {
                "role": "user",
                "content": (
                    "We have signed a contract with the following company: "
                    '"Guardian Insurance Ltd". Company is in the insurance industry. '
                    "Start date is Feb 2023 and end date is Feb 2026. "
                    "Contract value is £1.8M."
                ),
            },
            {
                "role": "user",
                "content": (
                    "We have signed a contract with the following company: "
                    '"Pioneer Assurance Group". Company is in the insurance industry. '
                    "Start date is Oct 2024 and end date is Oct 2029. "
                    "Contract value is £4.2M."
                ),
            },
            {
                "role": "user",
                "content": (
                    "We have signed a contract with the following company: "
                    '"Finovate Systems". Company is in the fintech industry. '
                    "Start date is May 2024 and end date is May 2027. "
                    "Contract value is £2.3M."
                ),
            },
        ]
    )
    print("\n=== AGENT RESPONSE ===")
    print(response.raw)

    response = fresh_agent.kickoff(
        "I need to research our contract portfolio. Can you search for any contracts "
        "we have with companies in the insurance industry? Please use the search "
        "functionality to find this information."
    )

    print("\n=== AGENT RESPONSE ===")
    print(response.raw)

    await visualize_graph(file_name="sessionized_tools_example_visualization.html")


if __name__ == "__main__":
    asyncio.run(main())
