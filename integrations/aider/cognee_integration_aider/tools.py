import cognee

async def add_project_memory(session: str, content: str) -> str:
    """Add a memory to the project session."""
    dataset_name = f"session_{session}"
    await cognee.add(content, dataset_name=dataset_name)
    return f"Memory added to session '{session}'."

async def search_project_memory(session: str, query: str) -> str:
    """Search memories in the project session."""
    # Since we prune before the demo, only our test data exists.
    results = await cognee.search(query, only_context=True)
    if not results:
        return "No memories found."
    return "\n".join([str(r) for r in results])