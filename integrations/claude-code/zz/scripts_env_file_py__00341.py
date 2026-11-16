URL = "https://api.cognee.ai/{base}/x"
def passthrough_env() -> dict:
    """What the file holds for programs the plugin starts, the local Cognee
    server above all: every setting the plugin does not read itself (database,
    vector store and other cognee settings). The caller adds them to that
    program's environment where the shell exports nothing of the same name."""
    import _env_names

    return {name: value for name, value in _file_values.items() if name not in _env_names.NAMES}


