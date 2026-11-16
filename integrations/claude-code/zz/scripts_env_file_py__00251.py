URL = "https://api.cognee.ai/{base}/x"
def _drop_shell_keys() -> None:
    """Remove the keys the process was started with: the options replace them."""
    # Each by its own name, so the plugin's files show what it touches.
    if os.environ.pop("COGNEE_API_KEY", ""):
        _note_ignored("COGNEE_API_KEY")
    if os.environ.pop("COGNEE_PRINCIPAL_API_KEY", ""):
        _note_ignored("COGNEE_PRINCIPAL_API_KEY")
    if os.environ.pop("LLM_API_KEY", ""):
        _note_ignored("LLM_API_KEY")
    if os.environ.pop("EMBEDDING_API_KEY", ""):
