#!/usr/bin/env python3
"""Render the Cognee status line.

Invoked by Claude Code's ``statusLine`` (via ``cognee-statusline.sh``), which
pipes a JSON context on stdin. Deliberately standalone and pure-local: reads
only env vars and ``~/.cognee-plugin/config.json`` - no network calls, no
``_plugin_common`` import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_SHARED_ROOT = Path.home() / ".cognee-plugin"
_CONFIG_PATH = _SHARED_ROOT / "claude-code" / "config.json"
_SERVER_READY_PATH = _SHARED_ROOT / "server-ready.json"
_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"
_PLUGIN_MANIFEST_PATH = (
    Path(os.environ.get("CLAUDE_PLUGIN_ROOT", "")) / ".claude-plugin" / "plugin.json"
)
_DEFAULT_DATASET = "agent_sessions"


def _active_dataset() -> str:
    # 1. env var (inherited from the shell that launched Claude Code)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 2. config file
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            v = str(data.get("dataset") or "").strip()
            if v:
                return v
    except Exception:
        pass
    # 3. default
    return _DEFAULT_DATASET


_LOOPBACK = {"localhost", "127.0.0.1", "::1", ""}


def _active_mode() -> str:
    # 1. env var
    url = os.environ.get("COGNEE_BASE_URL", "").strip()
    # 2. config file
    if not url:
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                url = str(data.get("base_url") or "").strip()
        except Exception:
            pass
    if not url:
        return "local"
    return "local" if (urlparse(url).hostname or "") in _LOOPBACK else "cloud"


def _health_prefix() -> str:
    try:
        raw = json.loads(_BREAKER_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            if float(raw.get("cooldown_until", 0) or 0) > time.time():
                return "✕ "
    except Exception:
        pass
    if _SERVER_READY_PATH.exists():
        return "● "
    return ""


def _installed_version() -> str:
    try:
        raw = json.loads(_PLUGIN_MANIFEST_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return str(raw.get("version") or "").strip()
    except Exception:
        pass
    return ""


def _version_suffix() -> str:
    version = _installed_version()
    return f" · v{version}" if version else ""


def main() -> None:
    try:
        json.load(sys.stdin)  # consume stdin as required by Claude Code
    except Exception:
        pass
    sys.stdout.write(
        f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}{_version_suffix()}"
    )


if __name__ == "__main__":
    main()
