#!/usr/bin/env python3
"""Opt-in session-end telemetry for the Cognee Claude Code plugin.

Emits one event per session containing only aggregate counts and
runtime metadata — no prompt text, no recall content, no URLs, no keys.

Opt-in: COGNEE_TELEMETRY_ENABLED=true
Default sink: ~/.cognee-plugin/claude-code/telemetry.jsonl
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

_PLUGIN = Path.home() / ".cognee-plugin" / "claude-code"
_TELEMETRY_FILE = _PLUGIN / "telemetry.jsonl"
_COUNTER_FILE = _PLUGIN / "counter.json"
_SAVE_COUNTER = _PLUGIN / "save_counter.json"
_SAVE_KINDS = ("prompt", "trace", "answer")


@runtime_checkable
class TelemetrySink(Protocol):
    def emit(self, event: dict) -> None: ...


class LocalFileSink:
    """Appends one JSON line per event to the telemetry file."""

    def __init__(self, path: Path = _TELEMETRY_FILE) -> None:
        self._path = path

    def emit(self, event: dict) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with self._path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(event, default=str) + "\n")
        except Exception:
            pass


def _pkg_version(name: str) -> str:
    try:
        import importlib.metadata

        return importlib.metadata.version(name)
    except Exception:
        return "?"


def _turn_count(session_id: str) -> int:
    try:
        data = (
            json.loads(_COUNTER_FILE.read_text(encoding="utf-8"))
            if _COUNTER_FILE.exists()
            else {}
        )
        return int(data.get(session_id) or 0)
    except Exception:
        return 0


def _save_counts(session_id: str) -> dict:
    zero = {k: 0 for k in _SAVE_KINDS}
    try:
        data = (
            json.loads(_SAVE_COUNTER.read_text(encoding="utf-8"))
            if _SAVE_COUNTER.exists()
            else {}
        )
        sess = data.get(session_id) or zero
        return {k: int(sess.get(k, 0)) for k in _SAVE_KINDS}
    except Exception:
        return zero


def is_enabled() -> bool:
    return os.environ.get("COGNEE_TELEMETRY_ENABLED", "").lower() in ("1", "true", "yes")


def emit_session_end(
    session_id: str,
    mode: str = "unknown",
    *,
    sink: "TelemetrySink | None" = None,
) -> None:
    """Emit a session-end event if telemetry is opted in.

    Only aggregate counts and metadata are emitted — never prompt text,
    recall content, URLs, or API keys.
    """
    if not is_enabled():
        return
    if not session_id:
        return

    event = {
        "event": "session_end",
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "session_id": session_id,
        "mode": mode,
        "turns": _turn_count(session_id),
        "saves": _save_counts(session_id),
        "versions": {
            "cognee": _pkg_version("cognee"),
            "plugin": _pkg_version("cognee-plugin"),
        },
    }

    (sink or LocalFileSink()).emit(event)
