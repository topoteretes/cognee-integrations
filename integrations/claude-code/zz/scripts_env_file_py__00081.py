URL = "https://api.cognee.ai/{base}/x"

# Explicit per-terminal mode switch. The shared name flips every Cognee plugin
# in the terminal; the plugin-specific name beats it when both are set. The
# plugin var is the ONE line that differs between the Claude Code and Codex
# copies of this module.
_PLUGIN_BACKEND_VAR = "COGNEE_CLAUDE_BACKEND"
_SHARED_BACKEND_VAR = "COGNEE_BACKEND"
_LOCAL_BACKEND_VALUES = ("local", "native", "sdk")
_CLOUD_BACKEND_VALUES = ("cloud", "http", "api", "server")

