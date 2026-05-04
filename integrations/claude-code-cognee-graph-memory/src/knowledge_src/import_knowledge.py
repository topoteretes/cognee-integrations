"""
Ingest pre-split knowledge files (user_chunks/) into Cognee graph memory.

Files split by split_knowledge.py are ingested one at a time via the Cognee MCP
server. After each file, the cognify result is checked; if status=errored, the
ingestion is retried up to 3 times.

Usage:
    cd <distribution root directory>
    src/venv/bin/python3 src/knowledge_src/import_knowledge.py
    src/venv/bin/python3 src/knowledge_src/import_knowledge.py --dry-run

Input:  .md files under knowledge/user_chunks/
Output: Cognee user_knowledge dataset
"""
import argparse
import asyncio
import json
import logging
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StdioTransport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent.parent
ENV_PATH = PROJECT_ROOT / "config" / ".env"
MCP_COMMAND = str(PROJECT_ROOT / "src" / "main_src" / "start_cognee_mcp.py")
INPUT_DIR = PROJECT_ROOT / "knowledge" / "user_chunks"
DATASET_NAME = "user_knowledge"
MAX_RETRY = 3


def check_ollama() -> None:
    """Verify Ollama is running and the LLM model specified in config/.env is available."""
    env = _load_env()
    llm_provider = env.get("LLM_PROVIDER", "").strip().lower()
    if llm_provider != "ollama":
        logger.info("LLM_PROVIDER=%s; skipping Ollama connectivity check",
                    llm_provider or "(unset)")
        return

    llm_model = env.get("LLM_MODEL", "").strip()
    if not llm_model:
        logger.error("LLM_MODEL is not set in config/.env")
        sys.exit(1)

    url = "http://localhost:11434/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            data = json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        logger.error("Cannot reach Ollama: %s", e)
        logger.error("Run 'ollama serve' and try again")
        sys.exit(1)
    except Exception as e:
        logger.error("Unexpected error during Ollama check: %s", e)
        sys.exit(1)

    model_names = [m.get("name", "") for m in data.get("models", [])]
    if not any(llm_model in name for name in model_names):
        logger.error("%s not found. Available models: %s", llm_model, model_names)
        logger.error("Run 'ollama pull %s' and try again", llm_model)
        sys.exit(1)

    logger.info("Ollama check OK: %s is available", llm_model)


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


async def import_one(client: Client, file_path: Path, idx: int, total: int) -> bool:
    """Ingest one file. Returns True on success, False on failure."""
    rel = file_path.relative_to(INPUT_DIR)
    content = file_path.read_text(encoding="utf-8")

    for attempt in range(1, MAX_RETRY + 1):
        try:
            result = await client.call_tool("remember", {
                "data": content,
                "dataset_name": DATASET_NAME,
            })
            text = result.content[0].text if result and result.content else ""

            if "status=completed" in text:
                logger.info("[%d/%d] OK %s (attempt %d)", idx, total, rel, attempt)
                return True

            if "status=errored" in text:
                logger.warning("[%d/%d] FAIL %s (attempt %d/%d): errored",
                               idx, total, rel, attempt, MAX_RETRY)
                if attempt < MAX_RETRY:
                    await asyncio.sleep(2)
                    continue
                return False

            # Unexpected status
            logger.warning("[%d/%d] Unexpected response: %s", idx, total, text[:120])
            return False
        except Exception as e:
            logger.warning("[%d/%d] Exception (attempt %d/%d): %s",
                           idx, total, attempt, MAX_RETRY, e)
            if attempt < MAX_RETRY:
                await asyncio.sleep(2)
                continue
            return False

    return False


async def import_all(files: list[Path], dry_run: bool) -> tuple[int, int]:
    """Ingest all files. Returns (success_count, failure_count)."""
    success = 0
    failed = 0
    total = len(files)

    if dry_run:
        for idx, f in enumerate(files, 1):
            rel = f.relative_to(INPUT_DIR)
            logger.info("[%d/%d] (dry-run) %s", idx, total, rel)
        return total, 0

    async with Client(make_transport()) as client:
        for idx, f in enumerate(files, 1):
            ok = await import_one(client, f, idx, total)
            if ok:
                success += 1
            else:
                failed += 1
                logger.error("3 failures, stopping: %s", f.relative_to(INPUT_DIR))
                logger.error(
                    "Halting so failure is not silently ignored. Investigate cause and rerun."
                )
                break

    return success, failed


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(description="Ingest user_chunks/ into Cognee")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the file list without ingesting")
    args = parser.parse_args()

    if not INPUT_DIR.exists():
        logger.error("Input folder not found: %s", INPUT_DIR)
        logger.error("Run split_knowledge.py first")
        sys.exit(1)

    md_files = sorted(INPUT_DIR.glob("**/*.md"))
    # README.md is excluded from ingestion
    md_files = [f for f in md_files if f.name != "README.md"]

    if not md_files:
        logger.warning("No .md files to ingest under: %s", INPUT_DIR)
        logger.warning("Run split_knowledge.py first")
        sys.exit(1)

    if not args.dry_run:
        check_ollama()

    logger.info("Files to ingest: %d -> dataset=%s", len(md_files), DATASET_NAME)
    success, failed = asyncio.run(import_all(md_files, args.dry_run))

    logger.info("=" * 50)
    logger.info("Succeeded: %d", success)
    logger.info("Failed: %d", failed)

    if failed > 0:
        sys.exit(1)


if __name__ == "__main__":
    main()
