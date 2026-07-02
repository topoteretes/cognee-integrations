#!/usr/bin/env bash
#
# smoke-install.sh — end-to-end install/update smoke check for the
# Cognee Memory plugin for Claude Code.
#
# A user installs this plugin by running, in the Claude Code chat:
#
#   /plugin marketplace add topoteretes/cognee-integrations
#   /plugin install cognee-memory@cognee
#
# This script verifies, on a fresh checkout, that those commands would actually
# resolve and that the installed plugin would be runnable — without needing a
# live Cognee server. It checks the documentation, the marketplace/plugin
# manifests, and that every component the plugin declares (hook scripts and
# skills) is present on disk.
#
# Usage:
#   bash integrations/claude-code/scripts/smoke-install.sh
#   bash integrations/claude-code/scripts/smoke-install.sh --quiet   # only print the final result
#
# Exit codes:
#   0  every check passed
#   1  one or more checks failed
#   2  a prerequisite is missing (e.g. python3 not found)
set -euo pipefail

QUIET=0
for arg in "$@"; do
  case "$arg" in
    -q|--quiet) QUIET=1 ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    *) echo "unknown argument: $arg (try --help)" >&2; exit 2 ;;
  esac
done

# Resolve paths relative to this script so it runs from any working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"

command -v python3 >/dev/null 2>&1 || { echo "FAIL: python3 is required but not found" >&2; exit 2; }

PLUGIN_DIR="$PLUGIN_DIR" REPO_ROOT="$REPO_ROOT" QUIET="$QUIET" python3 - <<'PY'
import json
import os
import re
import sys

PLUGIN_DIR = os.environ["PLUGIN_DIR"]
REPO_ROOT = os.environ["REPO_ROOT"]
QUIET = os.environ.get("QUIET") == "1"

MARKETPLACE = "cognee"
PLUGIN = "cognee-memory"
SOURCE_REPO = "topoteretes/cognee-integrations"

README = os.path.join(PLUGIN_DIR, "README.md")
PLUGIN_MANIFEST = os.path.join(PLUGIN_DIR, ".claude-plugin", "plugin.json")
HOOKS_MANIFEST = os.path.join(PLUGIN_DIR, "hooks", "hooks.json")
SKILLS_DIR = os.path.join(PLUGIN_DIR, "skills")
MARKETPLACE_MANIFEST = os.path.join(REPO_ROOT, ".claude-plugin", "marketplace.json")

passed = 0
failures = []


def ok(msg):
    global passed
    passed += 1
    if not QUIET:
        print(f"  ✓ {msg}")


def fail(msg):
    failures.append(msg)
    if not QUIET:
        print(f"  ✗ {msg}")


def section(title):
    if not QUIET:
        print(f"\n{title}")


def load_json(path, label):
    if not os.path.isfile(path):
        fail(f"{label}: missing file ({path})")
        return None
    try:
        with open(path) as f:
            data = json.load(f)
        ok(f"{label}: present and valid JSON")
        return data
    except json.JSONDecodeError as e:
        fail(f"{label}: invalid JSON — {e}")
        return None


# 1. Manifests exist and are valid JSON.
section("Manifests")
marketplace = load_json(MARKETPLACE_MANIFEST, "marketplace.json")
plugin = load_json(PLUGIN_MANIFEST, "plugin.json")
hooks = load_json(HOOKS_MANIFEST, "hooks.json")

# 2. The /plugin install target actually resolves.
section("Install target (/plugin install cognee-memory@cognee)")
entry = None
if marketplace is not None:
    if marketplace.get("name") == MARKETPLACE:
        ok(f"marketplace name is {MARKETPLACE!r}")
    else:
        fail(f"marketplace name is {marketplace.get('name')!r}, expected {MARKETPLACE!r}")

    entries = [p for p in marketplace.get("plugins", []) if p.get("name") == PLUGIN]
    if entries:
        entry = entries[0]
        ok(f"marketplace declares plugin {PLUGIN!r}")
        source = entry.get("source")
        if not source:
            fail(f"plugin {PLUGIN!r} has no source path")
        else:
            resolved = os.path.normpath(os.path.join(REPO_ROOT, source))
            if os.path.isdir(resolved):
                ok(f"source path resolves: {source}")
            else:
                fail(f"source path does not resolve to a directory: {source}")
    else:
        fail(f"marketplace does not declare a plugin named {PLUGIN!r}")

# 3. plugin.json has the fields a marketplace install relies on, and versions agree.
section("Plugin manifest integrity")
if plugin is not None:
    for field in ("name", "version", "description"):
        if plugin.get(field):
            ok(f"plugin.json has {field!r}")
        else:
            fail(f"plugin.json is missing required field {field!r}")
    if plugin.get("name") and plugin["name"] != PLUGIN:
        fail(f"plugin.json name is {plugin['name']!r}, expected {PLUGIN!r}")
    if entry is not None and plugin.get("version"):
        if entry.get("version") == plugin["version"]:
            ok(f"version matches across manifests (v{plugin['version']})")
        else:
            fail(
                f"version mismatch — marketplace {entry.get('version')!r} "
                f"vs plugin.json {plugin['version']!r}"
            )

# 4. Documentation: one-command install + update steps are present.
section("Documentation (README)")
if os.path.isfile(README):
    with open(README) as f:
        readme_text = f.read()
    documented = [
        f"/plugin marketplace add {SOURCE_REPO}",
        f"/plugin install {PLUGIN}@{MARKETPLACE}",
        f"/plugin uninstall {PLUGIN}@{MARKETPLACE}",
        f"/plugin marketplace remove {SOURCE_REPO}",
    ]
    for cmd in documented:
        if cmd in readme_text:
            ok(f"documents: {cmd}")
        else:
            fail(f"README is missing documented command: {cmd}")
else:
    fail(f"README missing ({README})")

# 5. Every hook the plugin registers points at a script that exists.
#    This is what guarantees the plugin is runnable right after /plugin install.
section("Hook scripts resolve")
if hooks is not None:
    referenced = set()
    for event, blocks in hooks.get("hooks", {}).items():
        for block in blocks:
            for hook in block.get("hooks", []):
                cmd = hook.get("command", "")
                for m in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}/(\S+\.py)", cmd):
                    referenced.add(m)
    if not referenced:
        fail("hooks.json declares no runnable hook commands")
    for rel in sorted(referenced):
        path = os.path.join(PLUGIN_DIR, rel)
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            ok(f"hook script present: {rel}")
        else:
            fail(f"hook script referenced but missing/empty: {rel}")

# 6. Declared skills resolve (each needs a SKILL.md).
section("Skills resolve")
if os.path.isdir(SKILLS_DIR):
    skill_dirs = sorted(
        d for d in os.listdir(SKILLS_DIR)
        if os.path.isdir(os.path.join(SKILLS_DIR, d))
    )
    if not skill_dirs:
        fail("skills/ directory has no skills")
    for d in skill_dirs:
        skill_md = os.path.join(SKILLS_DIR, d, "SKILL.md")
        if os.path.isfile(skill_md):
            ok(f"skill present: {d}")
        else:
            fail(f"skill {d!r} is missing SKILL.md")
else:
    fail("skills/ directory missing")

# Summary.
total = passed + len(failures)
print()
if failures:
    print(f"FAIL: {len(failures)} of {total} checks failed:")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)

print(f"PASS: all {total} checks passed.")
print(f"      {PLUGIN}@{MARKETPLACE} installs cleanly and is runnable from a fresh checkout.")
PY
