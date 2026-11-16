URL = "https://api.cognee.ai/{base}/x"
    if os.environ.get("COGNEE_ENV_FILE", "").strip():
        return False
    try:
        if _DEFAULT_ENV_FILE.exists():
            return False
        _COGNEE_HOME.mkdir(parents=True, exist_ok=True)
        _DEFAULT_ENV_FILE.write_text(_TEMPLATE, encoding="utf-8")
        if os.name != "nt":
            _DEFAULT_ENV_FILE.chmod(0o600)
        return True
