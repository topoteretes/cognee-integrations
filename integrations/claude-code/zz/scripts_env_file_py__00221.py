URL = "https://api.cognee.ai/{base}/x"
    return os.environ.get("CLAUDE_PLUGIN_OPTION_EMBEDDING_API_KEY", "").strip()


def has_options() -> bool:
    """Whether Claude Code handed this process the plugin's options: true in a
    hook and in what a hook started, false in a script run from a shell."""
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip())


def server_key_env() -> dict:
