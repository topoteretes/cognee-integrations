#!/usr/bin/env bash
set -euo pipefail

if [[ -n "${COGNEE_REPO_ROOT:-}" ]]; then
  cd "$COGNEE_REPO_ROOT"
elif [[ -f pyproject.toml ]] && grep -q '^name = "cognee"$' pyproject.toml; then
  :
else
  echo "Run this from the Cognee repository root or set COGNEE_REPO_ROOT." >&2
  exit 64
fi

# Doctor subcommand: delegate to the plugin's doctor.py
if [[ "${1:-}" == "doctor" ]]; then
  shift
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
  exec python "${SELF_DIR}/doctor.py" "$@"
fi

exec uv run cognee-cli "$@"
