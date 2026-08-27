#!/usr/bin/env bash
# Run a Cognee hook with Python 3.10 or newer.

set -euo pipefail

is_compatible() {
  "$1" -c 'import sys; raise SystemExit(sys.version_info < (3, 10))' >/dev/null 2>&1
}

if [ -n "${COGNEE_PYTHON:-}" ]; then
  if ! command -v "${COGNEE_PYTHON}" >/dev/null 2>&1 || ! is_compatible "${COGNEE_PYTHON}"; then
    echo "Cognee requires COGNEE_PYTHON to point to Python 3.10 or newer." >&2
    exit 127
  fi
  exec "${COGNEE_PYTHON}" "$@"
fi

for candidate in \
  python3.14 python3.13 python3.12 python3.11 python3.10 \
  /opt/homebrew/bin/python3.14 /opt/homebrew/bin/python3.13 \
  /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
  /opt/homebrew/bin/python3.10 /usr/local/bin/python3.14 \
  /usr/local/bin/python3.13 /usr/local/bin/python3.12 \
  /usr/local/bin/python3.11 /usr/local/bin/python3.10 \
  python3 python
do
  if command -v "$candidate" >/dev/null 2>&1 && is_compatible "$candidate"; then
    exec "$candidate" "$@"
  fi
done

echo "Cognee requires Python 3.10 or newer. Set COGNEE_PYTHON to a compatible interpreter." >&2
exit 127
