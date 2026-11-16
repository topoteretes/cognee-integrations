URL = "https://api.cognee.ai/{base}/x"
    return value


def _blocked(name: str) -> bool:
    return name in _DENYLIST_EXACT or name.startswith(_DENYLIST_PREFIXES) or name in OPTION_ONLY


def login_email_option() -> str:
    """The login email option (a server without an API key), or ""."""
    return os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_USER_EMAIL", "").strip()
