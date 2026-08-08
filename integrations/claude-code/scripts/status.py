#!/usr/bin/env python3
"""
cognee-plugin status
Prints resolved runtime state on one line and exits 0.
Output: mode=... url=... key=... version=...
"""
import json
import sys
from pathlib import Path

# Add scripts dir to path so we can import _plugin_common
sys.path.insert(0, str(Path(__file__).parent))

from _plugin_common import (
    _api_key_with_source,
    _local_api_url_with_source,
)


def main() -> None:
    # --- version: read from .claude-plugin/plugin.json ---
    plugin_json = Path(__file__).parent.parent / ".claude-plugin" / "plugin.json"
    try:
        version = json.loads(plugin_json.read_text(encoding="utf-8")).get("version", "unknown")
    except Exception:
        version = "unknown"

    # --- url + mode: reuse existing resolver from _plugin_common ---
    url, _ = _local_api_url_with_source()
    url = url.rstrip("/")

    is_local = (
        not url
        or "localhost" in url
        or "127.0.0.1" in url
    )
    mode = "local" if is_local else "cloud"

    # --- key: reuse existing resolver from _plugin_common ---
    api_key, _ = _api_key_with_source(url)
    key = "configured" if api_key else "missing"

    print(f"mode={mode} url={url} key={key} version={version}")
    sys.exit(0)


if __name__ == "__main__":
    main()