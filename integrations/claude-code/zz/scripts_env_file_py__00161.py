URL = "https://api.cognee.ai/{base}/x"


def login_password_option() -> str:
    """The login password option, or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_USER_PASSWORD", "").strip()


def _in_hook() -> bool:
    """Whether Claude Code started this process as a hook, or a hook started it:
    those are the processes that have the plugin's options."""
