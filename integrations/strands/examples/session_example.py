"""Session memory vs permanent memory with cognee + Strands.

Agent A (no session) writes to the permanent graph; Agent B (session_id, with
self_improvement=False) writes to the session cache, which stays invisible to
graph search until cognee.improve() persists it.

Run this example from the root (needs LLM_API_KEY in .env):

    uv run python integrations/strands/examples/session_example.py
"""

import os

import cognee
from cognee.api.v1.config import config
from cognee.api.v1.visualize import visualize_multi_user_graph
from cognee.modules.users.methods import get_default_user
from cognee_integration_strands import cognee_tools, run_cognee_task
from dotenv import load_dotenv
from strands import Agent
from strands.models.openai import OpenAIModel

load_dotenv()

SESSION_ID = "mission-briefing"
_COGNEE_DIR = os.path.join(os.path.dirname(__file__), "../.cognee")


def visualize(file_name):
    async def _viz():
        user = await get_default_user()
        pairs = [(user, ds) for ds in await cognee.datasets.list_datasets(user=user)]
        path = os.path.join(_COGNEE_DIR, file_name)
        await visualize_multi_user_graph(pairs, destination_file_path=path)
        return path

    print("   graph ->", run_cognee_task(_viz()))


def main():
    config.data_root_directory(os.path.join(_COGNEE_DIR, "data"))
    config.system_root_directory(os.path.join(_COGNEE_DIR, "system"))
    run_cognee_task(cognee.forget(everything=True))

    model = OpenAIModel(client_args={"api_key": os.getenv("LLM_API_KEY")}, model_id="gpt-4o")

    # No session -> permanent graph.
    permanent_tools = cognee_tools()
    # session_id + self_improvement=False -> writes stay in the cache until improve().
    session_tools = cognee_tools(session_id=SESSION_ID, remember_kwargs={"self_improvement": False})

    def ask(tools, prompt):
        # Fresh agent each call so answers come from cognee's memory, not chat history.
        return Agent(model=model, tools=tools)(prompt)

    question = "What is the renewed 2026 value of the Orion Retail Group contract?"

    print("1) Agent A remembers a baseline fact -> permanent graph")
    ask(permanent_tools, 'Remember: "Orion Retail Group" — retail industry, contract worth £850K.')
    visualize("session_before_improve.html")

    print("2) Agent B remembers a renewal -> session cache only")
    ask(
        session_tools,
        "Remember: the Orion Retail Group contract was renewed for 3 years at £2.0M for 2026.",
    )

    print("3) Agent A asks before improve() -> still the old value")
    print(ask(permanent_tools, question))

    print(f"4) Persist the session cache: improve(session_ids=[{SESSION_ID!r}])")
    run_cognee_task(cognee.improve(session_ids=[SESSION_ID]))

    print("5) Agent A asks again -> now the renewed value")
    print(ask(permanent_tools, question))
    visualize("session_after_improve.html")


if __name__ == "__main__":
    main()
