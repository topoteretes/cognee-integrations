#!/usr/bin/env python3
"""Validate required integration structure and inventory coverage."""

from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
INTEGRATIONS_DIR = ROOT / "integrations"
INVENTORY_FILE = INTEGRATIONS_DIR / "inventory.yml"


def _dir_has_files(directory: Path) -> bool:
    """Return True if *directory* contains at least one file (non-recursive)."""
    return any(p.is_file() for p in directory.iterdir())


def parse_inventory(path: Path) -> list[dict[str, str]]:
    """Parse inventory entries using a lightweight line parser.

    Assumptions (keep in mind when editing inventory.yml):
      - Each entry starts with ``- slug: <value>`` at list-item indent.
      - Subsequent fields use exactly 4-space indentation.
      - Values are flat scalars (no multi-line, no nested maps/sequences).
    """
    entries: list[dict[str, str]] = []
    current: dict[str, str] | None = None

    for raw_line in path.read_text().splitlines():
        line = raw_line.rstrip()

        slug_match = re.match(r'^\s*-\s+slug:\s*"?([^"#]+?)"?\s*$', line)
        if slug_match:
            if current:
                entries.append(current)
            current = {"slug": slug_match.group(1).strip()}
            continue

        if current is None:
            continue

        field_match = re.match(r"^\s{4}([a-z_]+):\s*(.*)$", line)
        if not field_match:
            continue

        key = field_match.group(1).strip()
        value = field_match.group(2).strip()

        # Remove inline comments from unquoted values.
        if " #" in value:
            value = value.split(" #", 1)[0].strip()

        if (
            (value.startswith('"') and value.endswith('"'))
            or (value.startswith("'") and value.endswith("'"))
        ) and len(value) >= 2:
            value = value[1:-1]

        current[key] = value

    if current:
        entries.append(current)

    return entries


def list_integration_dirs(path: Path) -> list[Path]:
    return sorted(
        p
        for p in path.iterdir()
        if p.is_dir() and not p.name.startswith(".") and not p.name.startswith("_")
    )


def main() -> int:
    if not INTEGRATIONS_DIR.exists():
        print(f"missing integrations dir: {INTEGRATIONS_DIR}")
        return 1

    if not INVENTORY_FILE.exists():
        print(f"missing inventory file: {INVENTORY_FILE}")
        return 1

    dirs = list_integration_dirs(INTEGRATIONS_DIR)
    entries = parse_inventory(INVENTORY_FILE)
    by_slug = {entry.get("slug", ""): entry for entry in entries}

    errors: list[str] = []

    for integration_dir in dirs:
        name = integration_dir.name
        readme = integration_dir / "README.md"
        examples = integration_dir / "examples"
        tests = integration_dir / "tests"
        tests_ts = integration_dir / "__tests__"
        pyproject = integration_dir / "pyproject.toml"
        package_json = integration_dir / "package.json"

        if name not in by_slug:
            errors.append(f"{name}: missing from integrations/inventory.yml")

        if not readme.exists():
            errors.append(f"{name}: missing README.md")

        if not examples.exists():
            errors.append(f"{name}: missing examples/ directory")
        elif not _dir_has_files(examples):
            errors.append(f"{name}: examples/ directory is empty (need at least one file)")

        tests_dir = tests if tests.exists() else tests_ts if tests_ts.exists() else None
        if tests_dir is None:
            errors.append(f"{name}: missing tests/ or __tests__/ directory")
        elif not _dir_has_files(tests_dir):
            errors.append(f"{name}: {tests_dir.name}/ directory is empty (need at least one test file)")

        if not pyproject.exists() and not package_json.exists():
            errors.append(f"{name}: missing pyproject.toml or package.json")

    # Validate required inventory metadata for migrated integrations.
    for slug, entry in by_slug.items():
        status = entry.get("migration_status", "")
        if status != "done":
            continue

        for required_key in (
            "ownership",
            "package_name",
            "monorepo_path",
            "install_path_status",
        ):
            if not entry.get(required_key):
                errors.append(f"{slug}: done entry missing `{required_key}` in inventory")

        monorepo_path = entry.get("monorepo_path", "")
        if monorepo_path:
            target = ROOT / monorepo_path
            if not target.exists():
                errors.append(f"{slug}: monorepo_path does not exist: {monorepo_path}")

        source_repo = entry.get("source_repo", "")
        if source_repo:
            if not entry.get("archive_status"):
                errors.append(f"{slug}: source_repo set but archive_status missing")
            if not entry.get("archive_redirect_link"):
                errors.append(f"{slug}: source_repo set but archive_redirect_link missing")

    print(
        f"Checked {len(dirs)} integration directory(ies) and {len(entries)} inventory entry(ies)."
    )

    if errors:
        print(f"\n{len(errors)} integration standard violation(s) found:\n")
        for error in errors:
            print(f"  - {error}")
        return 1

    print("All integrations satisfy required structure and inventory metadata.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
