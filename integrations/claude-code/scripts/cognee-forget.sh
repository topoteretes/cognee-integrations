#!/usr/bin/env bash
# Cognee forget helper — server access for the cognee-forget skill.
#
# Wraps the endpoints used to inspect and delete documents so every request
# carries resolved credentials. Raw curl with $COGNEE_API_KEY breaks in local
# mode: the key is auto-minted at bootstrap into ~/.cognee-plugin/api_key.json
# and is never exported to the user's shell.
#
# Usage:
#   cognee-forget.sh sync                            Flush the live session into documents (run before listing)
#   cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
#   cognee-forget.sh data <dataset_id>               List the data items in a dataset
#   cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
#   cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
#   cognee-forget.sh env                             Print eval-able COGNEE_BASE_URL/COGNEE_API_KEY exports
#
# Every API command prints the response body followed by a final
# "HTTP <status>" line — callers must check it (curl without -f exits 0 on
# HTTP errors, deliberately, so 404 "already deleted" stays inspectable).
#
# Configuration:
#   Resolves auth like cognee-search.sh / cognee-remember.sh:
#   shell env > ~/.cognee/.env > cached api_key.json (local auto-minted key).
#   Exits 2 when no API key resolves: the server enforces auth even on
#   localhost, so an unauthenticated request can only ever 401.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/claude-code"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"

# `sync` delegates to the plugin's session sync (it resolves its own auth and
# session id): flushes the live session's cache into dataset documents so
# content that hasn't been persisted yet becomes findable — and deletable —
# by the listing steps below. Dispatched before credential resolution.
if [ "${1:-}" = "sync" ]; then
    exec python3 "${SELF_DIR}/sync-session-to-graph.py"
fi
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" "${SELF_DIR}" 2>/dev/null || true
import json
import pathlib
import sys

plugin_dir = pathlib.Path(sys.argv[1])
import os
# One-time config from ~/.cognee/.env (shell exports still win).
sys.path.insert(0, sys.argv[2])
try:
    from _env_file import load_env_file
    load_env_file()
except Exception:
    pass
service_url = (os.environ.get("COGNEE_BASE_URL") or os.environ.get("COGNEE_LOCAL_API_URL") or "http://localhost:8011").strip()
api_key = (os.environ.get("COGNEE_API_KEY") or "").strip()

if not api_key:
    cache_path = plugin_dir.parent / "api_key.json"
    if cache_path.exists():
        try:
            cache = json.loads(cache_path.read_text())
            if isinstance(cache, dict):
                key = str(cache.get("api_key") or "").strip()
                cached_url = str(cache.get("base_url") or "").strip().rstrip("/")
                if key and (not cached_url or cached_url == service_url.rstrip("/")):
                    api_key = key
        except Exception:
            pass

print(json.dumps({"service_url": service_url, "api_key": api_key}))
PY
)"

SERVICE_URL="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("service_url") or "").strip())
except Exception:
    pass
PY
)"
API_KEY="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("api_key") or "").strip())
except Exception:
    pass
PY
)"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"
SERVICE_URL="${SERVICE_URL%/}"

usage() {
    cat >&2 <<'EOF'
Usage:
  cognee-forget.sh sync                            Flush the live session into documents (run before listing)
  cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
  cognee-forget.sh data <dataset_id>               List the data items in a dataset
  cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
  cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
  cognee-forget.sh env                             Print eval-able COGNEE_BASE_URL/COGNEE_API_KEY exports
EOF
}

if [ -z "$API_KEY" ]; then
    echo "Error: no API key resolved (checked COGNEE_API_KEY, ~/.cognee/.env, ${PLUGIN_DIR%/*}/api_key.json)." >&2
    echo "Cloud mode: set COGNEE_API_KEY in ~/.cognee/.env. Local mode: the key is minted at session start — start a new session or run cognee-doctor.sh." >&2
    exit 2
fi

CMD="${1:-}"
api_get() {
    curl -sS "$1" -H "X-Api-Key: ${API_KEY}" -w '\nHTTP %{http_code}\n'
}

case "$CMD" in
    datasets)
        api_get "${SERVICE_URL}/api/v1/datasets"
        ;;
    data)
        [ -n "${2:-}" ] || { echo "Error: data requires <dataset_id>" >&2; usage; exit 1; }
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data"
        ;;
    raw)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "Error: raw requires <dataset_id> <data_id>" >&2; usage; exit 1; }
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data/$3/raw"
        ;;
    forget)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "Error: forget requires <dataset_id> <data_id>" >&2; usage; exit 1; }
        curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
            -H "Content-Type: application/json" \
            -H "X-Api-Key: ${API_KEY}" \
            -d "{\"datasetId\": \"$2\", \"dataId\": \"$3\"}" \
            -w '\nHTTP %{http_code}\n'
        ;;
    env)
        # For manual/broader calls only (see SKILL.md) — eval in the same shell
        # as the curl that uses it; exports do not persist across Bash calls.
        printf 'export COGNEE_BASE_URL=%q\nexport COGNEE_API_KEY=%q\n' "$SERVICE_URL" "$API_KEY"
        ;;
    *)
        echo "Error: unknown command '${CMD}'" >&2
        usage
        exit 1
        ;;
esac
