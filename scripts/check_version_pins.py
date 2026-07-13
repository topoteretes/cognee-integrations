#!/usr/bin/env python3
"""Check that all Python integrations pin the cognee dependency with a bounded range.

Policy: Every Python integration that depends on cognee must declare it with both
a lower bound (>=) and an upper bound (< or <=). For example: cognee>=0.5.1,<0.6.0

Handles:
  - Simple deps:        "cognee>=0.5.1,<0.6.0"
  - With extras:        "cognee[graph]>=0.5.1,<0.6.0"
  - Git/URL deps:       "cognee @ git+https://..." -> flagged as error
  - Optional deps:      checks [project.optional-dependencies] too

Non-Python integrations (TypeScript, etc.) are skipped — they may depend on
Cognee via HTTP API rather than a Python package.

Usage:
    python scripts/check_version_pins.py                       # check every integration
    python scripts/check_version_pins.py integrations/vellum/pyproject.toml ...
    python scripts/check_version_pins.py vellum strands        # by slug or dir

With no arguments every integration is checked. Given one or more targets
(a slug, an integration dir, or a pyproject.toml path) only those are checked —
CI uses this to validate just the integrations a PR changes, so a PR is not
blocked by pre-existing pinning debt in integrations it does not touch.
"""

import re
import sys
from pathlib import Path

INTEGRATIONS_DIR = Path(__file__).resolve().parent.parent / "integrations"
REPO_ROOT = INTEGRATIONS_DIR.parent

# Matches cognee dependency with optional extras: "cognee..." or "cognee[extra]..."
# The negative lookahead (?![\w-]) prevents matching longer names like "cognee-dify-plugin".
COGNEE_DEP_PATTERN = re.compile(r'"(cognee(?:\[[^\]]*\])?)(?![\w-])\s*([^"]*)"')

# Git / URL dependency: cognee @ git+... or cognee @ https://...
COGNEE_URL_PATTERN = re.compile(r'"cognee(?![\w-])\s*@\s*[^"]*"')

# Check for lower bound (>= or >)
LOWER_BOUND_PATTERN = re.compile(r">=?\s*\d")

# Check for upper bound (< or <=, but not !=)
UPPER_BOUND_PATTERN = re.compile(r"(?<!=)<=?\s*\d")


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


def _pyproject_for(target: str) -> Path:
    """Resolve a target (slug, integration dir, or pyproject.toml path) to its pyproject."""
    path = Path(target)
    if not path.is_absolute():
        path = REPO_ROOT / path
    if path.name == "pyproject.toml":
        return path
    if path.is_dir():
        return path / "pyproject.toml"
    # Bare slug, e.g. "vellum".
    return INTEGRATIONS_DIR / target / "pyproject.toml"


def _pyprojects_to_check(argv):
    """The pyproject files to check: the given targets, or every integration."""
    if argv:
        seen = set()
        for target in argv:
            pyproject = _pyproject_for(target)
            if pyproject not in seen:
                seen.add(pyproject)
                yield pyproject
        return
    for integration_dir in sorted(INTEGRATIONS_DIR.iterdir()):
        if integration_dir.is_dir():
            yield integration_dir / "pyproject.toml"


def main(argv):
    if not INTEGRATIONS_DIR.exists():
        print(f"Integrations directory not found: {INTEGRATIONS_DIR}")
        sys.exit(1)

    all_errors = []
    checked = 0
    skipped = 0

    for pyproject in _pyprojects_to_check(argv):
        if not pyproject.exists():
            # Non-Python integration (e.g. TypeScript plugin) or a changed path
            # that isn't a Python integration — nothing to check.
            skipped += 1
            continue

        checked += 1
        all_errors.extend(check_pyproject(pyproject))

    scope = "changed" if argv else "all"
    print(f"Checked {checked} Python integration(s), skipped {skipped} ({scope} scope).")

    if checked == 0:
        print("No Python integrations with pyproject.toml to check.")
        sys.exit(0)

    if all_errors:
        print(f"\n{len(all_errors)} pinning violation(s) found:\n")
        for error in all_errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print("All checked integrations have properly bounded cognee dependencies.")
        sys.exit(0)


if __name__ == "__main__":
    main(sys.argv[1:])
