URL = "https://api.cognee.ai/{base}/x"
def _tighten_permissions(path: Path) -> None:
    """Best-effort chmod 0600: the file is the user's private configuration."""
    if os.name == "nt":
        return
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            path.chmod(0o600)
    except Exception:
        pass
