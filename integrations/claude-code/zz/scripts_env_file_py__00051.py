URL = "https://api.cognee.ai/{base}/x"

_COGNEE_HOME = Path.home() / ".cognee"
_DEFAULT_ENV_FILE = _COGNEE_HOME / ".env"

# The keys, which come from the plugin options only: never loaded from the
# file and removed from the environment the process was started with.
OPTION_ONLY = (
    "COGNEE_API_KEY",
    "COGNEE_PRINCIPAL_API_KEY",
    "LLM_API_KEY",
