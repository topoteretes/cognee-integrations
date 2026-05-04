#!/usr/bin/env python3
"""
Cognee MCP server startup script.

Invoked from Claude Code's MCP configuration.
Resolves the distribution root automatically, loads config/.env into the
environment, and launches `cognee-mcp` from the venv.

Registration command:
  claude mcp add cognee --scope user src/main_src/start_cognee_mcp.py
"""
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# Resolve the distribution root from this file's location.
# src/main_src/start_cognee_mcp.py -> src/main_src -> src -> distribution root
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
ENV_FILE = PROJECT_ROOT / "config" / ".env"
VENV_BIN = PROJECT_ROOT / "src" / "venv" / "bin"
COGNEE_MCP = VENV_BIN / "cognee-mcp"


def load_env_file(path: Path) -> None:
    """Load KEY=VALUE pairs from .env into os.environ.

    The cognee-mcp child process inherits os.environ via execv, so the
    LLM_API_KEY / LLM_ENDPOINT / SYSTEM_ROOT_DIRECTORY etc. configured in
    config/.env are made visible to the cognee runtime regardless of cwd.
    Existing environment variables take precedence over .env values.
    """
    if not path.exists():
        logger.warning(".env not found at %s (continuing without it)", path)
        return

    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main() -> None:
    # Verify the cognee-mcp binary exists
    if not COGNEE_MCP.exists():
        logger.error("cognee-mcp not found: %s", COGNEE_MCP)
        logger.error("Please create the venv following the setup guide")
        sys.exit(1)

    # Load config/.env into the current environment so that the spawned
    # cognee-mcp process inherits LLM credentials and storage paths.
    load_env_file(ENV_FILE)

    # Change to the project root (some Cognee internals resolve relative
    # paths against cwd).
    os.chdir(PROJECT_ROOT)
    logger.info("Starting: %s", COGNEE_MCP)

    try:
        # Replace the current process with cognee-mcp (using execv for memory efficiency)
        os.execv(str(COGNEE_MCP), [str(COGNEE_MCP)])
    except OSError as e:
        logger.error("Failed to launch cognee-mcp: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
