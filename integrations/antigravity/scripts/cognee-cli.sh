#!/usr/bin/env bash
set -euo pipefail

# Doctor subcommand: a self-contained, read-only diagnostic that does not need
# the Cognee repo, so dispatch it before the repo-root gate below (you often
# run the doctor from wherever you are when something looks off).
if [[ "${1:-}" == "doctor" ]]; then
  shift
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
  exec python3 "${SELF_DIR}/doctor.py" "$@"
fi

if [[ -n "${COGNEE_REPO_ROOT:-}" ]]; then
  cd "$COGNEE_REPO_ROOT"
elif [[ -f pyproject.toml ]] && grep -q '^name = "cognee"$' pyproject.toml; then
  :
else
  echo "Run this from the Cognee repository root or set COGNEE_REPO_ROOT." >&2
  exit 64
fi

exec uv run cognee-cli "$@"
