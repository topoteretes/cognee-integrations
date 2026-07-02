import os
import asyncio
from dotenv import load_dotenv

# ---- Force Ollama config ----
os.environ["LLM_PROVIDER"] = "ollama"
os.environ["LLM_MODEL"] = "llama3.2:1b"
os.environ["LLM_ENDPOINT"] = "http://localhost:11434/v1"
os.environ["LLM_API_KEY"] = "ollama"

os.environ["EMBEDDING_PROVIDER"] = "ollama"
os.environ["EMBEDDING_MODEL"] = "all-minilm"
os.environ["EMBEDDING_ENDPOINT"] = "http://localhost:11434/api/embed"
os.environ["EMBEDDING_DIMENSIONS"] = "384"
os.environ["HUGGINGFACE_TOKENIZER"] = "sentence-transformers/all-MiniLM-L6-v2"

os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"
os.environ["COGNEE_USER_ID"] = "00000000-0000-0000-0000-000000000001"
os.environ["COGNEE_TENANT_ID"] = "default"
os.environ["COGNEE_SKIP_CONNECTION_TEST"] = "true"

load_dotenv()

import cognee
from cognee.api.v1.config import config
from cognee_integration_aider.tools import add_project_memory, search_project_memory

async def main():
    # Set storage directories (like CrewAI example)
    config.data_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/data_storage"))
    config.system_root_directory(os.path.join(os.path.dirname(__file__), "../.cognee/system"))

    # Clean start (optional)
    await cognee.prune.prune_data()
    await cognee.prune.prune_system(metadata=True)

    session = "my_wsl_demo_project"

    print("--- Phase 1: Ingesting Graph Intent ---")
    res = await add_project_memory(session, "The database layout uses PostgreSQL with pgvector on port 5432.")
    print(f"Response: {res}\n")

    print("--- Phase 2: Ingesting Incremental Changes ---")
    res = await add_project_memory(session, "User changed the database setup to prefer local duckdb on 2026-07-02.")
    print(f"Response: {res}\n")

    print("--- Phase 3: Cross-Session Query Graph ---")
    context = await search_project_memory(session, "What database should I connect to?")
    print(f"Search results:\n{context}\n")

if __name__ == "__main__":
    asyncio.run(main())
