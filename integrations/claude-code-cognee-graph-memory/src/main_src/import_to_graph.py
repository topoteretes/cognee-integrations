"""
Production-time MD file ingestion script.

Used during production runtime (while Claude Code is running) to ingest
knowledge into Cognee graph memory. Besides loading the bundled samples,
this is also the production path called from Claude Code on demand.

Usage:
  cd <distribution root directory>
  src/venv/bin/python3 src/main_src/import_to_graph.py --target sample
  src/venv/bin/python3 src/main_src/import_to_graph.py --list-targets

Targets:
  - sample: bundled samples (knowledge/sample_knowledge/)

Note:
  - For initial bulk ingestion, use src/knowledge_src/import_knowledge.py
    (it supports retries on cognify failure and batched execution).
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
PROJECT_ROOT = SCRIPT_DIR.parent.parent  # src/main_src -> src -> distribution root
WORKSPACE = PROJECT_ROOT.parent
ENV_PATH = PROJECT_ROOT / "config" / ".env"
MCP_COMMAND = str(PROJECT_ROOT / "src" / "main_src" / "start_cognee_mcp.py")


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
    return StdioTransport(
        command=str(PROJECT_ROOT / "src" / "venv" / "bin" / "python3"),
        args=[MCP_COMMAND],
        env=_load_env(),
    )


TARGET_MAP = {
    "sample": {
        "path": PROJECT_ROOT / "knowledge" / "sample_knowledge",
        "dataset_name": "sample_knowledge",
        "description": "Bundled sample knowledge data (for verification)",
        "glob": "*.md",
    },
}


def collect_files(target_key: str) -> list[tuple[Path, str]]:
    """Return a list of (file, dataset_name) pairs."""
    if target_key not in TARGET_MAP:
        logger.error("Unknown target: %s", target_key)
        sys.exit(1)

    cfg = TARGET_MAP[target_key]
    base_path = cfg["path"]
    if not base_path.exists():
        logger.warning("Path does not exist: %s", base_path)
        return []

    try:
        files = sorted(base_path.glob(cfg["glob"]))
    except Exception as e:
        logger.error("Failed to enumerate files (%s): %s", base_path, e)
        sys.exit(1)

    return [(f, cfg["dataset_name"]) for f in files if f.is_file()]


async def import_files(files: list[tuple[Path, str]], dry_run: bool = False) -> None:
    """Ingest files into Cognee one by one."""
    if not files:
        logger.info("No files to ingest.")
        return

    logger.info("Ingestion target: %d files", len(files))

    async with Client(make_transport()) as client:
        for i, (file_path, dataset_name) in enumerate(files, 1):
            logger.info("[%d/%d] %s -> dataset=%s", i, len(files), file_path.name, dataset_name)
            if dry_run:
                logger.info("  (dry-run: skipped)")
                continue
            try:
                content = file_path.read_text(encoding="utf-8")
                result = await client.call_tool("remember", {
                    "data": content,
                    "dataset_name": dataset_name,
                })
                text = result.content[0].text if result and result.content else ""
                logger.info("  -> %s...", text[:80])
            except Exception as e:
                logger.error("  Ingestion failed (%s): %s", file_path.name, e)


def list_targets() -> None:
    """Print the list of available targets."""
    logger.info("Available targets:")
    for key, cfg in TARGET_MAP.items():
        path = cfg["path"]
        exists = "OK" if path.exists() else "MISSING (path not found)"
        logger.info("  %s: %s [%s]", key, cfg["description"], exists)
        logger.info("         Path: %s", path)


def main() -> None:
    """Entry point."""
    parser = argparse.ArgumentParser(
        description="Ingest MD files into Cognee graph memory (called from Claude Code at runtime)"
    )
    parser.add_argument("--target", help="Ingestion target (sample)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the file list without ingesting")
    parser.add_argument("--list-targets", action="store_true",
                        help="Print the list of available targets")
    args = parser.parse_args()

    if args.list_targets:
        list_targets()
        return

    if not args.target:
        parser.print_help()
        sys.exit(1)

    if not args.dry_run:
        check_ollama()

    files = collect_files(args.target)
    asyncio.run(import_files(files, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
