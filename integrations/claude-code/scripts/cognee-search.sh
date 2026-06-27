#!/usr/bin/env bash
# Search Cognee's memory (session or permanent graph).
#
# Usage:
#   cognee-search.sh <query> [top_k] [--session | --graph]
#
# --session: search session cache only
# --graph:   search permanent knowledge graph only
# No flag:   search session first, then graph if empty
#
# Configuration:
#   Resolves session ID and dataset from Cognee endpoints using API auth.
#   Falls back to env vars when endpoint lookup is unavailable.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/claude-code"
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" 2>/dev/null || true
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

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

session_id = ""
dataset = ""
if service_url and api_key:
    try:
        query = ""
        session_key = (os.environ.get("COGNEE_SESSION_KEY") or "").strip()
        if session_key:
            query = "?agent_session_name=" + urllib.parse.quote(session_key, safe="")
        req = urllib.request.Request(
            service_url.rstrip("/") + "/api/v1/agents/connections/me" + query,
            headers={"X-Api-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=3.0) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(payload, dict):
            agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
            if isinstance(agent, dict):
                session_id = str(agent.get("session_id") or "").strip()
                datasets = agent.get("datasets") if isinstance(agent.get("datasets"), list) else []
                for item in datasets:
                    if isinstance(item, dict):
                        name = str(item.get("name") or "").strip()
                        if name:
                            dataset = name
                            break
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError):
        pass

print(json.dumps({"session_id": session_id, "dataset": dataset, "service_url": service_url, "api_key": api_key}))
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
SESSION_ID="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("session_id") or "").strip())
except Exception:
    pass
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
[ -z "$DATASET" ] && DATASET="${COGNEE_PLUGIN_DATASET:-agent_sessions}"
[ -z "$SESSION_ID" ] && SESSION_ID="${COGNEE_SESSION_ID:-claude_session}"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"

QUERY="${1:-}"
TOP_K="${2:-5}"
MODE="auto"

# Parse flags from any position
for arg in "$@"; do
    case "$arg" in
        --session) MODE="session" ;;
        --graph)   MODE="graph" ;;
    esac
done

if [ -z "$QUERY" ]; then
    echo "Error: no query provided" >&2
    exit 1
fi

# Search scope from MODE
case "$MODE" in
    session) SCOPE='["session"]' ;;
    graph)   SCOPE='["graph"]' ;;
    *)       SCOPE='["session", "graph"]' ;;
esac

# Server-first: the running server (/api/v1/recall) is the source of truth.
# Only a 2xx response is authoritative (an empty list = genuinely no hits).
# Any non-2xx / error / unreachable returns the UNREACHABLE sentinel so we fall
# back to cognee-cli and warn — never reporting a server failure as "not found".
# $DATASET is resolved above (connections/me → COGNEE_PLUGIN_DATASET → default)
# and scopes the search to the plugin's dataset so unrelated datasets don't bleed in.
# Logic lives in _recall_http.py (stdlib-only, unit-tested); stderr is surfaced.
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
export COGNEE_PLUGIN_STATE_DIR="$PLUGIN_DIR"
RECALL_JSON="$(python3 "${SELF_DIR}/_cognee_client.py" "$SERVICE_URL" "$API_KEY" "$QUERY" "$SESSION_ID" "$SCOPE" "$TOP_K" "$DATASET" || true)"

if [ -n "$RECALL_JSON" ] && [ "$RECALL_JSON" != "UNREACHABLE" ]; then
    # Server answered — authoritative, even if the result is empty.
    printf '%s\n' "$RECALL_JSON"
else
    echo "[cognee-search] falling back to cognee-cli (degraded — empty CLI output is NOT proof of absence; ground-truth via: curl -X POST \"\$COGNEE_BASE_URL/api/v1/recall\")" >&2
    if [ "$MODE" = "graph" ]; then
        cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null || true
    elif [ "$MODE" = "session" ]; then
        cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null || true
    else
        RESULT=$(cognee-cli recall "$QUERY" -s "$SESSION_ID" -k "$TOP_K" -f json 2>/dev/null || true)
        if [ -n "$RESULT" ] && [ "$RESULT" != "[]" ]; then
            echo "$RESULT"
        else
            cognee-cli recall "$QUERY" -d "$DATASET" -k "$TOP_K" -f json 2>/dev/null || true
        fi
    fi
fi
