#!/usr/bin/env python3
"""
Cognee remember queue flusher

Drains entries appended by auto_remember_user_message.py and
auto_remember_completion.py into ~/.claude/cognee_pending_remembers.jsonl,
and persists each one into Cognee graph memory by calling `remember`
on the cognee MCP server.

Calling MCP directly from inside the UserPromptSubmit/Stop hooks
would delay the AI turn, so the hooks only append to a queue file
and this flusher runs separately to process them.

How to run:
- One-shot:  python3 cognee_remember_flusher.py
- Daemon:    nohup python3 cognee_remember_flusher.py --daemon &
- Cron (every 5 minutes):
    */5 * * * * /usr/bin/python3 /home/<youruser>/.claude/hooks/cognee_remember_flusher.py

Dependencies:
- The base distribution must be set up
- Either set COGNEE_GRAPH_MEMORY_ROOT to the distribution root, or
  place it at ~/tools/claude-code-tools/claude-code-cognee-graph-memory

Design notes:
- Read the queue line by line; on success the line is removed
- Failed entries are moved to ~/.claude/cognee_failed_remembers.jsonl
  for later inspection / re-injection
- Concurrent runs are prevented via flock
"""
import argparse
import asyncio
import fcntl
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

QUEUE_PATH = Path.home() / ".claude" / "cognee_pending_remembers.jsonl"
FAILED_PATH = Path.home() / ".claude" / "cognee_failed_remembers.jsonl"
LOCK_PATH = Path.home() / ".claude" / "cognee_flusher.lock"
LOG_PATH = Path.home() / ".claude" / "cognee_flusher.log"

# Distribution root candidates (env var takes precedence)
DEFAULT_ROOT_CANDIDATES = [
    Path(os.environ.get("COGNEE_GRAPH_MEMORY_ROOT", "")),
    Path.home() / "tools" / "claude-code-tools" / "claude-code-cognee-graph-memory",
]


def find_project_root() -> Path | None:
    for p in DEFAULT_ROOT_CANDIDATES:
        if p and p.exists() and (p / "src" / "main_src" / "import_to_graph.py").exists():
            return p
    return None


def log(msg: str) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] {msg}\n")


async def remember_via_mcp(project_root: Path, data: str, dataset_name: str) -> bool:
    """
    Call `remember` on the cognee MCP server via fastmcp StdioTransport.
    Returns True on success, False on failure.
    """
    try:
        # fastmcp lives in the distribution's venv
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        env = os.environ.copy()
        env_path = project_root / "config" / ".env"
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()

        transport = StdioTransport(
            command=str(project_root / "src" / "venv" / "bin" / "python3"),
            args=[str(project_root / "src" / "main_src" / "start_cognee_mcp.py")],
            env=env,
        )

        async with Client(transport) as client:
            # result =
            await client.call_tool(
                "remember",
                {"data": data, "dataset_name": dataset_name},
            )
            log(f"OK: dataset={dataset_name} len={len(data)}")
            return True
    except Exception as e:
        log(f"FAIL: {type(e).__name__}: {e}")
        return False


def acquire_lock():
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fp = LOCK_PATH.open("w")
    try:
        fcntl.flock(fp.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fp
    except BlockingIOError:
        return None


def append_failed(entry: dict) -> None:
    FAILED_PATH.parent.mkdir(parents=True, exist_ok=True)
    with FAILED_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


async def flush_once() -> None:
    if not QUEUE_PATH.exists():
        return

    project_root = find_project_root()
    if not project_root:
        log("ERROR: claude-code-cognee-graph-memory project root not found")
        return

    # Read queue, attempt every entry, drop succeeded ones
    lines = QUEUE_PATH.read_text(encoding="utf-8").splitlines()
    if not lines:
        return

    succeeded_indices = set()
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            succeeded_indices.add(i)
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            log(f"SKIP malformed line: {line[:80]}")
            succeeded_indices.add(i)
            continue

        ok = await remember_via_mcp(
            project_root,
            entry["data"],
            entry.get("dataset_name", "main_dataset"),
        )
        if ok:
            succeeded_indices.add(i)
        else:
            append_failed(entry)

    # Keep failed valid-data entries in the queue (drop succeeded and blank lines;
    # failed entries are also saved to failed.jsonl for reference)
    remaining = [
        line for i, line in enumerate(lines)
        if i not in succeeded_indices and line.strip()
    ]
    if remaining:
        QUEUE_PATH.write_text("\n".join(remaining) + "\n", encoding="utf-8")
    else:
        QUEUE_PATH.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--daemon", action="store_true", help="run continuously at fixed intervals")
    parser.add_argument(
        "--interval", type=int, default=60,
        help="seconds between runs in --daemon mode (default: 60)"
    )
    args = parser.parse_args()

    lock = acquire_lock()
    if not lock:
        log("Another flusher is already running. Exit.")
        sys.exit(0)

    if args.daemon:
        while True:
            asyncio.run(flush_once())
            time.sleep(args.interval)
    else:
        asyncio.run(flush_once())


if __name__ == "__main__":
    main()
