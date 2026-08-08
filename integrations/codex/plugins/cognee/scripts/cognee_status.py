#!/usr/bin/env python3
"""Print a compact runtime status one-liner and exit 0.

Output format: mode=… url=… key=set|missing version=…
"""
import json
import os
import sys
from pathlib import Path

# Find and add scripts dir to path so _plugin_common imports work
_SCRIPTS_DIR = Path(__file__).parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from _plugin_common import resolve_runtime_mode  # noqa: E402


def _plugin_version() -> str:
    """Read version from the integration's plugin.json."""
    plugin_json = _SCRIPTS_DIR.parent / ".codex-plugin" / "plugin.json"
    try:
        data = json.loads(plugin_json.read_text(encoding="utf-8"))
        return str(data.get("version") or "unknown")
    except Exception:
        return "unknown"


def main() -> None:
    runtime = resolve_runtime_mode()
    mode = str(runtime.get("mode") or "unknown")
    url = str(runtime.get("base_url") or "")
    key = "set" if runtime.get("api_key_present") else "missing"
    version = _plugin_version()
    print(f"mode={mode} url={url} key={key} version={version}")


if __name__ == "__main__":
    main()
