#!/usr/bin/env bash
# Cognee forget helper — server access for the cognee-forget skill.
#
# Wraps the endpoints used to inspect and delete documents so every request
# sends each request with the key resolved for it. A hand-written request breaks in local
# mode: the key is auto-minted at bootstrap into ~/.cognee-plugin/api_key.json
# and is never exported to the user's shell.
#
# Usage:
#   cognee-forget.sh sync                            Flush the live session into documents (run before listing)
#   cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
#   cognee-forget.sh data <dataset_id>               List the data items in a dataset
#   cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
#   cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
#   cognee-forget.sh forget-memory <dataset_id> --confirm-clear-memory     Clear a dataset's graph and vectors, keep its raw data (IRREVERSIBLE)
#   cognee-forget.sh forget-dataset <dataset_id> --confirm-delete-dataset  Delete a whole dataset (IRREVERSIBLE)
#   cognee-forget.sh forget-everything --confirm-delete-all   Delete ALL of the user's data (IRREVERSIBLE)
#
# The key never leaves this script: no command prints it, and the skills send
# no request of their own.
#
# Every command takes exactly the arguments shown, and the broad deletions a
# typed confirmation word, so a mistyped, truncated or overlong command line is
# refused before anything is sent.
#
# Every API command prints the response body followed by a final
# "HTTP <status>" line — callers must check it (an HTTP error still exits 0,
# deliberately, so 404 "already deleted" stays inspectable).
#
# Configuration:
#   Resolves auth like cognee-search.sh / cognee-remember.sh:
#   the plugin option (through the relay) > the cached key of the local server.
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
    if [ "$#" -ne 1 ]; then echo "Error: sync takes no arguments" >&2; exit 1; fi
    exec python3 "${SELF_DIR}/sync-session-to-graph.py"
fi
# The server address, resolved the way the hooks resolve it. The key never
# enters this script: scripts/_authed_request.py sends each request.
SERVICE_URL="$(python3 "${SELF_DIR}/_authed_request.py" url 2>/dev/null || true)"
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
  cognee-forget.sh forget-memory <dataset_id> --confirm-clear-memory     Clear a dataset's graph and vectors, keep its raw data (IRREVERSIBLE)
  cognee-forget.sh forget-dataset <dataset_id> --confirm-delete-dataset  Delete a whole dataset (IRREVERSIBLE)
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

key_code=0
python3 "${SELF_DIR}/_authed_request.py" has-key || key_code=$?
# 3: the run was aimed at a server the key is not for; the helper said so.
if [ "$key_code" -eq 3 ]; then exit 2; fi
if [ "$key_code" -ne 0 ]; then
    echo "Error: no API key resolved (checked the plugin option and ${PLUGIN_DIR%/*}/api_key.json)." >&2
    echo "Cloud mode: set the Cognee API key option of this plugin (/plugin, Cognee Memory, Configure options) and start a session. Local mode: the key is minted at session start — start a new session or run cognee-doctor.sh." >&2
    exit 2
fi

CMD="${1:-}"
# Refuse any command line that does not have exactly the expected shape.
expect() {
    if [ "$1" -ne "$2" ]; then
        echo "Error: ${CMD} requires exactly: $3" >&2
        usage
        exit 1
    fi
}
api_get() {
    python3 "${SELF_DIR}/_authed_request.py" GET "$1"
}
# POST one fixed JSON body to /api/v1/forget. Callers pass a body they built
# from validated ids or from a literal; nothing free-form reaches it.
api_forget() {
    printf '%s' "$1" | python3 "${SELF_DIR}/_authed_request.py" POST "${SERVICE_URL}/api/v1/forget"
}

case "$CMD" in
    datasets)
        expect "$#" 1 "datasets"
        api_get "${SERVICE_URL}/api/v1/datasets"
        ;;
    data)
        expect "$#" 2 "data <dataset_id>"
        validate_uuid "$2"
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data"
        ;;
    raw)
        expect "$#" 3 "raw <dataset_id> <data_id>"
        validate_uuid "$2"
        validate_uuid "$3"
        api_get "${SERVICE_URL}/api/v1/datasets/$2/data/$3/raw"
        ;;
    forget)
        expect "$#" 3 "forget <dataset_id> <data_id>"
        validate_uuid "$2"
        validate_uuid "$3"
        # Body built with json.dumps, not string interpolation, from the two
        # ids as arguments, so they never become part of the Python source.
        # Belt and braces with validate_uuid above: this helper must only ever
        # be able to express a single-document delete.
        payload="$(python3 -c 'import json, sys; print(json.dumps({"datasetId": sys.argv[1], "dataId": sys.argv[2]}))' "$2" "$3")"
        api_forget "$payload"
        ;;
    forget-memory)
        # A typed flag, like forget-everything: a mistyped or truncated
        # single-document command can never clear a whole dataset.
        if [ "$#" -ne 3 ] || [ "$3" != "--confirm-clear-memory" ]; then
            echo "Error: forget-memory clears a whole dataset's memory and takes exactly: <dataset_id> --confirm-clear-memory" >&2
            exit 1
        fi
        validate_uuid "$2"
        api_forget "$(python3 -c 'import json, sys; print(json.dumps({"datasetId": sys.argv[1], "memoryOnly": True}))' "$2")"
        ;;
    forget-dataset)
        if [ "$#" -ne 3 ] || [ "$3" != "--confirm-delete-dataset" ]; then
            echo "Error: forget-dataset deletes a whole dataset and takes exactly: <dataset_id> --confirm-delete-dataset" >&2
            exit 1
        fi
        validate_uuid "$2"
        api_forget "$(python3 -c 'import json, sys; print(json.dumps({"datasetId": sys.argv[1]}))' "$2")"
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
