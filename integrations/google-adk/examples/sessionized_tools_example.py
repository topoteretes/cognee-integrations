import asyncio
import os
import webbrowser

import cognee
from cognee_integration_google_adk import get_sessionized_cognee_tools
from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.runners import InMemoryRunner

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

    # """
    #     Do a research on the following topic: "What contracts are in the healthcare industy?"
    # """

    add_tool, search_tool = get_sessionized_cognee_tools("a-sample-session-id")

    # # A fresh agent instance, unaware of what is in the memory
    agent = Agent(
        model="gemini-2.5-flash",
        name="research_analyst",
        description=(
            "You are an expert research analyst with access to a comprehensive "
            "knowledge base about company contracts and partnerships."
        ),
        instruction=(
            "You are an expert research analyst with access to a comprehensive "
            "knowledge base about company contracts and partnerships."
        ),
        tools=[add_tool, search_tool],
    )

    runner = InMemoryRunner(agent=agent)

    contracts = [
        (
            'We have signed a contract with the following company: "Guardian Insurance Ltd".'
            " Company is in the insurance industry."
            " Start date is Feb 2023 and end date is Feb 2026. Contract value is £1.8M."
        ),
        (
            'We have signed a contract with the following company: "Pioneer Assurance Group".'
            " Company is in the insurance industry."
            " Start date is Oct 2024 and end date is Oct 2029. Contract value is £4.2M."
        ),
        (
            'We have signed a contract with the following company: "Finovate Systems".'
            " Company is in the fintech industry."
            " Start date is May 2024 and end date is May 2027. Contract value is £2.3M."
        ),
    ]

    print("\n=== ADDING CONTRACTS ===")
    for contract_text in contracts:
        print(f"\nProcessing: {contract_text[:50]}...")
        # We use run_debug for each contract to simulate sequential addition
        await runner.run_debug(contract_text)

    print("\n=== SEARCHING CONTRACTS ===")
    # Create a new runner to simulate a fresh session/agent
    # but with same tools (and thus same data access via session_id)
    fresh_runner = InMemoryRunner(agent=agent)

    search_query = (
        "I need to research our contract portfolio."
        " Can you search for any contracts we have with companies in the insurance industry?"
    )

    events = await fresh_runner.run_debug(search_query)

    print("\n=== AGENT RESPONSE ===")
    for event in events:
        if event.is_final_response() and event.content:
            for part in event.content.parts:
                if part.text:
                    print(part.text)

    await visualize_graph(file_name="sessionized_tools_example_visualization.html")


if __name__ == "__main__":
    asyncio.run(main())
