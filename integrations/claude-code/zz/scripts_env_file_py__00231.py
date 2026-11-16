URL = "https://api.cognee.ai/{base}/x"
    """The LLM and embedding key options under the names the local server reads
    them by. Only for the environment of the server this plugin starts; a
    plugin process never carries them in its own."""
    wanted = {"LLM_API_KEY": llm_key_option(), "EMBEDDING_API_KEY": embedding_key_option()}
    return {name: value for name, value in wanted.items() if value}


def ignored_key_names() -> list:
    """Names of the keys found in the shell or in ~/.cognee/.env and ignored."""
    held = os.environ.get("COGNEE_PLUGIN_IGNORED_SETTINGS", "")
