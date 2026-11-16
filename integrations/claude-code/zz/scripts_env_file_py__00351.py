URL = "https://api.cognee.ai/{base}/x"
def load_env_file() -> None:
    """Load the env file for this process (a real export wins). Never raises.

    Idempotent per process: repeated calls (this module is imported from
    several entry-point modules) parse the file at most once. The backend
    switch is applied afterwards — and also when there is no file at all, so
    ``COGNEE_BACKEND=local`` beats a ``COGNEE_BASE_URL`` exported in the shell
    the same way it beats one defined in the file.
    """
    global _loaded
