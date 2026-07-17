#!/usr/bin/env python3
"""Render the Cognee status line (Codex).

Invoked via ``cognee-statusline.sh``, which pipes a JSON context on stdin.
Deliberately standalone and pure-local: reads only env vars and
``~/.cognee-plugin/config.json`` — no network calls, no ``_plugin_common``
import.

Output: ``cognee: <dataset-name> · local`` or ``cognee: <dataset-name> · cloud``
"""

import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

_SHARED_ROOT = Path.home() / ".cognee-plugin"
_CONFIG_PATH = _SHARED_ROOT / "config.json"
_SERVER_READY_PATH = _SHARED_ROOT / "server-ready.json"
_BREAKER_PATH = _SHARED_ROOT / "recall-breaker.json"
_DEFAULT_DATASET = "agent_sessions"


def _active_dataset() -> str:
    # 1. env var (inherited from the shell that launched Codex)
    v = os.environ.get("COGNEE_PLUGIN_DATASET", "").strip()
    if v:
        return v
    # 2. default
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


def _plugin_version() -> str:
    current_version = ""
    try:
        plugin_file = Path.home() / ".codex-plugin" / "plugin.json"
        if plugin_file.exists():
            data = json.loads(plugin_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                v = str(data.get("version", "")).strip()
                if v:
                    current_version = v
    except Exception:
        pass

    if not current_version:
        return ""

    badge = ""
    try:
        update_file = Path.home() / ".codex-plugin" / "update-check.json"
        if update_file.exists():
            data = json.loads(update_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                latest = str(data.get("latest_version", "")).strip()
                if latest and latest != current_version:
                    badge = f" ↑ v{latest}"
    except Exception:
        pass

    return f" v{current_version}{badge}"


def render_status_for_host(host_id: str) -> str:
    """Return the status string (host_id is unused; kept for call-site compat)."""
    return f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}{_plugin_version()}"


def main() -> None:
    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}{_plugin_version()}")


if __name__ == "__main__":
    main()
