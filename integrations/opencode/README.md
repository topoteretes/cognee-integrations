# Cognee Memory Plugin for OpenCode

Gives OpenCode persistent memory across sessions using Cognee's knowledge graph. Tool calls and responses are automatically captured into session memory, relevant context is injected on every compaction, and session data is bridged into the permanent knowledge graph when idle.

## Installation

Add this package to your configuration:

1. Specify `@cognee/cognee-opencode` under the `plugin` array in your `opencode.json` configuration file:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@cognee/cognee-opencode"]
}
```

2. Make sure you have a running Cognee instance locally (`http://localhost:8000`) or configure environment variables:

```bash
export COGNEE_SERVICE_URL="http://localhost:8000"
export COGNEE_API_KEY="your-api-key" # optional
```

## Features

- **Auto-capture**: Listens to `tool.execute.after` to store all completed tool execution parameters and outputs directly into Cognee.
- **Auto-recall**: Injects relevant context into the LLM during context compaction using the `experimental.session.compacting` hook.
- **Custom Tools**:
  - `cognee_remember`: Save custom facts, user preferences, or project details into long-term graph memory.
  - `cognee_search`: Search the graph memory for specific details.
