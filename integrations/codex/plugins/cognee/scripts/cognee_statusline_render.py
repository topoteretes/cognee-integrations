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


def _active_url() -> str:
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
    return url or "http://localhost:8011"


def _active_key() -> str:
    # 1. env var
    key = os.environ.get("COGNEE_API_KEY", "").strip()
    if key:
        return key
    # 2. api_key.json cache
    try:
        api_key_cache_path = _SHARED_ROOT / "api_key.json"
        if api_key_cache_path.exists():
            data = json.loads(api_key_cache_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                key = str(data.get("api_key") or "").strip()
                if key:
                    return key
    except Exception:
        pass
    # 3. config file
    try:
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            key = str(data.get("api_key") or "").strip()
            if key:
                return key
    except Exception:
        pass
    return ""


def _active_version() -> str:
    # 1. server-ready.json
    try:
        if _SERVER_READY_PATH.exists():
            data = json.loads(_SERVER_READY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                v = str(data.get("version") or "").strip()
                if v:
                    return v
    except Exception:
        pass
    # 2. venv-ready.json
    try:
        venv_ready_path = _SHARED_ROOT / "venv-ready.json"
        if venv_ready_path.exists():
            data = json.loads(venv_ready_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                v = str(data.get("cognee_version") or "").strip()
                if v:
                    return v
    except Exception:
        pass
    return ""


def render_status_for_host(host_id: str) -> str:
    """Return the status string (host_id is unused; kept for call-site compat)."""
    return f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}"


def main() -> None:
    if "--compact" in sys.argv or "-c" in sys.argv:
        sys.stdout.write(f"mode={_active_mode()} url={_active_url()} key={_active_key()} version={_active_version()}\n")
        sys.exit(0)
    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}")


if __name__ == "__main__":
    main()
