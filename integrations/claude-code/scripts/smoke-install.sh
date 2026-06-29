#!/usr/bin/env bash
#
# Smoke-test the one-command install of the Cognee Memory plugin end to end,
# against the local checkout, in an isolated Claude Code config dir so it never
# touches your real ~/.claude. Verifies the same path a user runs:
#
#     /plugin marketplace add <repo>
#     /plugin install cognee-memory@cognee
#
# Usage:
#   integrations/claude-code/scripts/smoke-install.sh              # full end-to-end
#   integrations/claude-code/scripts/smoke-install.sh --manifest-only  # no claude CLI needed
#
# Exits 0 on success, non-zero on the first failed step.
set -euo pipefail

MARKETPLACE="cognee"
PLUGIN="cognee-memory"
PLUGIN_REF="${PLUGIN}@${MARKETPLACE}"

# repo root = three levels up from integrations/claude-code/scripts/
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PLUGIN_DIR="${REPO_ROOT}/integrations/claude-code"
MARKETPLACE_MANIFEST="${REPO_ROOT}/.claude-plugin/marketplace.json"
PLUGIN_MANIFEST="${PLUGIN_DIR}/.claude-plugin/plugin.json"

MANIFEST_ONLY=0
[ "${1:-}" = "--manifest-only" ] && MANIFEST_ONLY=1

pass() { printf '  \033[32m✔\033[0m %s\n' "$1"; }
info() { printf '\033[1m==>\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

# ── 1. Manifests exist and are well-formed ──────────────────────────────────
info "Checking plugin and marketplace manifests"
[ -f "${MARKETPLACE_MANIFEST}" ] || fail "missing ${MARKETPLACE_MANIFEST}"
[ -f "${PLUGIN_MANIFEST}" ] || fail "missing ${PLUGIN_MANIFEST}"

python3 - "${MARKETPLACE_MANIFEST}" "${PLUGIN_MANIFEST}" "${MARKETPLACE}" "${PLUGIN}" <<'PY'
import json
import sys

mkt_path, plugin_path, marketplace, plugin = sys.argv[1:5]

mkt = json.load(open(mkt_path))
plg = json.load(open(plugin_path))

assert mkt.get("name") == marketplace, f"marketplace name != {marketplace!r}"
entry = next((p for p in mkt.get("plugins", []) if p.get("name") == plugin), None)
assert entry, f"{plugin!r} not listed in marketplace.json"
assert entry.get("source") == "./integrations/claude-code", "unexpected plugin source path"
assert plg.get("name") == plugin, f"plugin.json name != {plugin!r}"
assert plg.get("version"), "plugin.json is missing a version"

# the marketplace entry should agree with the plugin manifest version
if "version" in entry:
    assert entry["version"] == plg["version"], (
        f"version drift: marketplace={entry['version']} plugin={plg['version']}"
    )

print(f"  manifests OK — {plugin} v{plg['version']} via {marketplace}")
PY
pass "manifests well-formed and consistent"

if [ "${MANIFEST_ONLY}" -eq 1 ]; then
  info "Manifest-only mode — skipping the live install"
  pass "smoke check passed"
  exit 0
fi

# ── 2. Need the claude CLI for the live install ─────────────────────────────
if ! command -v claude >/dev/null 2>&1; then
  fail "claude CLI not found. Install Claude Code, or re-run with --manifest-only."
fi

# Isolate everything in a throwaway config dir so the real ~/.claude is untouched.
CONFIG_DIR="$(mktemp -d)"
export CLAUDE_CONFIG_DIR="${CONFIG_DIR}"
cleanup() { rm -rf "${CONFIG_DIR}"; }
trap cleanup EXIT
info "Using isolated CLAUDE_CONFIG_DIR=${CONFIG_DIR}"

# ── 3. Validate manifests with the CLI (strict) ─────────────────────────────
info "Validating manifests with the Claude Code CLI"
claude plugin validate "${PLUGIN_DIR}" --strict
claude plugin validate "${REPO_ROOT}" --strict
pass "claude plugin validate passed"

# ── 4. marketplace add + install (the documented one-command path) ──────────
info "claude plugin marketplace add ${REPO_ROOT}"
claude plugin marketplace add "${REPO_ROOT}"

info "claude plugin install ${PLUGIN_REF}"
claude plugin install "${PLUGIN_REF}" --scope user

# ── 5. Assert it landed and is enabled ──────────────────────────────────────
info "Verifying the plugin is installed and enabled"
if ! claude plugin list 2>&1 | grep -q "${PLUGIN_REF}"; then
  fail "${PLUGIN_REF} not present in 'claude plugin list'"
fi
pass "${PLUGIN_REF} installed and enabled"

info "Smoke test passed ✅"
