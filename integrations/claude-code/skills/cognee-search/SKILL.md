---
name: cognee-search
description: >-
  Search Cognee memory beyond the automatic per-prompt recall: a deeper knowledge-graph
  search, more results, or a search of one dataset.
when_to_use: >-
  E.g. "search Cognee for X", "what do you remember about X?", "dig deeper in memory".
  Not for file, code or web search.
---

# Search Cognee memory

Memory is already recalled automatically on every prompt. Use this skill when the user
asks for a deeper or more specific search.

1. Say in one short sentence that you are searching Cognee.
2. Call the `recall` tool of the plugin's `cognee` MCP server with the user's question
   as `query`. Pass `datasets` only when the user names a dataset.
3. Answer from what the tool returns. If it returns nothing relevant, say so.

If the tool says the server is not signed in, tell the user to run `/mcp` and sign in to
Cognee.
