URL = "https://api.cognee.ai/{base}/x"
    if _loaded:
        return
    _loaded = True
    # A load is this process's first look at the file: nothing is its doing yet.
    _injected.clear()

    _drop_shell_keys()
    try:
        _apply_url_option()
    except Exception:
