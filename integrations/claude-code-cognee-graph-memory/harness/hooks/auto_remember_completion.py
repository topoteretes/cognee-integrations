#!/usr/bin/env python3
"""
Stop hook: queue AI response summaries for Cognee graph memory

When Claude Code finishes a turn, capture the gist of the AI's last
response and enqueue it for Cognee graph memory. This makes "what
did the AI do" traceable across sessions.

The Stop hook fires at the end of an AI turn. We pull the last
assistant message from the transcript, truncate it if needed, and
push it onto the queue.

Same queue mechanism as auto_remember_user_message.py:
- Read transcript_path from stdin (JSON)
- Extract the last assistant message
- Append to ~/.claude/cognee_pending_remembers.jsonl
- A separate flusher process drains the queue and calls `remember`

Input: JSON on stdin: {"transcript_path": "...", "session_id": "..."}
Output: exit 0 (always allowed; failure to record must never block turn end)

Design notes:
- An assistant response can be very long. We keep the head and the tail
  (up to 1000 + 1000 chars), with an ellipsis in between, instead of
  recording the entire message.
- Dataset name: "ai_responses" (separated from user_messages)
- For finer-grained capture you could add separate hooks that record
  into "incidents" or "decisions" datasets when files change. This
  sample writes everything into "ai_responses".

Setup:
  1. Copy this file into ~/.claude/hooks/
  2. Register it under hooks.Stop in ~/.claude/settings.json
     (see settings.example.json)
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# import os

# Target dataset name for AI response summaries
DATASET_NAME = "ai_responses"

# Truncation thresholds for long responses (head + tail)
MAX_HEAD_CHARS = 1000
MAX_TAIL_CHARS = 1000


def extract_last_assistant_message(transcript_path: str) -> str:
    """
    Extract the text of the last assistant message from a Claude Code
    transcript (JSONL: one JSON object per line). Concatenate the
    `content[].text` segments of the assistant turn.
    """
    path = Path(transcript_path)
    if not path.exists():
        return ""

    last_assistant_text = ""
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                # Only assistant turns
                role = entry.get("role") or entry.get("type", "")
                if role != "assistant":
                    continue

                content = entry.get("content") or entry.get("message", {}).get("content", [])
                if isinstance(content, str):
                    last_assistant_text = content
                elif isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            parts.append(c.get("text", ""))
                    if parts:
                        last_assistant_text = "\n".join(parts)
    except Exception:
        return ""

    return last_assistant_text


def truncate(text: str) -> str:
    """Keep head + tail of long responses with an ellipsis in between."""
    if len(text) <= MAX_HEAD_CHARS + MAX_TAIL_CHARS:
        return text
    head = text[:MAX_HEAD_CHARS]
    tail = text[-MAX_TAIL_CHARS:]
    return f"{head}\n\n... (truncated) ...\n\n{tail}"


def queue_remember(message: str, session_id: str) -> None:
    queue_path = Path.home() / ".claude" / "cognee_pending_remembers.jsonl"
    queue_path.parent.mkdir(parents=True, exist_ok=True)

    entry = {
        "timestamp": datetime.now().isoformat(),
        "session_id": session_id,
        "dataset_name": DATASET_NAME,
        "data": f"[ai response {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}",
    }

    with queue_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main() -> None:
    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        transcript_path = data.get("transcript_path", "")
        session_id = data.get("session_id", "unknown")

        if not transcript_path:
            sys.exit(0)

        text = extract_last_assistant_message(transcript_path)
        if not text:
            sys.exit(0)

        truncated = truncate(text)
        queue_remember(truncated, session_id)

    except Exception:
        # Recording failures must never block the turn end
        pass

    sys.exit(0)


if __name__ == "__main__":
    main()
