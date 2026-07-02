#!/usr/bin/env bash
# Smoke-test the one-command install/update path end-to-end on a clean checkout.
#
# Verifies, against THIS checkout's manifests (not the published repo):
#   claude plugin validate      -> marketplace + plugin manifests are well-formed
#   claude plugin marketplace add <local path>
#   claude plugin install cognee-memory@<marketplace>
#   claude plugin list          -> the plugin is actually installed
#   claude plugin update        -> the documented update path resolves
#   claude plugin uninstall     -> the documented remove path resolves
#
# Runs entirely inside a throwaway CLAUDE_CONFIG_DIR, so it never touches your
# real Claude Code config and is safe in CI. Exits 0 on success, non-zero on the
# first failing step.
#
# Usage: integrations/claude-code/scripts/smoke-install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
PLUGIN_DIR="${REPO_ROOT}/integrations/claude-code"
MARKETPLACE_MANIFEST="${REPO_ROOT}/.claude-plugin/marketplace.json"
MARKETPLACE_NAME="cognee"
PLUGIN_REF="cognee-memory@${MARKETPLACE_NAME}"

if ! command -v claude >/dev/null 2>&1; then
  echo "FAIL: 'claude' CLI not found on PATH (install Claude Code first)" >&2
  exit 127
fi

# Isolate all state so we never mutate the user's real Claude config.
CLAUDE_CONFIG_DIR="$(mktemp -d "${TMPDIR:-/tmp}/cognee-smoke.XXXXXX")"
export CLAUDE_CONFIG_DIR
cleanup() { rm -rf "${CLAUDE_CONFIG_DIR}"; }
trap cleanup EXIT

step() { printf '\n▶ %s\n' "$*"; }

step "validate marketplace manifest"
claude plugin validate "${MARKETPLACE_MANIFEST}"

step "validate plugin manifest"
claude plugin validate "${PLUGIN_DIR}"

step "marketplace add (from local checkout: ${REPO_ROOT})"
claude plugin marketplace add "${REPO_ROOT}"

step "install ${PLUGIN_REF}"
claude plugin install "${PLUGIN_REF}"

step "confirm the plugin is installed"
if ! claude plugin list 2>&1 | grep -q "cognee-memory"; then
  echo "FAIL: cognee-memory not present in 'claude plugin list' after install" >&2
  exit 1
fi

step "update path resolves"
claude plugin marketplace update "${MARKETPLACE_NAME}"
claude plugin update "${PLUGIN_REF}"

step "remove path resolves"
claude plugin uninstall "${PLUGIN_REF}"

printf '\n✅ SMOKE OK: install + update + remove verified on a clean checkout\n'
