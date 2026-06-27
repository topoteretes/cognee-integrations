#!/usr/bin/env python3
"""Background check for a newer published plugin version.

Spawned detached from SessionStart so it never blocks the session. It compares
the installed plugin version (``CLAUDE_PLUGIN_ROOT/.claude-plugin/plugin.json``)
against the latest published version in the marketplace manifest on GitHub, and
writes the result to ``~/.cognee-plugin/update-check.json`` for the (pure-local,
no-network) status line to read.

Deliberately standalone + stdlib-only (runs under the system ``python3`` without
the plugin venv). TTL-gated and fail-silent: a recent check or a network error is
a no-op — never a false "update available", never a stack trace in a hook.

Env knobs:
  COGNEE_UPDATE_CHECK=false            disable the check entirely
  COGNEE_UPDATE_CHECK_INTERVAL_HOURS   re-check interval (default 24)
"""

import json
import os
import sys
import time
import urllib.request
from pathlib import Path

_STATE_PATH = Path.home() / ".cognee-plugin" / "update-check.json"
_MARKETPLACE_URL = (
    "https://raw.githubusercontent.com/topoteretes/cognee-integrations/"
    "main/.claude-plugin/marketplace.json"
)
_PLUGIN_NAME = "cognee-memory"
_DEFAULT_TTL_HOURS = 24.0


def _enabled() -> bool:
    return os.environ.get("COGNEE_UPDATE_CHECK", "").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def _ttl_hours() -> float:
    try:
        raw = os.environ.get("COGNEE_UPDATE_CHECK_INTERVAL_HOURS", "").strip()
        return float(raw) if raw else _DEFAULT_TTL_HOURS
    except (TypeError, ValueError):
        return _DEFAULT_TTL_HOURS


def _installed_version(plugin_root: str) -> str:
    try:
        manifest = Path(plugin_root) / ".claude-plugin" / "plugin.json"
        return str(json.loads(manifest.read_text(encoding="utf-8")).get("version") or "").strip()
    except Exception:
        return ""


def _latest_version(timeout: float) -> str:
    """Latest published version of THIS plugin from the marketplace manifest.

    Reads ``plugins[].version`` for the matching plugin name only — NOT the
    marketplace's own ``metadata.version`` (a different, unrelated number).
    """
    req = urllib.request.Request(_MARKETPLACE_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    for plugin in data.get("plugins", []) or []:
        if isinstance(plugin, dict) and plugin.get("name") == _PLUGIN_NAME:
            return str(plugin.get("version") or "").strip()
    return ""


def _parse(version: str) -> tuple:
    """Parse 'X.Y.Z' to a comparable tuple. Tolerates a leading 'v' and a
    pre-release suffix (e.g. '1.2.3-rc1' -> (1, 2, 3))."""
    parts = []
    for chunk in str(version).strip().lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break  # stop at the first non-digit (drops '-rc1', '+build', etc.)
        parts.append(int(digits) if digits else 0)
    return tuple(parts)


def is_newer(latest: str, installed: str) -> bool:
    """True only when both versions parse and latest > installed."""
    if not latest or not installed:
        return False
    return _parse(latest) > _parse(installed)


def _recently_checked() -> bool:
    try:
        prev = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        return (time.time() - float(prev.get("checked_at", 0) or 0)) < _ttl_hours() * 3600.0
    except Exception:
        return False


def _previous_latest() -> str:
    try:
        return str(json.loads(_STATE_PATH.read_text(encoding="utf-8")).get("latest") or "").strip()
    except Exception:
        return ""


def run(plugin_root: str = "", *, timeout: float = 4.0, force: bool = False) -> None:
    if not _enabled():
        return
    if not force and _recently_checked():
        return

    installed = _installed_version(plugin_root or os.environ.get("CLAUDE_PLUGIN_ROOT", ""))
    try:
        latest = _latest_version(timeout)
    except Exception:
        # Network/parse failure: keep the last known 'latest' so a transient
        # outage doesn't clear a real notification, and refresh the timestamp so
        # we don't hammer the network on every session.
        latest = _previous_latest()

    record = {
        "checked_at": time.time(),
        "installed": installed,
        "latest": latest,
        "update_available": is_newer(latest, installed),
    }
    try:
        _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _STATE_PATH.write_text(json.dumps(record), encoding="utf-8")
    except Exception:
        pass


if __name__ == "__main__":
    args = sys.argv[1:]
    root = next((a for a in args if not a.startswith("-")), "")
    run(root, force="--force" in args)
