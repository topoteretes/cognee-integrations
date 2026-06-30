#!/usr/bin/env python3
"""Smoke-test that the Claude Code plugin marketplace manifest and plugin.json are valid.

Checks:
  1. marketplace.json is valid JSON and has expected structure
  2. plugin.json is valid JSON and has expected structure
  3. The plugin listed in marketplace.json points to a valid source path
  4. The plugin's hooks file exists (if referenced)

Exit 0 on success, 1 on failure — suitable for CI.
"""

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ERRORS = []


def check(path: Path, label: str):
    if not path.exists():
        ERRORS.append(f"{label}: file not found at {path}")
        return {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        ERRORS.append(f"{label}: invalid JSON — {e}")
        return {}

    if not isinstance(data, dict):
        ERRORS.append(f"{label}: root must be a JSON object, got {type(data).__name__}")
        return {}

    return data


def check_claude_code_plugin():
    plugin_dir = REPO_ROOT / "integrations" / "claude-code"
    plugin_json = plugin_dir / ".claude-plugin" / "plugin.json"
    hooks_json = plugin_dir / "hooks" / "hooks.json"

    data = check(plugin_json, "claude-code plugin.json")
    if not data:
        return

    required_keys = ["name", "version", "description"]
    for key in required_keys:
        if key not in data:
            ERRORS.append(f"claude-code plugin.json: missing required key '{key}'")

    version = data.get("version", "")
    if not re.match(r"^\d+\.\d+\.\d+", version):
        ERRORS.append(f"claude-code plugin.json: version '{version}' does not match semver")

    if hooks_json.exists():
        hooks_data = check(hooks_json, "claude-code hooks.json")
        if hooks_data:
            hook_scripts = hooks_json.parent.parent / "scripts"
            for scripts in hooks_data.values() if isinstance(hooks_data, dict) else []:
                if isinstance(scripts, list):
                    for script_path in scripts:
                        candidate = hook_scripts / str(script_path)
                        if not candidate.exists():
                            ERRORS.append(f"claude-code hooks.json: referenced script '{script_path}' not found at {candidate}")


def check_marketplace():
    marketplace = REPO_ROOT / ".claude-plugin" / "marketplace.json"
    data = check(marketplace, "marketplace.json")
    if not data:
        return

    plugins = data.get("plugins", [])
    if not isinstance(plugins, list) or not plugins:
        ERRORS.append("marketplace.json: 'plugins' must be a non-empty list")
        return

    for i, plugin in enumerate(plugins):
        if not isinstance(plugin, dict):
            ERRORS.append(f"marketplace.json plugins[{i}]: must be an object")
            continue

        name = plugin.get("name", f"<index {i}>")
        required = ["name", "version", "source"]
        for key in required:
            if key not in plugin:
                ERRORS.append(f"marketplace.json plugin '{name}': missing required key '{key}'")

        source = plugin.get("source", "")
        if source and isinstance(source, str):
            source_path = REPO_ROOT / source
            if not source_path.exists():
                ERRORS.append(f"marketplace.json plugin '{name}': source path '{source}' does not exist at {source_path}")


def main():
    check_marketplace()
    check_claude_code_plugin()

    if ERRORS:
        print("Smoke install check FAILED:", file=sys.stderr)
        for err in ERRORS:
            print(f"  - {err}", file=sys.stderr)
        sys.exit(1)

    print("All install manifest checks passed.")
    print(f"  marketplace.json: valid, {len(json.loads((REPO_ROOT / '.claude-plugin' / 'marketplace.json').read_text()).get('plugins', []))} plugin(s)")
    print("  claude-code plugin.json: valid")
    sys.exit(0)


if __name__ == "__main__":
    main()
