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
_UPDATE_CHECK_PATH = _SHARED_ROOT / "codex" / "update-check.json"
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


def _update_segment() -> str:
    """Plain-text 'update available' segment, or '' — read purely from the marker.

    Codex surfaces status inside the model's context (not a terminal bar), so this
    stays plain text (no ANSI). The idle watcher's background check writes the
    marker; this remains network-free and free of any ``_plugin_common`` import.
    """
    if os.environ.get("COGNEE_UPDATE_CHECK", "").strip().lower() in ("0", "false", "no", "off"):
        return ""
    try:
        marker = json.loads(_UPDATE_CHECK_PATH.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not (isinstance(marker, dict) and marker.get("update_available")):
        return ""
    installed = str(marker.get("installed_version") or "")
    latest = str(marker.get("latest_version") or "")
    if not (installed and latest):
        return ""
    return f"  ⬆ Cognee update available {installed}→{latest}"


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
    return (
        f"{_health_prefix()}cognee: {_active_dataset()} · "
        f"{_active_mode()}{_update_segment()}{_plugin_version()}"
    )


def main() -> None:
    # Windows defaults stdio to the locale code page (e.g. cp1252), which cannot
    # encode the status glyphs (●, ✕, ⬆); writing one raises UnicodeEncodeError
    # and exits non-zero. Force UTF-8 on both streams so this renderer stays
    # crash-free when invoked directly via cognee-statusline.sh. Kept inside
    # main() (not at module scope) because session-start.py and
    # session-context-lookup.py import render_status_for_host — a module-level
    # reconfigure would hijack the importer's stdout. Best-effort: a stream that
    # can't be reconfigured (e.g. a captured stdout under test) is left as-is.
    for _stream in (sys.stdin, sys.stdout):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(
        f"{_health_prefix()}cognee: {_active_dataset()} · "
        f"{_active_mode()}{_update_segment()}{_plugin_version()}"
    )


if __name__ == "__main__":
    main()
