#!/usr/bin/env bash
# Cognee Doctor — diagnostics for the Claude Code plugin.
#
# Usage:
#   cognee-doctor.sh            # human-readable output
#   cognee-doctor.sh --json     # JSON output

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
exec bash "${SELF_DIR}/run-python.sh" "${SELF_DIR}/doctor.py" "$@"
