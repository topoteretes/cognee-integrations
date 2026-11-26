#!/usr/bin/env bash
# Propose architecture rules mined from the code graph; accept or reject them.
#
# Usage:
#   cognee-rules.sh list [--limit <n>]
#   cognee-rules.sh accept <id> | reject <id>
#
# The work is cognee-rules.py (its docstring has the flags); this
# wrapper keeps the path the skills and the docs use.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
exec python3 "${SELF_DIR}/cognee-rules.py" "$@"
