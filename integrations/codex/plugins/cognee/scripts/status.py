#!/usr/bin/env python3
"""
cognee-plugin status
Prints resolved runtime state on one line and exits 0.
Output: mode=... url=... key=... version=...
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from _plugin_common import _api_key, _local_api_url


def main() -> None:
    # --- version: read from .cognee-plugin/plugin.json ---
    plugin_json = Path(__file__).parent.parent / ".cognee-plugin" / "plugin.json"
    try:
        version = json.loads(plugin_json.read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        version = "unknown"

    # --- url + mode ---
    url = _local_api_url().rstrip("/")
    is_local = not url or "localhost" in url or "127.0.0.1" in url
    mode = "local" if is_local else "cloud"

    # --- key ---
    api_key = _api_key()
    key = "configured" if api_key else "missing"

    print(f"mode={mode} url={url} key={key} version={version}")
    sys.exit(0)


if __name__ == "__main__":
    main()