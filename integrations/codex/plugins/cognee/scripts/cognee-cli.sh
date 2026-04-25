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

exec uv run cognee-cli "$@"
