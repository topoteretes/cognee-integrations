#!/usr/bin/env python3
"""Verify that integration versions agree across inventory.yml, plugin.json, and marketplace.json.

Exit code 0 when all checked manifests agree; 1 on any mismatch (CI fails).
Inventory.yml is line-based YAML (not loaded via pyyaml) to keep CI dependencies minimal.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY = REPO_ROOT / "integrations" / "inventory.yml"
MARKETPLACE = REPO_ROOT / ".claude-plugin" / "marketplace.json"

errors = []


def _slug_version(inventory_text: str, slug: str) -> str:
    """Extract current_version for a given slug from inventory.yml text."""
    in_section = False
    for line in inventory_text.splitlines():
        if re.match(r"^\s+-\s+slug:\s+" + re.escape(slug) + r"\s*$", line):
            in_section = True
            continue
        if in_section:
            m = re.match(r"^\s+current_version:\s+\"([^\"]+)\"\s*$", line)
            if m:
                return m.group(1)
            if re.match(r"^\s+-\s+slug:", line):
                break
    return ""


def check_claude_code(inventory_text: str):  # noqa: E501
    inv_version = _slug_version(inventory_text, "claude-code")
    if not inv_version:
        errors.append("claude-code: not found in inventory.yml")
        return

    plugin_path = REPO_ROOT / "integrations" / "claude-code" / ".claude-plugin" / "plugin.json"
    plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
    plugin_version = plugin.get("version", "")

    if inv_version != plugin_version:
        errors.append(
            f"claude-code version mismatch: inventory.yml says '{inv_version}' "
            f"but plugin.json says '{plugin_version}'"
        )

    marketplace = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    for p in marketplace.get("plugins", []):
        if p.get("name") == "cognee-memory":
            mkt_version = p.get("version", "")
            if inv_version != mkt_version:
                errors.append(
                    f"claude-code version mismatch: inventory.yml says '{inv_version}' "
                    f"but marketplace.json says '{mkt_version}'"
                )
            break
    else:
        errors.append("claude-code: cognee-memory not found in marketplace.json")


def check_codex(inventory_text: str):  # noqa: E501
    inv_version = _slug_version(inventory_text, "codex")
    if not inv_version:
        return

    plugin_path = REPO_ROOT / "integrations" / "codex" / "plugins" / "cognee" / ".codex-plugin" / "plugin.json"
    if plugin_path.exists():
        plugin = json.loads(plugin_path.read_text(encoding="utf-8"))
        plugin_version = plugin.get("version", "")
        if inv_version != plugin_version:
            errors.append(
                f"codex version mismatch: inventory.yml says '{inv_version}' "
                f"but plugin.json says '{plugin_version}'"
            )


def main():
    inventory_text = INVENTORY.read_text(encoding="utf-8")
    check_claude_code(inventory_text)
    check_codex(inventory_text)

    if errors:
        print("Version consistency check FAILED:", file=sys.stderr)
        for err in errors:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print("All integration versions are consistent.")
    sys.exit(0)


if __name__ == "__main__":
    main()
