---
name: cognee-sync
description: Sync session cache entries into the permanent Cognee knowledge graph. Run this to make session memory searchable, or it runs automatically at session end.
---

# Sync Session to Permanent Graph

Bridge session cache entries into the permanent knowledge graph.

## Instructions

The sync happens automatically at session end via the `shutdown` hook. To trigger a manual sync, use a bash command to POST the session's cached Q&A and trace data to the Cognee server's remember endpoint:

```bash
curl -s -X POST "${COGNEE_BASE_URL:-http://localhost:8011}/api/v1/remember" \
  -H "X-Api-Key: ${COGNEE_API_KEY:-}" \
  -F "datasetName=${COGNEE_PLUGIN_DATASET:-agent_sessions}" \
  -F "node_set=user_sessions_from_cache" \
  -F "run_in_background=false" \
  -F "data=<session QA and trace content>"
```

## What this does

1. **Persist session Q&A** — cognifies session text into the permanent graph
2. **Sync graph to session** — copies new graph relationships back into the session cache as a knowledge snapshot, so subsequent completions have instant access to the enriched graph context
3. **Dedup by content hash** — already-synced content is skipped

After this, session entries become searchable via the `cognee-search` skill or `cognee_recall` tool, and the graph knowledge is automatically included in session completion prompts.

## When to use

- Before searching for session content that hasn't been synced yet
- When you want to force an early sync without waiting for session end
- This runs automatically at session end via the `shutdown` hook
