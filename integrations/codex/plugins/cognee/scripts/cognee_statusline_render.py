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


def render_status_for_host(host_id: str) -> str:
    """Return the status string (host_id is unused; kept for call-site compat)."""
    return f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}"


def _resolve_config() -> tuple[str, str, str, str]:
    # 1. Resolve mode and url
    embedded = os.environ.get("COGNEE_EMBEDDED", "").lower() in ("true", "1", "yes")

    url = os.environ.get("COGNEE_BASE_URL", "").strip()
    if not url:
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                url = str(data.get("base_url") or "").strip()
        except Exception:
            pass

    if embedded:
        mode = "embedded"
        resolved_url = "none"
    elif url:
        mode = "remote"
        resolved_url = url.rstrip("/")
    else:
        mode = "local-server"
        local_url = os.environ.get("COGNEE_LOCAL_API_URL", "").strip()
        if not local_url:
            local_url = "http://localhost:8011"
        resolved_url = local_url.rstrip("/")

    # 2. Resolve key
    key = os.environ.get("COGNEE_API_KEY", "").strip()
    if not key:
        cache_path = _SHARED_ROOT / "api_key.json"
        if cache_path.exists():
            try:
                cache = json.loads(cache_path.read_text(encoding="utf-8"))
                if isinstance(cache, dict):
                    cached_key = str(cache.get("api_key") or "").strip()
                    cached_url = str(cache.get("base_url") or "").strip().rstrip("/")
                    if cached_key and (not cached_url or cached_url == resolved_url):
                        key = cached_key
            except Exception:
                pass

    # 3. Resolve version
    version = "0.0.0"
    plugin_root = Path(__file__).resolve().parent.parent
    for sub in (".claude-plugin", ".codex-plugin"):
        p = plugin_root / sub / "plugin.json"
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    v = data.get("version")
                    if v:
                        version = str(v)
                        break
            except Exception:
                pass

    return mode, resolved_url, key, version


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("status", "--status"):
        mode, url, key, version = _resolve_config()
        sys.stdout.write(f"mode={mode} url={url} key={key} version={version}\n")
        sys.exit(0)

    try:
        json.load(sys.stdin)  # consume stdin as required by the host
    except Exception:
        pass
    sys.stdout.write(f"{_health_prefix()}cognee: {_active_dataset()} · {_active_mode()}")


if __name__ == "__main__":
    main()

