---
name: cognee-remember
description: >-
  Store a fact, preference, decision or note permanently in Cognee memory. Plain
  "remember this" requests go here, not to Claude's built-in memory.
when_to_use: >-
  E.g. "remember that I prefer X", "save this to memory", "keep this in mind for next
  time". Not for reminders for the current task, or when CLAUDE.md or /memory is named.
---

# Remember in Cognee

1. Write the fact as one or two plain sentences that will make sense out of context.
2. Call the `remember` tool of the plugin's `cognee` MCP server with that text as
   `data`. Do not pass `session_id`: this stores it permanently.
3. Tell the user what was saved.

If the tool says the server is not signed in, tell the user to run `/mcp` and sign in to
Cognee.
