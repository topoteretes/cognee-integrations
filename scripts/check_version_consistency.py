#!/usr/bin/env python3
"""Check inventory and release versions for the six in-repository plugin release surfaces.

Run ``python scripts/check_version_consistency.py`` from any directory.
The inventory scanner supports its existing flat ``slug``/``current_version``
schema, including quoted values and comments; it is not a general YAML parser.
This guard covers plugin release manifests, including OpenCode and Antigravity;
it does not compare unrelated Python packages or externally maintained integrations.
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFESTS = {
    "claude-code": "integrations/claude-code/.claude-plugin/plugin.json",
    "codex": "integrations/codex/plugins/cognee/.codex-plugin/plugin.json",
    "openclaw": "integrations/openclaw/package.json",
    "n8n": "integrations/n8n/package.json",
    "opencode": "integrations/opencode/package.json",
    "antigravity": "integrations/antigravity/plugin.json",
}
MARKETPLACE = ".claude-plugin/marketplace.json"


def parse_inventory_versions(text: str) -> dict[str, set[str]]:
    versions: dict[str, set[str]] = {}
    slug = None
    scalar = r"""(?:"([^"\n]+)"|'([^'\n]+)'|([^\s#]+))\s*(?:#.*)?$"""
    for line in text.splitlines():
        match = re.match(r"\s*-\s+slug:\s*" + scalar, line)
        if match:
            slug = next(value for value in match.groups() if value is not None)
            continue
        match = re.match(r"\s+current_version:\s*" + scalar, line)
        if match and slug:
            version = next(value for value in match.groups() if value is not None)
            versions.setdefault(slug, set()).add(version)
    return versions


def check_versions(root: Path) -> list[str]:
    """Return actionable violations, including missing or malformed metadata."""
    errors = []
    try:
        inventory = parse_inventory_versions(
            (root / "integrations/inventory.yml").read_text(encoding="utf-8")
        )
    except OSError as exc:
        return [f"inventory.yml: {exc}"]

    marketplace_versions = []
    try:
        marketplace = json.loads((root / MARKETPLACE).read_text(encoding="utf-8"))
        plugins = marketplace["plugins"]
        if not isinstance(plugins, list):
            raise ValueError("plugins must be a list")
        for index, plugin in enumerate(plugins):
            source = str(plugin.get("source", "")).removeprefix("./").rstrip("/")
            if source == "integrations/claude-code":
                version = plugin.get("version")
                if not isinstance(version, str) or not version.strip():
                    errors.append(f"{MARKETPLACE}: plugins[{index}].version is missing or invalid")
                else:
                    marketplace_versions.append(version)
        if not marketplace_versions:
            errors.append(f"{MARKETPLACE}: no versioned Claude Code plugin entry")
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        errors.append(f"{MARKETPLACE}: {exc}")

    for slug, manifest in MANIFESTS.items():
        inventory_versions = inventory.get(slug, set())
        if not inventory_versions:
            errors.append(f"{slug}: no current_version in inventory.yml")
        if len(inventory_versions) > 1:
            errors.append(f"{slug}: conflicting inventory versions {sorted(inventory_versions)}")
        try:
            version = json.loads((root / manifest).read_text(encoding="utf-8"))["version"]
            if not isinstance(version, str) or not version.strip():
                raise ValueError("version must be a non-empty string")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            errors.append(f"{manifest}: {exc}")
            continue
        for inventory_version in sorted(inventory_versions):
            if inventory_version != version:
                errors.append(f"{slug}: inventory.yml={inventory_version}, {manifest}={version}")
        if slug == "claude-code":
            for marketplace_version in marketplace_versions:
                if marketplace_version != version:
                    errors.append(
                        f"{slug}: {MARKETPLACE}={marketplace_version}, {manifest}={version}"
                    )
    return errors


def main() -> int:
    errors = check_versions(ROOT)
    if errors:
        print("Version consistency failed:")
        for error in errors:
            print(f"  - {error}")
        return 1
    print(f"Inventory and release manifests agree for {len(MANIFESTS)} integrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
