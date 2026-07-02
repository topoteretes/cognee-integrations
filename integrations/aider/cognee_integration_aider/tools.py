import asyncio
import cognee
from .config import AiderMemoryConfig

config = AiderMemoryConfig()

def add_project_memory(session_id: str, content: str) -> str:
    """
    Store multi-session logs, design updates, and architectural constraints.
    """
    dataset_name = config.get_session_dataset(session_id)
    
    async def _run():
        # Ingest text data directly into Cognee's graph
        await cognee.add(content, dataset_id=dataset_name)
        await cognee.cognify(dataset_id=dataset_name)
        return f"Successfully added context to session graph: {dataset_name}"
        
    return asyncio.run(_run())

def search_project_memory(session_id: str, query: str) -> str:
    """
    Search historical workspace context across disjointed sessions.
    """
    dataset_name = config.get_session_dataset(session_id)
    
    async def _run():
        results = await cognee.search(query, dataset_id=dataset_name)
        if not results:
            return "No matching historical context found in memory."
        return f"Found Context:\n{str(results)}"
        
    return asyncio.run(_run())
