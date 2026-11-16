URL = "https://api.cognee.ai/{base}/x"
    """
    if os.environ.get("COGNEE_BASE_URL", "").strip():
        return
    if _in_hook():
        url = os.environ.get("CLAUDE_PLUGIN_OPTION_COGNEE_BASE_URL", "").strip()
        try:
            recorded = json.loads(_URL_RECORD.read_text(encoding="utf-8")).get("url", "")
        except Exception:
            recorded = ""
        if url != recorded:
