#!/usr/bin/env bash
# Cognee forget helper — server access for the cognee-forget skill.
#
# Wraps the endpoints used to inspect and delete documents so every request
# carries resolved credentials. Raw curl with a key from the shell breaks in local
# mode: the key is auto-minted at bootstrap into ~/.cognee-plugin/api_key.json
# and is never exported to the user's shell.
#
# Usage:
#   cognee-forget.sh sync                            Flush the live session into documents (run before listing)
#   cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
#   cognee-forget.sh data <dataset_id>               List the data items in a dataset
#   cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
#   cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
#   cognee-forget.sh forget-memory <dataset_id>      Clear a dataset's graph and vectors, keep its raw data (IRREVERSIBLE)
#   cognee-forget.sh forget-dataset <dataset_id>     Delete a whole dataset (IRREVERSIBLE)
#   cognee-forget.sh forget-everything --confirm-delete-all   Delete ALL of the user's data (IRREVERSIBLE)
#
# The key never leaves this script: no command prints it, and the skills send
# no request of their own.
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

# A shell has no key (the keys are plugin options, handed to hooks only). When a
# hook's relay is up it runs this script; exit code 111 means there is none.
if [ "${COGNEE_RELAYED:-}" != "1" ]; then
    relay_code=0
    python3 "${SELF_DIR}/_relay.py" run "$(basename "${BASH_SOURCE[0]:-$0}")" "$@" || relay_code=$?
    if [ "$relay_code" -ne 111 ]; then exit "$relay_code"; fi
fi

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
# The key the hooks send: the plugin option (present when this script runs
# through the relay a hook started), else the cached key of the local server.
api_key = ""
os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
try:
    import _plugin_common
    api_key = _plugin_common.shell_api_key(service_url)
except Exception:
    pass

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
SERVICE_URL="${SERVICE_URL%/}"

usage() {
    cat >&2 <<'EOF'
Usage:
  cognee-forget.sh sync                            Flush the live session into documents (run before listing)
  cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
  cognee-forget.sh data <dataset_id>               List the data items in a dataset
  cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
  cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
  cognee-forget.sh forget-memory <dataset_id>      Clear a dataset's graph and vectors, keep its raw data (IRREVERSIBLE)
  cognee-forget.sh forget-dataset <dataset_id>     Delete a whole dataset (IRREVERSIBLE)
  cognee-forget.sh forget-everything --confirm-delete-all   Delete ALL of the user's data (IRREVERSIBLE)
EOF
}

# Ids reach both the request URL and the JSON body, so anything that is not a
# canonical UUID is refused before it is used: a crafted value could otherwise
# redirect the request to a different endpoint, or close the JSON string and
# append fields (`{"everything": true}` deletes every dataset the user owns).
validate_uuid() {
    if ! [[ "$1" =~ ^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$ ]]; then
        echo "Error: invalid id (expected a UUID): $1" >&2
        exit 1
    fi
}

if [ -z "$API_KEY" ]; then
    echo "Error: no API key resolved (checked the plugin option and ${PLUGIN_DIR%/*}/api_key.json)." >&2
    echo "Cloud mode: set the Cognee API key option of this plugin (/plugin, Cognee Memory, Configure options) and start a session. Local mode: the key is minted at session start — start a new session or run cognee-doctor.sh." >&2
    exit 2
fi

CMD="${1:-}"
api_get() {
    curl -sS "$1" -H "X-Api-Key: ${API_KEY}" -w '\nHTTP %{http_code}\n'
}
# POST one fixed JSON body to /api/v1/forget. Callers pass a body they built
# from validated ids or from a literal; nothing free-form reaches it.
api_forget() {
    curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
        -H "Content-Type: application/json" \
        -H "X-Api-Key: ${API_KEY}" \
        -d "$1" \
        -w '\nHTTP %{http_code}\n'
}

case "$CMD" in
    datasets)
        api_get "${SERVICE_URL}/api/v1/datasets"
        ;;
    data)
        [ -n "${2:-}" ] || { echo "Error: data requires <dataset_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data"
        ;;
    raw)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "Error: raw requires <dataset_id> <data_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        validate_uuid "$3"
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data/$3/raw"
        ;;
    forget)
        [ -n "${2:-}" ] && [ -n "${3:-}" ] || { echo "Error: forget requires <dataset_id> <data_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        validate_uuid "$3"
        # Body built with json.dumps, not string interpolation, and the values
        # passed through the environment so they never touch argv or the
        # Python source. Belt and braces with validate_uuid above: this helper
        # must only ever be able to express a single-document delete.
        payload="$(FORGET_DATASET_ID="$2" FORGET_DATA_ID="$3" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"], "dataId": os.environ["FORGET_DATA_ID"]}))')"
        curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
            -H "Content-Type: application/json" \
            -H "X-Api-Key: ${API_KEY}" \
            -d "$payload" \
            -w '\nHTTP %{http_code}\n'
        ;;
    forget-memory)
        [ -n "${2:-}" ] || { echo "Error: forget-memory requires <dataset_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        api_forget "$(FORGET_DATASET_ID="$2" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"], "memoryOnly": True}))')"
        ;;
    forget-dataset)
        [ -n "${2:-}" ] || { echo "Error: forget-dataset requires <dataset_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        api_forget "$(FORGET_DATASET_ID="$2" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"]}))')"
        ;;
    forget-everything)
        # Deletes every dataset the user owns. The flag has to be typed out so
        # this can never be reached by a mistyped or truncated command.
        if [ "${2:-}" != "--confirm-delete-all" ] || [ -n "${3:-}" ]; then
            echo "Error: forget-everything deletes ALL of the user's data and needs exactly: --confirm-delete-all" >&2
            exit 1
        fi
        api_forget '{"everything": true}'
        ;;
    *)
        echo "Error: unknown command '${CMD}'" >&2
        usage
        exit 1
        ;;
esac
