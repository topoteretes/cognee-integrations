"""Claude Code's native auto-memory alongside Cognee: read, advise, never write.

Earlier versions turned ``autoMemoryEnabled`` off in ``~/.claude/settings.json``
in Cloud mode. This plugin no longer writes Claude Code's settings (directory
policy, SDK-1411): in Cloud mode it says once that both memories are on and how
to turn Claude's off, and it says once when an earlier version had turned it
off, so the user can turn it back on.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from _injection_ledger import atomic_json


def _paths():
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    root = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return (
        root / "settings.json",
        Path.home() / ".cognee-plugin" / "claude-code" / "auto-memory.json",
    )


def _read(path):
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError("settings must be a JSON object")
    return data


def _claude_memory_on(settings: dict) -> bool:
    if os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", "").strip():
        return False
    return settings.get("autoMemoryEnabled") is not False


def advice(cloud: bool) -> str:
    """A one-time notice about Claude Code's auto-memory, or "". Never raises.

    Reads the settings, writes only the plugin's own marker.
    """
    try:
        settings_path, marker_path = _paths()
        settings, marker = _read(settings_path), _read(marker_path)
        if marker.get("owned") and not marker.get("handback_reported"):
            # An earlier version set autoMemoryEnabled=false; it is no longer ours to undo.
            atomic_json(marker_path, {**marker, "owned": False, "handback_reported": True})
            if settings.get("autoMemoryEnabled") is False:
                return (
                    "An earlier Cognee version turned off Claude auto-memory "
                    "(autoMemoryEnabled=false in ~/.claude/settings.json). Cognee no longer "
                    "changes Claude settings; remove that line to turn it back on."
                )
            return ""
        if cloud and _claude_memory_on(settings) and not marker.get("advice_reported"):
            atomic_json(marker_path, {**marker, "advice_reported": True})
            return (
                "Claude auto-memory is on alongside Cognee. To keep memory in Cognee only, "
                "set autoMemoryEnabled to false in ~/.claude/settings.json."
            )
    except Exception:
        pass
    return ""


def state() -> dict:
    settings_path, marker_path = _paths()
    try:
        settings, marker = _read(settings_path), _read(marker_path)
        return {
            "enabled": settings.get("autoMemoryEnabled", "default"),
            "plugin_owned": False,
            "earlier_version_disabled_it": bool(marker.get("owned")),
            "environment_override": os.environ.get("CLAUDE_CODE_DISABLE_AUTO_MEMORY", ""),
        }
    except (OSError, ValueError) as exc:
        return {"error": str(exc)}
