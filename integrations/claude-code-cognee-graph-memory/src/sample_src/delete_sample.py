"""
Delete the sample_knowledge dataset from Cognee graph memory.

Use this once you have finished verifying with the bundled samples and want
to start clean with only your own knowledge. Other datasets (e.g. user_knowledge)
are not affected.

Usage:
    cd <distribution root directory>
    src/venv/bin/python3 src/sample_src/delete_sample.py
"""
import asyncio
import logging
import os
import sys
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# Resolve the distribution root from this file's location.
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
ENV_PATH = PROJECT_ROOT / "config" / ".env"
MCP_COMMAND = str(PROJECT_ROOT / "src" / "main_src" / "start_cognee_mcp.py")
DATASET_NAME = "sample_knowledge"


def _load_env() -> dict[str, str]:
    """Load config/.env and return the resulting environment dict."""
    env = os.environ.copy()
    if ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
        except OSError as e:
            logger.warning("Failed to read .env (skipping): %s", e)
    return env


def make_transport() -> StdioTransport:
    """Build the StdioTransport for connecting to the Cognee MCP server."""
    return StdioTransport(
        command=str(PROJECT_ROOT / "src" / "venv" / "bin" / "python3"),
        args=[MCP_COMMAND],
        env=_load_env(),
    )


async def delete_sample_dataset() -> None:
    """Delete the sample_knowledge dataset."""
    logger.info("Dataset to delete: %s", DATASET_NAME)
    async with Client(make_transport()) as client:
        try:
            result = await client.call_tool("delete_dataset", {
                "dataset_name": DATASET_NAME,
            })
            text = result.content[0].text if result and result.content else ""
            logger.info("Delete result: %s", text)
        except Exception as e:
            logger.error("Delete failed: %s", e)
            sys.exit(1)


def main() -> None:
    """Entry point."""
    asyncio.run(delete_sample_dataset())


if __name__ == "__main__":
    main()
