#!/usr/bin/env python3
"""Fail CI when inventory.yml drifts from integration plugin manifests."""

from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
INVENTORY = ROOT / "integrations" / "inventory.yml"


def _plugin_version_for_slug(slug: str) -> str | None:
    if slug == "claude-code":
        path = ROOT / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
    elif slug == "codex":
        path = ROOT / "integrations" / "codex" / "plugins" / "cognee" / ".codex-plugin" / "plugin.json"
    else:
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    return str(data.get("version") or "").strip()


def main() -> int:
    text = INVENTORY.read_text(encoding="utf-8")
    mismatches: list[str] = []

    current_slug = ""
    for line in text.splitlines():
        match = re.match(r"^\s*-\s*slug:\s*(.+)\s*$", line)
        if match:
            current_slug = match.group(1).strip().strip('"')
            continue

        match = re.match(r'^\s*current_version:\s*"?(.*?)"?\s*$', line)
        if not match or not current_slug:
            continue

        expected = match.group(1).strip()
        actual = _plugin_version_for_slug(current_slug)
        if actual is None:
            continue
        if expected != actual:
            mismatches.append(f"{current_slug}: inventory={expected} manifest={actual}")

    if mismatches:
        for line in mismatches:
            print(line)
        return 1

    print("inventory versions match plugin manifests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
