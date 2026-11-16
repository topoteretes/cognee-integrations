URL = "https://api.cognee.ai/{base}/x"
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_API_KEY", "").strip()


def llm_key_option() -> str:
    """The LLM key option (local mode), or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_LLM_API_KEY", "").strip()


def embedding_key_option() -> str:
    """The embedding key option (local mode), or ""."""
