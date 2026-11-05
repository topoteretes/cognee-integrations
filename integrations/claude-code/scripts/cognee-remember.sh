#!/usr/bin/env bash
# Store text in Cognee's permanent knowledge graph.
#
# Usage:
#   cognee-remember.sh <content> [--node-set <node_set>] [--dataset <dataset>]
#   cognee-remember.sh --file <path> [--node-set <node_set>] [--dataset <dataset>]
#
# The write itself is cognee_remember.py (its docstring has the flags); this
# wrapper keeps the path the skills and the docs use.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
exec python3 "${SELF_DIR}/cognee_remember.py" "$@"
