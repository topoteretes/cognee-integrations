#!/usr/bin/env bash
set -euo pipefail

ACTION="${1:-run-full}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [[ -z "${COGNEE_PYTHON:-}" ]]; then
  if [[ -n "${COGNEE_REPO:-}" && -x "${COGNEE_REPO}/.venv/bin/python" ]]; then
    COGNEE_PYTHON="${COGNEE_REPO}/.venv/bin/python"
  elif command -v python3 >/dev/null 2>&1; then
    COGNEE_PYTHON="$(command -v python3)"
  elif command -v python >/dev/null 2>&1; then
    COGNEE_PYTHON="$(command -v python)"
  else
    printf '{"status":"error","message":"Set COGNEE_PYTHON to a Python executable with Cognee installed."}\n'
    exit 1
  fi
fi

exec "$COGNEE_PYTHON" "$SCRIPT_DIR/run_self_improve_skill.py" "$ACTION"
