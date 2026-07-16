#!/usr/bin/env python3
"""Clear Claude Code transcript context after each assistant response.

Claude Code hooks cannot execute the built-in /clear command directly. This
Stop hook is the integration-level demo workaround: when enabled, it empties
the transcript file Claude passes in the hook payload.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

ENV_NAME = "COGNEE_CLAUDE_CLEAR_AFTER_MESSAGE"
TRUTHY = {"1", "true", "yes", "on"}
PLUGIN_DIR = Path.home() / ".cognee-plugin" / "claude-code"
LOG_FILE = PLUGIN_DIR / "hook.log"


def _enabled() -> bool:
    return os.environ.get(ENV_NAME, "").strip().lower() in TRUTHY


def _log(event: str, detail: dict | None = None) -> None:
    try:
        PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
        line = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "pid": os.getpid(),
            "event": event,
        }
        if detail:
            line["detail"] = detail
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, default=str) + "\n")
    except Exception:
        pass


def _clear_transcript(payload: dict) -> tuple[bool, str]:
    transcript_raw = str(payload.get("transcript_path") or "").strip()
    if not transcript_raw:
        return False, "missing transcript_path"

    transcript_path = Path(transcript_raw).expanduser()
    if not transcript_path.exists() or not transcript_path.is_file():
        return False, "transcript_path is not a file"

    try:
        transcript_path.write_text("", encoding="utf-8")
    except Exception as exc:
        return False, str(exc)[:200]

    _log(
        "claude_context_cleared",
        {
            "transcript_path": str(transcript_path),
        },
    )
    return True, str(transcript_path)


def main() -> int:
    payload_raw = sys.stdin.read()
    if not _enabled() or not payload_raw.strip():
        return 0

    try:
        payload = json.loads(payload_raw)
    except json.JSONDecodeError:
        _log("clear_context_invalid_payload")
        return 0

    if payload.get("stop_hook_active"):
        _log("clear_context_skipped_stop_hook_active")
        return 0

    ok, detail = _clear_transcript(payload)
    if not ok:
        _log("clear_context_failed", {"reason": detail})
        return 0

    output = {
        "systemMessage": "Cognee demo: Claude transcript context was emptied.",
        "suppressOutput": True,
    }
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
