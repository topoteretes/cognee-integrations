#!/usr/bin/env bash
# Index a code repository into Cognee's code graph (enola pipeline).
#
# Usage:
#   cognee-index-repo.sh <repo-path-or-git-url> [--dataset <name>] [--index-vectors] [--wait <seconds>]
#
# <repo-path-or-git-url>: a local repository path (works when the Cognee
#                         server shares this filesystem — the default local
#                         plugin server does) or a remote git URL (the server
#                         shallow-clones it; required for cloud servers).
# --dataset:       target dataset (default: codebase-<repo-name>-<digest> —
#                  one narrow dataset per repo keeps CODE searches fast. The
#                  digest is the indexed path: two checkouts often share a
#                  basename, and letting them share a dataset would let each
#                  ingestion's stale-node sweep delete the other's nodes. The
#                  resulting name is printed on success, and cognee-search.sh
#                  --code resolves it from the checkout automatically).
# --index-vectors: also embed the extracted code facts so semantic search can
#                  see them (needs an embedding provider; default off — the
#                  code graph pipeline makes no LLM/embedding calls).
# --wait:          seconds to poll the code_graph_pipeline status before
#                  returning (default 0: submit in background and return).
#
# Requires a Cognee server >= 1.5.3. There is deliberately NO CLI fallback:
# an unreachable server prints UNREACHABLE and nothing is submitted.
#
# Indexing a repo here is also the opt-in for automatic freshness, and what
# "fresh" means depends on how the server reads the code:
#   * local path  -> the server reads the working tree in place, so the graph
#     includes uncommitted and untracked changes. The plugin records a git
#     fingerprint and re-submits in the background after any turn that changed
#     a file, keeping the graph current per turn.
#   * git URL     -> the server clones the repo and can only ever see PUSHED
#     commits. Local edits are invisible to it, so the plugin does NOT
#     re-submit these on edits (a re-pull would change nothing); the graph
#     advances when you push and re-run this command. Normal behavior, but the
#     results look identical to the local case, so it is worth saying out loud
#     when answering from a cloud-indexed graph.

set -euo pipefail

PLUGIN_DIR="${HOME}/.cognee-plugin/claude-code"
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" >/dev/null 2>&1 && pwd)"
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

REPO="${1:-}"
DATASET=""
INDEX_VECTORS="false"
WAIT_SECONDS="0"

shift || true
while [ $# -gt 0 ]; do
    case "$1" in
        --dataset|-d)
            shift
            DATASET="${1:-}"
            ;;
        --index-vectors)
            INDEX_VECTORS="true"
            ;;
        --wait)
            shift
            WAIT_SECONDS="${1:-0}"
            ;;
        *)
            ;;
    esac
    shift || true
done

if [ -z "$REPO" ]; then
    echo "Error: no repository path or git URL provided" >&2
    echo "Usage: cognee-index-repo.sh <repo-path-or-git-url> [--dataset <name>] [--index-vectors] [--wait <seconds>]" >&2
    exit 1
fi

RESULT="$(python3 "${SELF_DIR}/_code_graph.py" "$SERVICE_URL" "$API_KEY" index "$REPO" "$DATASET" "$INDEX_VECTORS" "$WAIT_SECONDS" || true)"

if [ -n "$RESULT" ] && [ "$RESULT" != "UNREACHABLE" ]; then
    printf '%s\n' "$RESULT"
else
    echo "[cognee-index-repo] server unreachable at ${SERVICE_URL} — nothing was submitted. Start the server (new session) or fix COGNEE_BASE_URL, then re-run." >&2
    echo "UNREACHABLE"
    exit 1
fi
