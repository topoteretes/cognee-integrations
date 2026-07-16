---
name: cognee-search
description: Search Cognee memory. Session memory is automatically searched on every prompt via hooks. Use this skill explicitly for permanent knowledge graph search, filtered category search, or when you need more results than the automatic lookup provides.
---

# Cognee Memory Search

Search both session memory and the permanent knowledge graph, optionally filtered by data category.

## Automatic session search

Session memory is searched **automatically on every user prompt** via the `UserPromptSubmit` hook. You do not need to run this skill to access current-session context.

## Data categories

Knowledge is organized into three categories via `node_set`:

| Category | Node set | Contains |
|----------|----------|----------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts |

## Instructions

Search goes through the **running Cognee server** (`POST /api/v1/recall`) — the source of truth. Use the wrapper below: it queries the server first, searches **all your authorized datasets** (so a hit isn't missed because it lives in another dataset), and falls back to `cognee-cli` only if the server is unreachable.

**One broad search is usually enough** — the `UserPromptSubmit` hook already injects session/trace/graph context every turn, so avoid running many targeted searches (each is an extra permission prompt for the user).

### Search (server-first)

```bash
# session cache + permanent graph (default)
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS"

# permanent graph only
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS" 10 --graph

# current session only
${CLAUDE_PLUGIN_ROOT}/scripts/cognee-search.sh "$ARGUMENTS" 10 --session
```

### Filter by category (optional)

Categories (`user_context` / `project_docs` / `agent_actions`) filter by node set. `cognee-cli recall` does **not** expose this — pass `node_name` to the server directly:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "node_name": ["project_docs"]}'
```

### Ground-truth a suspicious result (debugging)

The server is authoritative. If a search returns empty but you expect content, confirm directly — **do not** conclude "not found" from empty CLI output:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"]}'
```

(An authed/cloud server needs `COGNEE_API_KEY`; a local single-user server ignores an empty key. If the response is an `{"error": ...}` object rather than a list, the server was reachable but rejected/failed the request — that's an error, **not** "no results".)

### Fallback only — server unreachable

`cognee-cli` is a thin client over the same server and can print **empty stdout even when content exists**. Use it only when the server is down, and treat empty output as *inconclusive*, never as "no results":

```bash
cognee-cli recall "$ARGUMENTS" -k 5 -f json
```

## Understanding results

Results include a `_source` field:
- `"session"` — from the session cache (current conversation)
- `"graph"` — from the permanent knowledge graph

Session entries tagged with `[category:agent]` are automatic tool call logs.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | Already automatic, no action needed |
| User explicitly says "search cognee" | `cognee-search.sh "<query>"` (server-first) |
| "what does the codebase do" / "what did we do last time" | `cognee-search.sh "<query>" 10 --graph` |
| Need a specific category | use the `node_name` curl form above (`["user_context"\|"project_docs"\|"agent_actions"]`) |
| Auto context insufficient | `cognee-search.sh "<query>" 10 --session` |
| **Result empty but you expect content** | **Ground-truth via the `curl` above before concluding "not found"** |
