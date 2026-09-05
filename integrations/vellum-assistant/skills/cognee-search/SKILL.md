---
name: cognee-search
description: Search Cognee memory. Session memory is automatically searched on every prompt via hooks. Use this skill explicitly for permanent knowledge graph search, filtered category search, or when you need more results than the automatic lookup provides.
---

# Cognee Memory Search

Search both session memory and the permanent knowledge graph, optionally filtered by data category.

## Automatic session search

Session memory is searched **automatically on every user prompt** via the `user-prompt-submit` hook. You do not need to run this skill to access current-session context.

## Data categories

Knowledge is organized into three categories via `node_set`:

| Category | Node set | Contains |
|----------|----------|----------|
| **user** | `user_context` | User preferences, corrections, personal facts |
| **project** | `project_docs` | Repository docs, code context, architecture decisions |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts |

## Instructions

Use the **cognee_recall** tool to search Cognee memory. It calls the running Cognee server (`POST /api/v1/recall`) directly in-process — no shell, no subprocess.

**One broad search is usually enough** — the `user-prompt-submit` hook already injects session/trace/graph context every turn, so avoid running many targeted searches.

### Search via the cognee_recall tool

```
cognee_recall(query="your search query", top_k=10, scope="auto")
```

- `scope="auto"` — session cache + permanent graph (default)
- `scope="graph"` — permanent graph only
- `scope="session"` — current session only

### Filter by category (optional)

Categories (`user_context` / `project_docs` / `agent_actions`) filter by node set. Use a direct curl to pass `node_name`:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"], "node_name": ["project_docs"]}'
```

### Ground-truth a suspicious result (debugging)

The server is authoritative. If a search returns empty but you expect content, confirm directly — **do not** conclude "not found" from an empty result without checking:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/recall" \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -d '{"query": "...", "top_k": 5, "only_context": true, "scope": ["graph"]}'
```

If the response is an `{"error": ...}` object rather than a list, the server was reachable but rejected/failed the request — that's an error, **not** "no results".

## Understanding results

Results include a `_source` field:
- `"session"` — from the session cache (current conversation)
- `"graph"` — from the permanent knowledge graph

Session entries tagged with `[category:agent]` are automatic tool call logs.

## Decision table

| Signal | Action |
|--------|--------|
| Need current session context | Already automatic, no action needed |
| User explicitly says "search cognee" | `cognee_recall(query="...")` |
| "what does the codebase do" / "what did we do last time" | `cognee_recall(query="...", scope="graph")` |
| Need a specific category | use the `node_name` curl form above |
| Auto context insufficient | `cognee_recall(query="...", scope="session")` |
| **Result empty but you expect content** | **Ground-truth via the curl above before concluding "not found"** |
