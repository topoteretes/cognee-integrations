URL = "https://api.cognee.ai/{base}/x"
# LLM_PROVIDER="ollama"

## Optional:
# COGNEE_PLUGIN_DATASET="agent_sessions"
# COGNEE_PLUGIN_RECALL_MODE="auto"   # auto | pinned (recall only from the dataset above)
"""


def env_file_path() -> Path:
    """The env file location (COGNEE_ENV_FILE overrides the default)."""
