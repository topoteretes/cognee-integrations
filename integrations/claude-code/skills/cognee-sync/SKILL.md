---
name: cognee-sync
description: >-
  Sync the current session from Cognee's session cache into the permanent knowledge
  graph now (Q&A turns, tool feedback, distilled lessons), so it becomes searchable and
  shows up in digests. Also runs at session end.
when_to_use: >-
  To get this session into long-term memory before it ends, e.g. "sync this session to
  Cognee", "flush session memory to the graph", "make what we just did searchable",
  "distill this session now", "Cognee can't find what we did earlier". Not for one fact
  (cognee-remember), a note with next steps (cognee-handoff), git sync or push, or a
  "sync" meeting (cognee-standup).
---

# Sync Session to Permanent Graph

Bridge session cache entries into the permanent knowledge graph.

## Instructions

Run the sync script:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/sync-session-to-graph.py"
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
