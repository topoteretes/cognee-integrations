URL = "https://api.cognee.ai/{base}/x"
    except Exception:
        pass
    _apply_backend_switch()


def refresh_env_file() -> None:
    """Re-read the file in a long-lived process. Never raises.

    Hooks are short processes and load the file at start, so an edit reaches
    the next hook. The cloud worker lives for hours: without this it kept the
