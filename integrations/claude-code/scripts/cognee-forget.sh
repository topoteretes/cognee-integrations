#!/usr/bin/env bash
# Cognee forget helper — server access for the cognee-forget skill.
#
# Wraps the endpoints used to inspect and delete documents. The requests are
# made by cognee_forget.py, in one process: this shell holds no key and sends
# nothing itself. In local mode the request is authorized as the plugin's own
# server's owner; nothing is stored on disk. A Cognee Cloud key is a plugin
# option that Claude Code gives to hooks only, so in Cloud mode these commands
# cannot authenticate from a shell.
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
# "HTTP <status>" line — callers must check it (the command exits 0 on an
# HTTP error, deliberately, so 404 "already deleted" stays inspectable).
#
# Configuration:
#   Resolves auth like cognee-search.sh / cognee-remember.sh:
#   the local server's owner key, looked up from that server.
#   Exits 2 when no API key resolves: the server enforces auth even on
#   localhost, so an unauthenticated request can only ever 401.

set -euo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"

# `sync` delegates to the plugin's session sync (it resolves its own auth and
# session id): flushes the live session's cache into dataset documents so
# content that hasn't been persisted yet becomes findable — and deletable —
# by the listing steps below. Dispatched before credential resolution.
if [ "${1:-}" = "sync" ]; then
    exec python3 "${SELF_DIR}/sync-session-to-graph.py"
fi
exec python3 "${SELF_DIR}/cognee_forget.py" "$@"
