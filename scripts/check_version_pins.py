#!/usr/bin/env python3
"""Check that all Python integrations pin the cognee dependency with a bounded range.

Policy: Every Python integration that depends on cognee must declare it with both
a lower bound (>=) and an upper bound (<). For example: cognee>=0.5.1,<0.6.0

Handles:
  - Simple deps:        "cognee>=0.5.1,<0.6.0"
  - With extras:        "cognee[graph]>=0.5.1,<0.6.0"
  - Git/URL deps:       "cognee @ git+https://..." -> flagged as error
  - Optional deps:      checks [project.optional-dependencies] too

Non-Python integrations (TypeScript, etc.) are skipped — they may depend on
Cognee via HTTP API rather than a Python package.

Usage:
    python scripts/check_version_pins.py
"""

import sys
import re
from pathlib import Path

INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "integrations"

# Matches cognee dependency with optional extras: "cognee..." or "cognee[extra]..."
# The negative lookahead (?![\w-]) prevents matching longer names like "cognee-dify-plugin".
COGNEE_DEP_PATTERN = re.compile(r'"(cognee(?:\[[^\]]*\])?)(?![\w-])\s*([^"]*)"')

# Git / URL dependency: cognee @ git+... or cognee @ https://...
COGNEE_URL_PATTERN = re.compile(r'"cognee(?![\w-])\s*@\s*[^"]*"')

# Check for lower bound (>= or >)
LOWER_BOUND_PATTERN = re.compile(r">=?\s*\d")

# Check for upper bound (< or <=, but not <=>, !=)
UPPER_BOUND_PATTERN = re.compile(r"(?<!=)<\s*\d")


def check_pyproject(pyproject_path: Path) -> list[str]:
    """Check a single integration's pyproject.toml for proper cognee pinning."""
    errors = []
    content = pyproject_path.read_text()
    integration_name = pyproject_path.parent.name

    # Check for URL/git dependencies (always an error for released packages)
    url_matches = COGNEE_URL_PATTERN.findall(content)
    for url_dep in url_matches:
        errors.append(
            f"{integration_name}: cognee uses URL/git dependency (not publishable). "
            f"Found: {url_dep}"
        )

    # Find all cognee version-pinned dependencies (main + optional)
    matches = COGNEE_DEP_PATTERN.findall(content)
    if not matches and not url_matches:
        # No cognee dependency at all — skip (might be intentional)
        return []

    for dep_name, version_spec in matches:
        version_spec = version_spec.strip()

        if not version_spec:
            errors.append(
                f"{integration_name}: cognee dependency has no version constraint. "
                f"Found: {dep_name} (unpinned)"
            )
            continue

        if not LOWER_BOUND_PATTERN.search(version_spec):
            errors.append(
                f"{integration_name}: cognee dependency missing lower bound (>=). "
                f"Found: {dep_name}{version_spec}"
            )

        if not UPPER_BOUND_PATTERN.search(version_spec):
            errors.append(
                f"{integration_name}: cognee dependency missing upper bound (<). "
                f"Found: {dep_name}{version_spec}"
            )

    return errors


def main():
    if not INTEGRATIONS_DIR.exists():
        print(f"Integrations directory not found: {INTEGRATIONS_DIR}")
        sys.exit(1)

    all_errors = []
    checked = 0
    skipped = 0

    for integration_dir in sorted(INTEGRATIONS_DIR.iterdir()):
        if not integration_dir.is_dir():
            continue

        pyproject = integration_dir / "pyproject.toml"
        if not pyproject.exists():
            # Non-Python integration (e.g., TypeScript/OpenClaw plugin) — skip
            skipped += 1
            continue

        checked += 1
        errors = check_pyproject(pyproject)
        all_errors.extend(errors)

    print(f"Checked {checked} Python integration(s), skipped {skipped} non-Python.")

    if checked == 0:
        print("No Python integrations with pyproject.toml found.")
        sys.exit(0)

    if all_errors:
        print(f"\n{len(all_errors)} pinning violation(s) found:\n")
        for error in all_errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("All Python integrations have properly bounded cognee dependencies.")
        sys.exit(0)


if __name__ == "__main__":
    main()
