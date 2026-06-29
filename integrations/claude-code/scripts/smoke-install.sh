#!/usr/bin/env bash
# Smoke check for the Cognee Memory plugin install/update flow.
#
# Verifies, on a fresh checkout, that the install/update steps documented in the
# Claude Code README stay consistent with what the marketplace would actually
# resolve when a user runs:
#
#   /plugin marketplace add topoteretes/cognee-integrations
#   /plugin install cognee-memory@cognee
#
# Exits 0 when everything lines up, non-zero with a clear message otherwise.
set -euo pipefail

# Resolve paths relative to this script so it runs from any working directory.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_ROOT="$(cd "$PLUGIN_DIR/../.." && pwd)"

README="$PLUGIN_DIR/README.md"
PLUGIN_MANIFEST="$PLUGIN_DIR/.claude-plugin/plugin.json"
MARKETPLACE_MANIFEST="$REPO_ROOT/.claude-plugin/marketplace.json"

MARKETPLACE="cognee"
PLUGIN="cognee-memory"
SOURCE_REPO="topoteretes/cognee-integrations"

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok: $*"; }

# 1. Required files exist.
for f in "$README" "$PLUGIN_MANIFEST" "$MARKETPLACE_MANIFEST"; do
  [ -f "$f" ] || fail "missing required file: $f"
done
ok "README and plugin/marketplace manifests present"

# 2. Documented install/update commands are present in the README.
require_in_readme() {
  grep -qF "$1" "$README" || fail "README is missing documented command: $1"
}
require_in_readme "/plugin marketplace add $SOURCE_REPO"
require_in_readme "/plugin install $PLUGIN@$MARKETPLACE"
require_in_readme "/plugin uninstall $PLUGIN@$MARKETPLACE"
require_in_readme "/plugin marketplace remove $SOURCE_REPO"
ok "documented install + update commands present in README"

# 3. Manifests are valid JSON and the install target actually resolves.
#    This is what makes the README commands work: marketplace name "cognee",
#    a plugin named "cognee-memory", and a source path that points at this plugin.
python3 - "$MARKETPLACE_MANIFEST" "$PLUGIN_MANIFEST" "$REPO_ROOT" "$MARKETPLACE" "$PLUGIN" <<'PY'
import json, sys, os

mkt_path, plugin_path, repo_root, marketplace, plugin = sys.argv[1:6]

with open(mkt_path) as f:
    mkt = json.load(f)
with open(plugin_path) as f:
    plug = json.load(f)

if mkt.get("name") != marketplace:
    sys.exit(f"FAIL: marketplace name is {mkt.get('name')!r}, expected {marketplace!r}")

entries = [p for p in mkt.get("plugins", []) if p.get("name") == plugin]
if not entries:
    sys.exit(f"FAIL: marketplace does not declare a plugin named {plugin!r}")
entry = entries[0]

source = entry.get("source")
if not source:
    sys.exit(f"FAIL: plugin {plugin!r} has no source path")
resolved = os.path.normpath(os.path.join(repo_root, source))
if not os.path.isdir(resolved):
    sys.exit(f"FAIL: plugin source path does not resolve to a directory: {source} -> {resolved}")

if plug.get("name") != plugin:
    sys.exit(f"FAIL: plugin.json name is {plug.get('name')!r}, expected {plugin!r}")

if entry.get("version") != plug.get("version"):
    sys.exit(
        f"FAIL: version mismatch — marketplace {entry.get('version')!r} vs "
        f"plugin.json {plug.get('version')!r}"
    )

print(f"ok: {plugin}@{marketplace} resolves to {source} (v{plug.get('version')})")
PY

echo "Claude Code plugin install/update smoke check passed."
