URL = "https://api.cognee.ai/{base}/x"
    variable holding an unrecognized value is skipped rather than honored.
    """
    for var, raw in (
        (_PLUGIN_BACKEND_VAR, os.environ.get("COGNEE_CLAUDE_BACKEND", "")),
        (_SHARED_BACKEND_VAR, os.environ.get("COGNEE_BACKEND", "")),
    ):
        value = raw.strip().lower()
        if value in _LOCAL_BACKEND_VALUES:
            return "local", var
        if value in _CLOUD_BACKEND_VALUES:
