"""End-to-end demo of the Aider Cognee memory integration.

Stores memories in two separate project sessions and shows that a search
scoped to one session does not see the other's data — i.e. real per-project
isolation.

Reads provider config from ``.env`` (copy ``.env.example`` first). Works with
OpenAI by default, or Ollama for a free local run. Then:

    uv run python examples/aider_memory_demo.py
"""

import asyncio
import os

import cognee
from cognee_integration_aider import get_sessionized_cognee_tools
from dotenv import load_dotenv

load_dotenv()


async def main():
    from cognee.api.v1.config import config

    # Keep all state inside this integration's directory (as the crewai example does).
    here = os.path.dirname(__file__)
    config.data_root_directory(os.path.join(here, "../.cognee/data_storage"))
    config.system_root_directory(os.path.join(here, "../.cognee/system"))

    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    backend_add, backend_search = get_sessionized_cognee_tools("backend")
    mobile_add, mobile_search = get_sessionized_cognee_tools("mobile")

    print("--- Storing memory in two separate projects ---")
    print(await backend_add("The backend uses PostgreSQL with pgvector on port 5432."))
    print(await mobile_add("The mobile app is built with React Native."))

    question = "What database does this project use?"

    print("\n--- Recall in the 'backend' project (knows about PostgreSQL) ---")
    print(await backend_search(question))

    # The same question in the 'mobile' project: only a leaky, non-isolated
    # implementation (shared dataset) would surface PostgreSQL here.
    print("\n--- Same question in the 'mobile' project (should NOT know PostgreSQL) ---")
    print(await mobile_search(question))

    print("\nThe mobile answer cannot mention PostgreSQL — memory is isolated per project.")


if __name__ == "__main__":
    asyncio.run(main())
