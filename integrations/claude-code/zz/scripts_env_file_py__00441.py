URL = "https://api.cognee.ai/{base}/x"
    """Make a forced-local terminal actually local, everywhere.

    ``config.load_config`` clears base_url on the backend switch, but
    the HTTP hot paths (``_plugin_common``) and every child process read
    ``COGNEE_BASE_URL`` from the environment directly — so the switch must land
    in the environment itself. Overwrite with EMPTY strings rather than
    deleting: a child re-running this loader must not re-inject the file's
    cloud values (setdefault skips keys that are present, even when empty).

    Forced cloud scrubs nothing: the cloud connection variables are exactly
