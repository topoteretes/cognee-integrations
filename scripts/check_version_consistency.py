#!/usr/bin/env python3
"""Check that plugin.json versions match their marketplace.json declarations.

For each marketplace.json that declares versioned plugins with a string `source`
path, this script locates the corresponding plugin.json and asserts that their
version fields agree.

Marketplace entries that lack a `version` field or use an object `source` (e.g.
Codex-style manifests) are silently skipped — they follow a different schema and
are not subject to this check.

Optionally (--check-inventory), cross-references integrations/inventory.yml
against the resolved manifest versions.

Usage:
    python scripts/check_version_consistency.py [--check-inventory]

Exit codes:
    0  All checked versions are consistent.
    1  At least one mismatch or error was found.
"""

import json
import os
import re
import glob
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def find_marketplace_files():
    """Find all marketplace.json files in the repository."""
    patterns = [
        str(REPO_ROOT / "**" / "marketplace.json"),
        str(REPO_ROOT / ".*" / "**" / "marketplace.json"),
    ]
    paths = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern, recursive=True))
    return sorted(paths)


def find_plugin_json(source_dir):
    """Find plugin.json files inside a source directory (including hidden dirs)."""
    patterns = [
        os.path.join(source_dir, "**", "plugin.json"),
        os.path.join(source_dir, ".*", "plugin.json"),
        os.path.join(source_dir, ".*", "**", "plugin.json"),
    ]
    paths = set()
    for pattern in patterns:
        paths.update(glob.glob(pattern, recursive=True))
    return sorted(paths)


def check_marketplace_vs_plugin():
    """Check plugin.json.version == marketplace.json plugins[].version."""
    failed = False
    matched = 0

    marketplace_files = find_marketplace_files()
    if not marketplace_files:
        print("::error::No marketplace.json files found in repository.")
        return True  # treat as failure

    for m_path in marketplace_files:
        try:
            with open(m_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            print(f"::error file={m_path}::Failed to read/parse marketplace.json: {e}")
            failed = True
            continue

        plugins = data.get("plugins", [])
        for plugin in plugins:
            source = plugin.get("source")
            m_version = plugin.get("version")

            # Skip plugins that don't declare a version or use an object source
            # (e.g. Codex-style manifests with {"source": "local", "path": "..."})
            if not isinstance(source, str) or not isinstance(m_version, str):
                continue

            # The source path in marketplace.json is relative to the repo root
            source_dir = os.path.normpath(str(REPO_ROOT / source))

            p_paths = find_plugin_json(source_dir)
            if not p_paths:
                print(
                    f"::error file={m_path}::No plugin.json found for plugin "
                    f"'{plugin.get('name', '?')}' in {source_dir}"
                )
                failed = True
                continue

            for p_path in p_paths:
                try:
                    with open(p_path, "r") as f:
                        p_data = json.load(f)
                except (json.JSONDecodeError, OSError) as e:
                    print(f"::error file={p_path}::Failed to read/parse plugin.json: {e}")
                    failed = True
                    continue

                p_version = p_data.get("version")
                if not p_version:
                    print(
                        f"::error file={p_path}::plugin.json is missing a 'version' field"
                    )
                    failed = True
                    continue

                if p_version != m_version:
                    print(
                        f"::error file={p_path}::Version mismatch! "
                        f"{os.path.basename(m_path)} says {m_version} "
                        f"but {os.path.basename(p_path)} says {p_version}"
                    )
                    failed = True
                else:
                    rel_m = os.path.relpath(m_path, REPO_ROOT)
                    rel_p = os.path.relpath(p_path, REPO_ROOT)
                    print(f"  ✓ {rel_p} ({p_version}) == {rel_m} ({m_version})")
                    matched += 1

    print(f"\nChecked {matched} plugin version(s) against marketplace manifests.")
    return failed


def parse_inventory_versions(inventory_path):
    """Parse inventory.yml using simple line-by-line parsing (no PyYAML needed).

    Returns a dict of {slug: current_version} for all entries that have both.
    """
    versions = {}
    current_slug = None

    with open(inventory_path, "r") as f:
        for line in f:
            slug_match = re.match(r"\s+-\s+slug:\s+(\S+)", line)
            if slug_match:
                current_slug = slug_match.group(1)
                continue

            version_match = re.match(r'\s+current_version:\s+"?([^"\s]+)"?', line)
            if version_match and current_slug:
                versions[current_slug] = version_match.group(1)
                current_slug = None

    return versions


def check_inventory(plugin_versions):
    """Cross-reference inventory.yml against resolved plugin.json versions.

    Args:
        plugin_versions: dict of {source_dir_basename: version} from the
                         marketplace/plugin check.
    """
    inventory_path = REPO_ROOT / "integrations" / "inventory.yml"
    if not inventory_path.exists():
        print("::warning::integrations/inventory.yml not found, skipping inventory check.")
        return False

    try:
        inv_versions = parse_inventory_versions(inventory_path)
    except OSError as e:
        print(f"::error file=integrations/inventory.yml::Failed to read inventory.yml: {e}")
        return True

    failed = False
    checked = 0
    for slug, inv_ver in inv_versions.items():
        if slug in plugin_versions:
            p_ver = plugin_versions[slug]
            if inv_ver != p_ver:
                print(
                    f"::error file=integrations/inventory.yml::"
                    f"inventory.yml says {slug} is {inv_ver} "
                    f"but plugin manifest says {p_ver}"
                )
                failed = True
            else:
                print(f"  ✓ inventory.yml {slug} ({inv_ver}) matches manifest")
                checked += 1

    print(f"\nCross-checked {checked} inventory entry/entries against manifests.")
    return failed


def collect_plugin_versions():
    """Build a map of {integration_slug: version} from marketplace/plugin checks."""
    versions = {}
    marketplace_files = find_marketplace_files()
    for m_path in marketplace_files:
        try:
            with open(m_path, "r") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue

        for plugin in data.get("plugins", []):
            source = plugin.get("source")
            m_version = plugin.get("version")
            if not isinstance(source, str) or not isinstance(m_version, str):
                continue
            # The source path is relative to the repo root
            source_dir = os.path.normpath(str(REPO_ROOT / source))
            slug = os.path.basename(source_dir)
            versions[slug] = m_version

    return versions


def main():
    print("=== Plugin version consistency check ===\n")
    failed = check_marketplace_vs_plugin()

    if "--check-inventory" in sys.argv:
        print("\n=== Inventory cross-reference check ===\n")
        plugin_versions = collect_plugin_versions()
        inv_failed = check_inventory(plugin_versions)
        failed = failed or inv_failed

    if failed:
        print("\n✗ Version consistency check FAILED.")
        sys.exit(1)
    else:
        print("\n✓ All versions are consistent.")
        sys.exit(0)


if __name__ == "__main__":
    main()
