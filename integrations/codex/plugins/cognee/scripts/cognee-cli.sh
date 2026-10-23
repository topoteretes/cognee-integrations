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

# Text handed to remember/add/recall/search is sanitized like everywhere else in
# the plugin (<private> spans, then secret patterns). Options and existing paths
# pass unchanged: a file goes up byte for byte, as with cognee-remember.sh --file.
# Checked before the cd below, so relative paths resolve where they were typed.
if [[ "${1:-}" =~ ^(remember|add|recall|search)$ ]]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
  args=("$1")
  shift
  for arg in "$@"; do
    if [[ "$arg" == -* || -e "$arg" ]]; then
      args+=("$arg")
    else
      clean="$(python3 "${SELF_DIR}/_capture_policy.py" "$arg")" || exit 1
      args+=("$clean")
    fi
  done
  set -- "${args[@]}"
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
