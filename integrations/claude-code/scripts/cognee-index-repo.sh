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

# A shell has no key (the keys are plugin options, handed to hooks only). When a
# hook's relay is up it runs this script; exit code 111 means there is none.
if [ "${COGNEE_RELAYED:-}" != "1" ]; then
    relay_code=0
    python3 "${SELF_DIR}/_relay.py" run "$(basename "${BASH_SOURCE[0]:-$0}")" "$@" || relay_code=$?
    if [ "$relay_code" -ne 111 ]; then exit "$relay_code"; fi
fi
# The server address, resolved the way the hooks resolve it. The key never
# enters this script: _code_graph.py resolves it for itself.
SERVICE_URL="$(python3 "${SELF_DIR}/_authed_request.py" url 2>/dev/null || true)"
[ -z "$SERVICE_URL" ] && SERVICE_URL="${COGNEE_BASE_URL:-${COGNEE_LOCAL_API_URL:-http://localhost:8011}}"

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

RESULT="$(python3 "${SELF_DIR}/_code_graph.py" "$SERVICE_URL" - index "$REPO" "$DATASET" "$INDEX_VECTORS" "$WAIT_SECONDS" || true)"

if [ -n "$RESULT" ] && [ "$RESULT" != "UNREACHABLE" ]; then
    printf '%s\n' "$RESULT"
else
    echo "[cognee-index-repo] server unreachable at ${SERVICE_URL} — nothing was submitted. Start the server (new session) or fix COGNEE_BASE_URL, then re-run." >&2
    echo "UNREACHABLE"
    exit 1
fi
