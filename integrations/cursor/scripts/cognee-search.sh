#!/usr/bin/env bash
# Search Cognee's memory: the knowledge graph, or a repository's code graph.
#
# Usage:
#   cognee-search.sh <query> [top_k] [--graph]
#   cognee-search.sh <query> [top_k] --code [--dataset <name>] [--code-query '<json>']
#   cognee-search.sh <query> [top_k] --graph --dataset-id <uuid>
#
# --graph:   search the permanent knowledge graph (the default; the flag is
#            accepted for callers that spell it out)
# --code:    deterministic code-graph search (cognee >= 1.5.3). Query text is
#            the seed; --code-query selects an exact operation instead, e.g.
#            '{"operation": "impact_analysis", "targets": ["process_payment"]}'
#            (operations: query_facts, explore, traverse, find_path,
#            impact_analysis, delta). The repository's own code dataset is
#            resolved from the current directory automatically.
# --dataset: override the dataset to search (default: the plugin dataset, or
#            the current repo's code dataset in --code mode). A name only
#            resolves among datasets this identity OWNS; anything else must be
#            addressed by UUID.
# --dataset-id: search another dataset by UUID (the cross-dataset picker flow:
#            list-datasets.py names the candidates). Searching a dataset other
#            than the launch's active one is graph-only — session history is
#            bound to the active dataset — so the scope is forced to graph and
#            the session id is dropped, with a note on stderr.
# No flag:   same as --graph. Memory is read from the graph and the code graph
#            only; the session cache is written, never searched.
#
# Configuration:
#   Session ID and dataset come from this launch's record (~/.cognee-plugin/
#   codex/sessions/<host id>.json — follows a dataset switch), falling
#   back to the Cognee connection endpoint and COGNEE_PLUGIN_DATASET / agent_sessions.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/codex"
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
dataset_ids = ""
# The launch record wins (dataset + session chosen with switch-dataset.py, the
# dataset UUIDs under shared memory, the plugin-agent key the hooks use).
# NOTE: no apostrophes in this block - bash 3.2 scans $( ... ) for quotes
# without understanding the heredoc, and a lone quote breaks the whole script.
try:
    from _plugin_common import shell_runtime_overrides
    _rt = shell_runtime_overrides(service_url)
    dataset = _rt["dataset"] or dataset
    session_id = _rt["session_id"] or session_id
    dataset_ids = _rt["dataset_ids"]
    api_key = _rt["api_key"] or api_key
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

print(json.dumps({"session_id": session_id, "dataset": dataset, "dataset_ids": dataset_ids, "service_url": service_url, "api_key": api_key}))
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
DATASET_IDS="$(python3 - <<'PY' "${runtime_json}" 2>/dev/null || true
import json, sys
try:
    print((json.loads(sys.argv[1] or "{}").get("dataset_ids") or "").strip())
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
[ -z "$SESSION_ID" ] && SESSION_ID="${COGNEE_SESSION_ID:-codex_session}"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"
[ -z "$API_KEY" ] && API_KEY="${COGNEE_API_KEY:-}"

# The launch's active dataset, by name and by every UUID graph recall spans
# (the canonical write id is always first among them), so an explicit target
# can be told apart from "the active dataset, by hand".
ACTIVE_DATASET="$DATASET"
ACTIVE_DATASET_IDS="$DATASET_IDS"

QUERY="${1:-}"
TOP_K="${2:-5}"
MODE="graph"
CODE_QUERY=""
DATASET_EXPLICIT=""

# Parse flags from any position (value flags consume the next argument)
_args=("$@")
_i=0
while [ $_i -lt ${#_args[@]} ]; do
    case "${_args[$_i]}" in
        --graph)   MODE="graph" ;;
        --code)    MODE="code" ;;
        --code-query)
            _i=$((_i + 1))
            CODE_QUERY="${_args[$_i]:-}"
            ;;
        --dataset|-d|--dataset-id)
            _i=$((_i + 1))
            DATASET="${_args[$_i]:-$DATASET}"
            DATASET_EXPLICIT="1"
            ;;
    esac
    _i=$((_i + 1))
done

# The resolved UUIDs belong to the launch's active dataset only: an explicit
# --dataset or the code lane (the repo's own dataset) must search by name.
if [ -n "${DATASET_EXPLICIT:-}" ] || [ "$MODE" = "code" ]; then
    DATASET_IDS=""
fi

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

# A dataset other than the active one has none of this session's history, and
# the server rejects a session bound to one dataset being read against another
# — so a foreign target is a graph-only read with no session id. The active
# dataset named by hand (its name or one of its UUIDs) keeps the full scope.
FOREIGN=""
if [ -n "${DATASET_EXPLICIT:-}" ] && [ "$MODE" != "code" ]; then
    FOREIGN="1"
    [ "$DATASET" = "$ACTIVE_DATASET" ] && FOREIGN=""
    case ",${ACTIVE_DATASET_IDS}," in
        *",${DATASET},"*) FOREIGN="" ;;
    esac
    if [ -n "$FOREIGN" ]; then
        if [ "$MODE" != "graph" ]; then
            echo "[cognee-search] dataset '$DATASET' is not this session's active dataset (${ACTIVE_DATASET:-unknown}) — searching its knowledge graph only (no session history there)" >&2
        fi
        MODE="graph"
        SESSION_ID=""
    fi
fi

# Search scope from MODE. Graph and code only: the session cache is never a
# search source (its history reaches the model through the graph item's
# prompt on cognee >= 1.6.0, and through the sync bridge before that).
case "$MODE" in
    code) SCOPE='["code"]' ;;
    *)    SCOPE='["graph"]' ;;
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
RECALL_JSON="$(python3 "${SELF_DIR}/_cognee_client.py" "$SERVICE_URL" "$API_KEY" "$QUERY" "$SESSION_ID" "$SCOPE" "$TOP_K" "$DATASET" "$CODE_QUERY" "$DATASET_IDS" || true)"

if [ -n "$RECALL_JSON" ] && [ "$RECALL_JSON" != "UNREACHABLE" ]; then
    # Server answered — authoritative, even if the result is empty.
    printf '%s\n' "$RECALL_JSON"
elif [ "$MODE" = "code" ] || [ -n "$FOREIGN" ]; then
    # No CLI fallback for code searches or for another dataset: the code lane
    # exists only on the server (>= 1.5.3), and a dataset addressed by UUID
    # resolves only there — a CLI recall would answer from a different backend
    # or identity and read as authoritative when it is not.
    echo "[cognee-search] server unreachable — search not run; retry once the server is back" >&2
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
