---
name: cognee-remember
description: Store data permanently in the Cognee knowledge graph. Accepts a data category (user, project, or agent) to tag the data with the correct node_set for filtered retrieval.
---

# Cognee Permanent Memory Storage

Store data permanently in the Cognee knowledge graph with category tagging.

## Data categories

Cognee organizes knowledge into three categories via `node_set` tagging:

| Category | Node set | What belongs here |
|----------|----------|-------------------|
| **user** | `user_context` | User preferences, corrections, personal facts, communication style |
| **project** | `project_docs` | Repository docs, code context, architecture decisions, company data |
| **agent** | `agent_actions` | Tool call logs, reasoning traces, generated artifacts (auto-captured by hooks) |

## Instructions

Use the `cognee_recall` tool to search for existing related content before storing, then use a bash command to POST to the running Cognee server's remember endpoint:

**User data** (preferences, corrections, personal context):
```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/remember" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -F "datasetName=${COGNEE_PLUGIN_DATASET:-agent_sessions}" \
  -F "node_set=user_context" \
  -F "run_in_background=false" \
  -F "data=$ARGUMENTS"
```

**Project data** (docs, code, company knowledge):
```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/remember" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -F "datasetName=${COGNEE_PLUGIN_DATASET:-agent_sessions}" \
  -F "node_set=project_docs" \
  -F "run_in_background=false" \
  -F "data=$ARGUMENTS"
```

**Agent data** (explicit agent notes — routine tool logs are automatic):
```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/remember" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -F "datasetName=${COGNEE_PLUGIN_DATASET:-agent_sessions}" \
  -F "node_set=agent_actions" \
  -F "run_in_background=false" \
  -F "data=$ARGUMENTS"
```

A `{"ok": true}` response means the server accepted the data. An error response means the server rejected or failed the request — check `COGNEE_API_KEY` and server logs; do **not** re-run or conclude the data wasn't stored without confirming against the server.

**IMPORTANT**: The remember call always runs in the foreground (`run_in_background=false`) to ensure the full pipeline completes before returning.

## When to use

- User says "remember this" or "save this" -> category **user**
- User says "remember this about the project/codebase" -> category **project**
- You want to persist your own findings or conclusions -> category **agent**
- NOT for routine tool call logging (that's automatic via hooks with `agent_actions` tagging)

## Category routing guide

| Signal | Category |
|--------|----------|
| "remember my preference for..." | user |
| "I always want..." / "I prefer..." | user |
| "remember this about the codebase" | project |
| "save these docs" / "index this file" | project |
| "note that this API works like..." | project |
| "remember what we discovered" | agent |
| Routine tool calls | agent (automatic, no action needed) |
