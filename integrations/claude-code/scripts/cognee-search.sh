#!/usr/bin/env bash
# Search Cognee's memory: the knowledge graph, or a repository's code graph.
#
# Usage:
#   cognee-search.sh <query> [top_k] [--graph]
#   cognee-search.sh <query> [top_k] --code [--dataset <name>] [--code-query '<json>']
#   cognee-search.sh <query> [top_k] --graph --dataset-id <uuid>
#
# The search itself is cognee_search.py (its docstring has the flags); this
# wrapper keeps the path the skills and the docs use.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
exec python3 "${SELF_DIR}/cognee_search.py" "$@"
