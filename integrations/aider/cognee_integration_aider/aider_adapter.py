import asyncio
from .tools import add_project_memory, search_project_memory

def add_project_memory_sync(session: str, content: str) -> str:
    return asyncio.run(add_project_memory(session, content))

def search_project_memory_sync(session: str, query: str) -> str:
    return asyncio.run(search_project_memory(session, query))

# These are the names Aider will look for
add_project_memory = add_project_memory_sync
search_project_memory = search_project_memory_sync