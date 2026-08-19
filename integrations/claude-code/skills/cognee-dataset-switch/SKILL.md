---
name: cognee-dataset-switch
description: Switch the active Cognee dataset mid-session without losing conversation context. Seals the old dataset's bridge, re-registers the agent on the new dataset, and keeps recall of prior turns intact.
---

# Switch Dataset Mid-Session

Repoint where new memory is written — to a different Cognee dataset — while
keeping the current conversation's context.

## Instructions

Run the switch script with the new dataset name:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/dataset-switch.py <new_dataset>
```

## What this does

1. **Seals the old bridge** — flushes the current dataset's buffered Q&A/trace
   into its permanent graph *before* switching, so nothing is orphaned. The old
   `(dataset, session_id)` bridge is marked sealed. `hook.log` records
   `old bridge sealed`.
2. **Switches the active dataset** — persists the new dataset so every
   subsequent hook writes there (updates the global plugin config, and the
   project `.cognee/session-config.json` picker if present).
3. **Re-registers the agent** — calls `/api/v1/agents/register` with the *same*
   `agent_session_name` (connection handle) and `session_id`, bound to the new
   dataset. `hook.log` records `agent re-registered`.

## What stays the same

The Cognee `session_id` (and connection handle) never change, and the session
cache is keyed by `session_id` — **not** the dataset. So the conversation
context carries over: `recall` still returns prior-conversation turns after the
switch. Only *new* graph writes go to the new dataset; pre-switch turns stay in
the old dataset (no duplicate graph writes).

## When to use

- You want to store the rest of this session's memory under a different dataset
  (e.g. switching projects/topics) but keep the ongoing conversation.
- Switching to the dataset that is already active is a safe no-op.
