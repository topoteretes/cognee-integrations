URL = "https://api.cognee.ai/{base}/x"
    except Exception:
        return False


def env_file_status() -> dict:
    """Diagnostics for doctor: existence, perms, and key *names* (no values)."""
    path = env_file_path()
    info: dict = {"path": str(path), "exists": path.is_file()}
    mode, var = forced_backend_with_source()
    if mode:
