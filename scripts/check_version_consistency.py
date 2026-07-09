#!/usr/bin/env python3
"""Check that every integration's version agrees across all of its manifests.

Policy: For each integration that ships a local manifest in this monorepo, the
version declared in `integrations/inventory.yml` (`current_version`) must match
the version in that integration's manifest(s):

  - Claude Code plugin:  .claude-plugin/plugin.json  AND the matching entry in
                         the top-level .claude-plugin/marketplace.json
  - Codex plugin:        .codex-plugin/plugin.json
  - npm packages:        package.json

This catches drift like inventory saying 0.1.0 while plugin.json says 0.2.0.

Integrations without a local manifest (pending migrations, pypi-only packages
published from another repo) are skipped — there is nothing in-tree to compare.

Usage:
    python scripts/check_version_consistency.py
"""

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_DIR = ROOT / "integrations"
INVENTORY = INTEGRATIONS_DIR / "inventory.yml"
MARKETPLACE = ROOT / ".claude-plugin" / "marketplace.json"

# slug -> list of manifest files whose `version` field must match inventory.
# Paths are relative to the repo root. Only integrations with an in-tree,
# versioned manifest are listed; add new ones here when they migrate in.
MANIFESTS = {
    "claude-code": ["integrations/claude-code/.claude-plugin/plugin.json"],
    "codex": ["integrations/codex/plugins/cognee/.codex-plugin/plugin.json"],
    "openclaw": ["integrations/openclaw/package.json"],
    "n8n": ["integrations/n8n/package.json"],
}


def parse_inventory_versions(text: str) -> dict[str, set[str]]:
    """Return {slug: {current_version, ...}} from inventory.yml.

    Minimal line scanner (no PyYAML dep, matching check_version_pins.py's
    stdlib-only style). The file is a flat list of `- slug:` blocks each with a
    `current_version:` line; a slug may appear more than once and every listed
    version for it must agree.
    """
    versions: dict[str, set[str]] = {}
    slug = None
    for line in text.splitlines():
        m = re.match(r"\s*-?\s*slug:\s*(\S+)", line)
        if m:
            slug = m.group(1).strip().strip('"')
            continue
        m = re.match(r'\s*current_version:\s*"?([^"\n]+?)"?\s*$', line)
        if m and slug:
            versions.setdefault(slug, set()).add(m.group(1).strip())
    return versions


def manifest_version(path: Path) -> str:
    return json.loads(path.read_text())["version"]


def marketplace_versions() -> dict[str, set[str]]:
    """Map slug -> {version} for each plugin in marketplace.json.

    Slug is the last path segment of the plugin's `source` (e.g.
    "./integrations/claude-code" -> "claude-code").
    """
    out: dict[str, set[str]] = {}
    if not MARKETPLACE.exists():
        return out
    data = json.loads(MARKETPLACE.read_text())
    for plugin in data.get("plugins", []):
        source = plugin.get("source")
        version = plugin.get("version")
        if not isinstance(source, str) or version is None:
            continue
        slug = source.rstrip("/").split("/")[-1]
        out.setdefault(slug, set()).add(str(version))
    return out


def main() -> None:
    if not INVENTORY.exists():
        print(f"Inventory not found: {INVENTORY}")
        sys.exit(1)

    inventory = parse_inventory_versions(INVENTORY.read_text())
    marketplace = marketplace_versions()

    errors: list[str] = []
    checked = 0

    for slug, manifest_paths in sorted(MANIFESTS.items()):
        # Collect every version this integration declares, tagged by source.
        found: dict[str, str] = {}

        inv = inventory.get(slug)
        if not inv:
            errors.append(f"{slug}: no current_version in inventory.yml")
            continue
        if len(inv) > 1:
            errors.append(f"{slug}: inventory.yml lists conflicting versions {sorted(inv)}")
        found["inventory.yml"] = sorted(inv)[0]

        for rel in manifest_paths:
            path = ROOT / rel
            if not path.exists():
                errors.append(f"{slug}: manifest missing: {rel}")
                continue
            found[rel] = manifest_version(path)

        for version in sorted(marketplace.get(slug, set())):
            found[".claude-plugin/marketplace.json"] = version

        checked += 1
        unique = set(found.values())
        if len(unique) > 1:
            detail = ", ".join(f"{src}={ver}" for src, ver in sorted(found.items()))
            errors.append(f"{slug}: version mismatch -> {detail}")

    print(f"Checked {checked} integration(s) with in-tree manifests.")

    if errors:
        print(f"\n{len(errors)} version-consistency violation(s) found:\n")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)

    print("All integration manifests agree with inventory.yml.")
    sys.exit(0)


if __name__ == "__main__":
    main()
