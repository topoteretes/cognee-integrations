#!/usr/bin/env bash
# Store text in Cognee's permanent knowledge graph.
#
# Usage:
#   cognee-remember.sh <content> [--node-set <node_set>] [--dataset <dataset>]
#
# --node-set: node set for categorization (default: user_context)
#             user_context | project_docs | agent_actions
# --dataset:  dataset name (default: COGNEE_PLUGIN_DATASET or agent_sessions)
#
# Configuration:
#   Resolves auth from env/api_key.json.
#   Dataset is env-driven: COGNEE_PLUGIN_DATASET, then agent_sessions.
#   Falls back to cognee-cli only if the server is unreachable.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/claude-code"
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" 2>/dev/null || true
import json
import pathlib
import sys

plugin_dir = pathlib.Path(sys.argv[1])
import os
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

dataset = (os.environ.get("COGNEE_PLUGIN_DATASET") or "").strip()

print(json.dumps({"service_url": service_url, "api_key": api_key, "dataset": dataset}))
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
DATASET="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("dataset") or "").strip())
except Exception:
    pass
PY
)"

[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"
[ -z "$DATASET" ] && DATASET="${COGNEE_PLUGIN_DATASET:-agent_sessions}"

# Parse arguments: content is first positional; flags follow
CONTENT="${1:-}"
NODE_SET="user_context"

shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --node-set)
            shift
            NODE_SET="${1:-user_context}"
            ;;
        --dataset|-d)
            shift
            DATASET="${1:-$DATASET}"
            ;;
        *)
            ;;
    esac
    shift || true
done

if [ -z "$CONTENT" ]; then
    echo "Error: no content provided" >&2
    exit 1
fi

# Server-first: POST to /api/v1/remember via _remember_http.py.
# UNREACHABLE → fall back to cognee-cli and warn.
# Any other result (ok or error) → authoritative; do not fall back.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
RESULT="$(python3 "${SELF_DIR}/_remember_http.py" "$SERVICE_URL" "$API_KEY" "$CONTENT" "$DATASET" "$NODE_SET" || true)"

if [ -n "$RESULT" ] && [ "$RESULT" != "UNREACHABLE" ]; then
    printf '%s\n' "$RESULT"
else
    echo "[cognee-remember] falling back to cognee-cli (degraded — server unreachable; verify the store succeeded once the server is back)" >&2
    cognee-cli remember "$CONTENT" -d "$DATASET" --node-set "$NODE_SET" 2>/dev/null || true
fi
