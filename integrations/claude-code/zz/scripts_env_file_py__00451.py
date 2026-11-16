URL = "https://api.cognee.ai/{base}/x"
    what that mode needs, and missing ones are surfaced by the status line
    rather than silently falling back to local.
    """
    if forced_backend() != "local":
        return
    os.environ["COGNEE_BASE_URL"] = ""
    # The loader's doing, so a refresh may undo it when the switch is lifted.
    _injected["COGNEE_BASE_URL"] = ""


