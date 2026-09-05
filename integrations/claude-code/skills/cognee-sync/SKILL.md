---
name: cognee-sync
description: Sync session cache entries into the permanent Cognee knowledge graph. Run this to make session memory searchable, or it runs automatically at session end.
---

# Sync Session to Permanent Graph

Bridge session cache entries into the permanent knowledge graph.

## Instructions

Run the sync script:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/sync-session-to-graph.py
```

## What this does

Runs Cognee's session-aware `improve` for the current session (the server bridges the session from its own session cache — no session text is re-uploaded):

1. **Apply feedback weights** -- session entries with feedback scores update graph node/edge weights
2. **Persist session Q&A** -- cognifies the session's question/answer turns into the permanent graph
3. **Persist trace feedback** -- cognifies the compact per-step tool feedback lines (not raw tool output)
4. **Distill lessons** -- curates gated session guidance into entity-anchored lessons in the graph
5. **Enrich + sync back** -- runs graph enrichment and copies new graph relationships back into the session cache as a knowledge snapshot, so subsequent completions have instant access to the enriched graph context

After this, session entries become searchable via `/cognee-memory:cognee-search`, and the graph knowledge is automatically included in session completion prompts.

## When to use

- Before searching for session content that hasn't been synced yet
- When you want to force an early sync without waiting for session end
- This runs automatically at session end via the SessionEnd hook
