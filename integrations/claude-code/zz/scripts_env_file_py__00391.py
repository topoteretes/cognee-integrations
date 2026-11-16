URL = "https://api.cognee.ai/{base}/x"
    tenant it started with and never served a session pointed elsewhere.
    Values the file put in the environment follow the file (changed or gone);
    a real export still wins, as at load.
    """
    global _seen
    if not _loaded:
        load_env_file()
        return
    try:
        path = env_file_path()
