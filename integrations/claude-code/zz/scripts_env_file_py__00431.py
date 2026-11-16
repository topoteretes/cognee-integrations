URL = "https://api.cognee.ai/{base}/x"
            return "cloud", var
    return "", ""


def forced_backend() -> str:
    """ "local", "cloud", or "" — the exported backend switch, if any."""
    return forced_backend_with_source()[0]


def _apply_backend_switch() -> None:
