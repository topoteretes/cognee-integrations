#!/usr/bin/env python3
"""
UserPromptSubmit hook: queue user messages for Cognee graph memory

Captures the prompt the user submits, and enqueues it so that it can
be persisted into Cognee graph memory via the cognee MCP `remember`
tool. This is what enables cross-session context retrieval.

How it works:
- The UserPromptSubmit hook reads the prompt from stdin
- The prompt is appended to ~/.claude/cognee_pending_remembers.jsonl
- A separate process (cognee_remember_flusher.py) drains the queue and
  actually calls `remember` on the cognee MCP server

Calling MCP directly from inside the hook would delay the start of the
AI turn, so this implementation uses an asynchronous file-queue approach.

Input: JSON on stdin: {"prompt": "...", "session_id": "..."}
Output: exit 0 (always allowed; failure to record must never block the prompt)

Setup:
  1. Copy this file into ~/.claude/hooks/
  2. Register it under hooks.UserPromptSubmit in ~/.claude/settings.json
     (see settings.example.json)
  3. Make sure the cognee MCP server is registered (`claude mcp list`)

Design notes:
- Every user message is recorded (no filtering).
  Rationale: filtering at write time means anything you didn't think
  was important right now is lost. Graph memory is designed for
  large accumulation; search can extract what is needed later.
- Dataset name: "user_messages" (separated by purpose)
- Failure to record must never block the prompt (exit 0 always)
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# import os
# import subprocess

# Skip very short messages (acknowledgements, single-word replies)
MIN_LENGTH = 5

# Target dataset name for user messages
DATASET_NAME = "user_messages"

# The cognee MCP server is expected to be registered via
# `claude mcp add cognee --scope user <PROJECT_ROOT>/src/main_src/start_cognee_mcp.py`


def queue_remember(message: str, session_id: str) -> None:
    """
    Append the message to ~/.claude/cognee_pending_remembers.jsonl.
    A flusher process picks up entries from this file and calls
    `remember` against the cognee MCP server.
    """
    queue_path = Path.home() / ".claude" / "cognee_pending_remembers.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "dataset_name": DATASET_NAME,
        "data": f"[user message {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
    }

    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        message = data.get("prompt", data.get("message", "")).strip()
        session_id = data.get("session_id", "unknown")

        if not message:
            sys.exit(0)

        if len(message) < MIN_LENGTH:
            sys.exit(0)

        queue_remember(message, session_id)

    except Exception:
        # Recording failures must never block the user's prompt
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
