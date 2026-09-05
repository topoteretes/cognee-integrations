#!/usr/bin/env bash
# Search Cognee's memory (session or permanent graph).
#
# Usage:
#   cognee-search.sh <query> [top_k] [--session | --graph]
#   cognee-search.sh <query> [top_k] --code [--dataset <name>] [--code-query '<json>']
#
# --session: search session cache only
# --graph:   search permanent knowledge graph only
# --code:    deterministic code-graph search (cognee >= 1.5.3). Query text is
#            the seed; --code-query selects an exact operation instead, e.g.
#            '{"operation": "impact_analysis", "targets": ["process_payment"]}'
#            (operations: query_facts, explore, traverse, find_path,
#            impact_analysis, delta). The repository's own code dataset is
#            resolved from the current directory automatically.
# --dataset: override the dataset to search (default: the plugin dataset, or
#            the current repo's code dataset in --code mode)
# No flag:   search session first, then graph if empty
#
# Configuration:
#   Session ID and dataset come from this launch's record (~/.cognee-plugin/
#   antigravity/sessions/<host id>.json — follows a dataset switch), falling
#   back to the Cognee connection endpoint and COGNEE_PLUGIN_DATASET / agent_sessions.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/antigravity"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
runtime_json="$(python3 - <<'PY' "${PLUGIN_DIR}" "${SELF_DIR}" 2>/dev/null || true
import json
import pathlib
import sys
import urllib.error
import urllib.parse
import urllib.request

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

session_id = ""
dataset = (os.environ.get("COGNEE_PLUGIN_DATASET") or "").strip()
# The launch record wins: it carries the dataset + session chosen with
# switch-dataset.py, which the shell env and a first-connection lookup lack.
try:
    from _plugin_common import _read_map_record, resolve_host_key_outside_hook
    _host_key, _ = resolve_host_key_outside_hook()
    _rec = _read_map_record(_host_key) if _host_key else {}
    if str(_rec.get("dataset") or "").strip():
        dataset = str(_rec["dataset"]).strip()
    if str(_rec.get("session_id") or "").strip():
        session_id = str(_rec["session_id"]).strip()
except Exception:
    pass
if not session_id and service_url and api_key:
    try:
        import ssl
        try:
            import certifi
            _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        except ImportError:
            # macOS host python often lacks root CAs; fall back like the recall path.
            _ssl_ctx = ssl.create_default_context()
            for _p in filter(None, [os.environ.get("SSL_CERT_FILE"), "/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"]):
                if os.path.exists(_p):
                    try:
                        _ssl_ctx.load_verify_locations(_p)
                        break
                    except Exception:
                        pass
        query = ""
        session_key = (os.environ.get("COGNEE_SESSION_KEY") or "").strip()
        if session_key:
            query = "?agent_session_name=" + urllib.parse.quote(session_key, safe="")
        req = urllib.request.Request(
            service_url.rstrip("/") + "/api/v1/agents/connections/me" + query,
            headers={"X-Api-Key": api_key},
        )
        with urllib.request.urlopen(req, timeout=3.0, context=_ssl_ctx) as resp:
            payload = json.loads(resp.read().decode("utf-8") or "{}")
        if isinstance(payload, dict):
            agent = payload.get("agent") if isinstance(payload.get("agent"), dict) else {}
            if isinstance(agent, dict):
                session_id = str(agent.get("session_id") or "").strip()
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
[ -z "$SESSION_ID" ] && SESSION_ID="${COGNEE_SESSION_ID:-antigravity_session}"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"

QUERY="${1:-}"
TOP_K="${2:-5}"
MODE="auto"
CODE_QUERY=""
DATASET_EXPLICIT=""

# Parse flags from any position (value flags consume the next argument)
_args=("$@")
_i=0
while [ $_i -lt ${#_args[@]} ]; do
    case "${_args[$_i]}" in
        --session) MODE="session" ;;
        --graph)   MODE="graph" ;;
        --code)    MODE="code" ;;
        --code-query)
            _i=$((_i + 1))
            CODE_QUERY="${_args[$_i]:-}"
            ;;
        --dataset|-d)
            _i=$((_i + 1))
            DATASET="${_args[$_i]:-$DATASET}"
            DATASET_EXPLICIT="1"
            ;;
    esac
    _i=$((_i + 1))
done

# Code searches target the repository's OWN dataset, whose name carries a path
# digest (two checkouts can share a basename, so the basename cannot be the
# identity). Resolve it from the current checkout rather than making the caller
# reconstruct it. An unindexed cwd leaves DATASET alone, and the search then
# reports no code facts rather than silently querying the session dataset.
if [ "$MODE" = "code" ] && [ -z "${DATASET_EXPLICIT:-}" ]; then
    CODE_DATASET="$(python3 "${SELF_DIR}/_code_graph.py" "" "" dataset "$PWD" 2>/dev/null || true)"
    [ -n "$CODE_DATASET" ] && DATASET="$CODE_DATASET"
fi

if [ -z "$QUERY" ]; then
    echo "Error: no query provided" >&2
    exit 1
fi

# Search scope from MODE
case "$MODE" in
    session) SCOPE='["session"]' ;;
    graph)   SCOPE='["graph"]' ;;
    code)    SCOPE='["code"]' ;;
    *)       SCOPE='["session", "graph"]' ;;
esac

# Server-first: the running server (/api/v1/recall) is the source of truth.
# Only a 2xx response is authoritative (an empty list = genuinely no hits).
# Any non-2xx / error / unreachable returns the UNREACHABLE sentinel so we fall
# back to cognee-cli and warn — never reporting a server failure as "not found".
# $DATASET is resolved above (COGNEE_PLUGIN_DATASET → default)
# and scopes the search to the plugin's dataset so unrelated datasets don't bleed in.
# Logic lives in _recall_http.py (stdlib-only, unit-tested); stderr is surfaced.
# No COGNEE_PLUGIN_STATE_DIR override here: the circuit breaker must be the ONE
# at ~/.cognee-plugin/recall-breaker.json that the per-prompt hooks, doctor and
# the status line use. Pointing it at the per-plugin dir gave this skill its own
# breaker, so a server the hooks had already given up on looked healthy here.
RECALL_JSON="$(python3 "${SELF_DIR}/_cognee_client.py" "$SERVICE_URL" "$API_KEY" "$QUERY" "$SESSION_ID" "$SCOPE" "$TOP_K" "$DATASET" "$CODE_QUERY" || true)"

if [ -n "$RECALL_JSON" ] && [ "$RECALL_JSON" != "UNREACHABLE" ]; then
    # Server answered — authoritative, even if the result is empty.
    printf '%s\n' "$RECALL_JSON"
elif [ "$MODE" = "code" ]; then
    # No CLI fallback for code searches: the deterministic code lane exists
    # only on the server (>= 1.5.3); a CLI recall would answer from the wrong
    # retriever and read as authoritative when it is not.
    echo "[cognee-search] server unreachable — code search not run; retry once the server is back" >&2
    echo "UNREACHABLE"
    exit 1
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
