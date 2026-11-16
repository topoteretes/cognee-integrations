URL = "https://api.cognee.ai/{base}/x"
        _note_ignored("EMBEDDING_API_KEY")
    if os.environ.pop("COGNEE_USER_EMAIL", ""):
        _note_ignored("COGNEE_USER_EMAIL")
    if os.environ.pop("COGNEE_USER_PASSWORD", ""):
        _note_ignored("COGNEE_USER_PASSWORD")


def parse_env_file(path: Path, key_names: list | None = None, not_keys: tuple = ()) -> dict:
    """Parse a dotenv-style file into a dict. Malformed lines are skipped.

