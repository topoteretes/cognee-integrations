#!/usr/bin/env python3
"""Coding memory capture at the end of each request (Stop hook, SDK-1336).

Exits at once unless ``COGNEE_CODING_MEMORY`` is on. Then it reads the
transcript and queues every tool call not sent yet plus one QA entry with the
request and the final message (see ``_coding_memory.capture``).

Claude Code runs this hook asynchronously, so the work happens inline. Codex
runs every hook synchronously, so there the hook only hands the payload to a
detached copy of itself (``--run <file>``) and returns.

``--warm <cwd>`` fills the lesson cache in the background (spawned by
SessionStart), so the first prompt's memory block needs no downloads.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
import _coding_memory
from _plugin_common import (
    _PLUGIN_DIR,
    get_session_key,
    hook_log,
    quiet_hook_output,
    resolve_session_key_from_payload,
    set_session_key,
)

try:  # Claude Code's observer children must not capture; Codex has no observer.
    from _plugin_common import is_observer_child
except ImportError:  # pragma: no cover - Codex

    def is_observer_child() -> bool:
        return False


_RUN_ARG = "--run"
_WARM_ARG = "--warm"


def _spawn(*args: str) -> bool:
    try:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=os.environ.copy(),
            start_new_session=True,
            close_fds=True,
        )
        return True
    except Exception as exc:
        hook_log("coding_spawn_failed", {"args": list(args)[:1], "error": str(exc)[:200]})
        return False


def spawn_warm(cwd: str) -> bool:
    """Start the background lesson-cache fill (called by SessionStart)."""
    return _spawn(_WARM_ARG, cwd)


def _capture(payload: dict) -> None:
    candidate, _source = resolve_session_key_from_payload(payload)
    if candidate:
        set_session_key(candidate)
    if not get_session_key():
        hook_log("coding_capture_no_session_key")
        return
    with quiet_hook_output("send-unit"):
        _coding_memory.capture(payload)


def _run_detached(path: str) -> None:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        hook_log("coding_payload_unreadable", {"error": str(exc)[:200]})
        return
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    if isinstance(payload, dict):
        _capture(payload)


def _handoff(payload: dict) -> None:
    """Codex: park the payload in a private file and let a detached child send it."""
    spool = Path(_PLUGIN_DIR) / "coding" / "handoff"
    spool.mkdir(parents=True, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="stop-", suffix=".json", dir=str(spool))
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    if not _spawn(_RUN_ARG, path):
        _run_detached(path)


def main() -> None:
    if is_observer_child() or not _coding_memory.enabled():
        return
    try:
        if _RUN_ARG in sys.argv:
            index = sys.argv.index(_RUN_ARG)
            if len(sys.argv) > index + 1:
                _run_detached(sys.argv[index + 1])
            return
        if _WARM_ARG in sys.argv:
            index = sys.argv.index(_WARM_ARG)
            cwd = sys.argv[index + 1] if len(sys.argv) > index + 1 else os.getcwd()
            with quiet_hook_output("send-unit"):
                cached = _coding_memory.warm_lessons(cwd)
            hook_log("coding_lessons_warmed", {"lessons": cached})
            return
        raw = sys.stdin.read()
        if not raw.strip():
            return
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            return
        if _coding_memory.agent() == "codex":
            _handoff(payload)
        else:
            _capture(payload)
    except Exception as exc:
        hook_log("coding_capture_failed", {"error": str(exc)[:200]})


if __name__ == "__main__":
    main()
