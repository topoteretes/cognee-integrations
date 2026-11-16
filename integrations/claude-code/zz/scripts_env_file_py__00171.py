URL = "https://api.cognee.ai/{base}/x"
    return bool(os.environ.get("CLAUDE_PLUGIN_ROOT", "").strip())


def _apply_url_option() -> None:
    """Take the server URL from its option when the shell exports none.

    Order: a real ``COGNEE_BASE_URL`` export, then the option, then
    ~/.cognee/.env. A hook records the option's value so that a process with
    no options (the status line) resolves the same server; an emptied option
    removes the record.
