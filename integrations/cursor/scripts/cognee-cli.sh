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
# the plugin (<private> spans, then secret patterns). Option names and existing
# paths pass unchanged: a file goes up byte for byte, as with cognee-remember.sh
# --file. The value of --opt=value is sanitized like any text, and so is text
# that merely starts with "-" (a "- note" bullet is a positional to argparse).
# A --code-query value is JSON, so it is sanitized field by field and stays valid.
# Checked before the cd below, so relative paths resolve where they were typed.
if [[ "${1:-}" =~ ^(remember|add|recall|search)$ ]]; then
  SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
  opt_re='^--?[A-Za-z][A-Za-z0-9-]*'
  args=("$1")
  shift
  json=""  # "--json" while the next argument is a spaced --code-query value
  for arg in "$@"; do
    if [[ "$arg" =~ ${opt_re}$ || -e "$arg" ]]; then
      args+=("$arg")
      if [[ "$arg" == "--code-query" ]]; then json="--json"; else json=""; fi
    elif [[ "$arg" =~ ${opt_re}= ]]; then
      # Split at the first "=": the value (a JSON --code-query, say) may hold more.
      opt="${arg%%=*}"
      val="${arg#*=}"
      if [[ -e "$val" ]]; then
        args+=("$arg")
      else
        if [[ "$opt" == "--code-query" ]]; then json="--json"; fi
        clean="$(python3 "${SELF_DIR}/_capture_policy.py" ${json:+"$json"} "$val")" || exit 1
        args+=("${opt}=${clean}")
      fi
      json=""
    else
      clean="$(python3 "${SELF_DIR}/_capture_policy.py" ${json:+"$json"} "$arg")" || exit 1
      args+=("$clean")
      json=""
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
