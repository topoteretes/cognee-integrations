#!/usr/bin/env bash
# Review what the working tree did to the architecture since a base ref.
#
# Usage:
#   cognee-review.sh [<base-ref>] [--fail-on <explainers>] [--max-spillover <n>] [--target <t>]
#   cognee-review.sh --stored
#
# The review itself is cognee-review.py (its docstring has the flags); this
# wrapper keeps the path the skills and the docs use.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
exec python3 "${SELF_DIR}/cognee-review.py" "$@"
