#!/usr/bin/env bash
# Cognee forget helper — server access for the cognee-forget skill.
#
# Wraps the endpoints used to inspect and delete documents so every request
# carries resolved credentials. In local mode the key belongs to the plugin's
# own server and is looked up from it; nothing is stored on disk. A Cognee
# Cloud key is a plugin option that Claude Code gives to hooks only, so in
# Cloud mode these commands cannot authenticate from a shell.
#
# Usage:
#   cognee-forget.sh sync                            Flush the live session into documents (run before listing)
#   cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
#   cognee-forget.sh data <dataset_id>               List the data items in a dataset
#   cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
#   cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
#   cognee-forget.sh forget-memory <dataset_id>      Clear a dataset's graph and vectors, keep raw data (IRREVERSIBLE)
#   cognee-forget.sh forget-dataset <dataset_id>     Delete a whole dataset (IRREVERSIBLE)
#   cognee-forget.sh forget-everything --confirm-delete-all
#                                                    Delete ALL of the user's data (IRREVERSIBLE)
#
# Every API command prints the response body followed by a final
# "HTTP <status>" line — callers must check it (curl without -f exits 0 on
# HTTP errors, deliberately, so 404 "already deleted" stays inspectable).
#
# Configuration:
#   Resolves auth like cognee-search.sh / cognee-remember.sh:
#   the local server's owner key, looked up from that server.
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
    # Without the loader the environment is still the one the shell exported:
    # that URL and key are not plugin settings, so resolve nothing rather than
    # use them. (No apostrophes in this heredoc: bash 3.2 parses them.)
    print(json.dumps({"service_url": "", "api_key": ""}))
    raise SystemExit(0)
# After the loader this holds a plugin setting only: the URL option (or a
# loopback COGNEE_LOCAL_API_URL). A shell export of it was dropped.
service_url = (os.environ.get("COGNEE_LOCAL_API_URL") or os.environ.get("COGNEE_BASE_URL") or "http://localhost:8011").strip()

# No key is stored on disk or taken from the shell: against the plugin local
# server its owner key is looked up from that server. A Cloud key is a plugin
# option that reaches hooks only. The opt-out keeps _plugin_common from
# re-execing this heredoc into the plugin venv (stdin is the script): nothing
# here needs it.
api_key = ""
os.environ["COGNEE_PLUGIN_IN_VENV"] = "1"
try:
    from _plugin_common import _api_key_with_source
    api_key = _api_key_with_source(service_url)[0]
except Exception:
    pass

try:
    from _net import is_loopback_url
    loopback = is_loopback_url(service_url)
except Exception:
    loopback = False
print(json.dumps({"service_url": service_url, "api_key": api_key, "loopback": loopback}))
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
# "1" when the server is on this machine: such a request never goes through a
# proxy (curl would otherwise send it, with the key, to ALL_PROXY/http_proxy).
LOOPBACK="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print("1" if json.loads(sys.argv[1] or "{}").get("loopback") else "")
except Exception:
    pass
PY
)"
# Never fall back to the shell's COGNEE_BASE_URL: it is not the plugin's
# setting.
if [ -z "$SERVICE_URL" ]; then
    echo "Error: could not resolve the Cognee server URL from the plugin's settings (is python3 available?)." >&2
    exit 1
fi
SERVICE_URL="${SERVICE_URL%/}"

usage() {
    cat >&2 <<'EOF'
Usage:
  cognee-forget.sh sync                            Flush the live session into documents (run before listing)
  cognee-forget.sh datasets                        List datasets (find the plugin dataset's id)
  cognee-forget.sh data <dataset_id>               List the data items in a dataset
  cognee-forget.sh raw <dataset_id> <data_id>      Print a data item's raw stored content
  cognee-forget.sh forget <dataset_id> <data_id>   Delete one data item (IRREVERSIBLE)
  cognee-forget.sh forget-memory <dataset_id>      Clear a dataset's graph and vectors, keep raw data (IRREVERSIBLE)
  cognee-forget.sh forget-dataset <dataset_id>     Delete a whole dataset (IRREVERSIBLE)
  cognee-forget.sh forget-everything --confirm-delete-all
                                                   Delete ALL of the user's data (IRREVERSIBLE)
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
    echo "Error: no API key resolved." >&2
    echo "Cloud mode: the Cognee Cloud key is a plugin option that Claude Code passes to the plugin's hooks only, so this command cannot use it. Local mode: the key comes from the local server, which may not be running — start a new session or run cognee-doctor.sh." >&2
    exit 2
fi

CMD="${1:-}"
# Every request goes through here: a local server is reached directly.
server_curl() {
    if [ -n "$LOOPBACK" ]; then
        curl --noproxy '*' "$@"
    else
        curl "$@"
    fi
}
api_get() {
    server_curl -sS "$1" -H "X-Api-Key: ${API_KEY}" -w '\nHTTP %{http_code}\n'
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
        # Python source. Belt and braces with validate_uuid above: this command
        # must only ever be able to express a single-document delete.
        payload="$(FORGET_DATASET_ID="$2" FORGET_DATA_ID="$3" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"], "dataId": os.environ["FORGET_DATA_ID"]}))')"
        server_curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
            -H "Content-Type: application/json" \
            -H "X-Api-Key: ${API_KEY}" \
            -d "$payload" \
            -w '\nHTTP %{http_code}\n'
        ;;
    forget-memory|forget-dataset)
        # The broader deletions, each its own command taking exactly one
        # validated dataset id. Anything after the id is refused: a second id
        # is a single-document delete typed under the wrong command, and
        # ignoring it would delete the whole dataset. The key stays inside
        # this helper: it is never printed or exported to the calling shell.
        [ "$#" -eq 2 ] || { echo "Error: $CMD takes exactly one <dataset_id>" >&2; usage; exit 1; }
        validate_uuid "$2"
        if [ "$CMD" = "forget-memory" ]; then
            payload="$(FORGET_DATASET_ID="$2" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"], "memoryOnly": True}))')"
        else
            payload="$(FORGET_DATASET_ID="$2" python3 -c 'import json, os; print(json.dumps({"datasetId": os.environ["FORGET_DATASET_ID"]}))')"
        fi
        server_curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
            -H "Content-Type: application/json" \
            -H "X-Api-Key: ${API_KEY}" \
            -d "$payload" \
            -w '\nHTTP %{http_code}\n'
        ;;
    forget-everything)
        # Deletes every dataset the user owns. It names no target, so it runs
        # only with this one literal argument and nothing else: a bare
        # command, a typo or an extra argument sends no request.
        if [ "$#" -ne 2 ] || [ "${2:-}" != "--confirm-delete-all" ]; then
            echo "Error: forget-everything deletes ALL of the user's data and runs only as: cognee-forget.sh forget-everything --confirm-delete-all" >&2
            usage
            exit 1
        fi
        server_curl -sS -X POST "${SERVICE_URL}/api/v1/forget" \
            -H "Content-Type: application/json" \
            -H "X-Api-Key: ${API_KEY}" \
            -d '{"everything": true}' \
            -w '\nHTTP %{http_code}\n'
        ;;
    *)
        echo "Error: unknown command '${CMD}'" >&2
        usage
        exit 1
        ;;
esac
