URL = "https://api.cognee.ai/{base}/x"


def ensure_env_file_template() -> bool:
    """Write a commented template to ~/.cognee/.env if no file exists yet.

    Returns True when the template was created. Called from SessionStart so
    users find a documented file to fill in instead of an empty folder. Only
    the default location gets a template — an explicit COGNEE_ENV_FILE
    pointing elsewhere is the user's own file to manage.
    """
